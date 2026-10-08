"""Run coordinator: DAG workflow with Review -> Replan loop and automatic Pi fallback (FR05, FR06).

One run is pinned to one requirements version and one data snapshot. The DAG is:

    Planner -> Desktop Planner and/or Laptop Selector -> (Evidence || Validate) -> Review
            -> [Replan the affected parts, at most ``max_revisions`` rounds] -> Explanation

When every option is still failing after the revision budget is used up and a Pi runtime is
configured, the run is handed to Pi with the same tools and constraints. Hard constraints are never
relaxed: an unsolvable request ends as ``no_feasible_option`` with the reasons.
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import URLError
from urllib.request import Request, urlopen

from backend.agents import (AgentContext, DesktopPlanningAgent, EvidenceAgent, ExplanationAgent, LaptopSelectionAgent,
                            PlannerAgent, ReviewAgent)
from backend.explanation_guard import guard, option_facts, unsupported
from backend.harness import RunHarness, ToolHarness, ToolSpec
from backend.model_gateway import ModelGateway
from backend.planning import Chooser, revise
from backend.requirements_parser import for_model
from backend.revision import carry_over, requirement_changes, reuse_decision
from backend.retrieval import LocalCorpus
from backend.settings import Settings
from backend.store import StateStore
from backend.validation import validate_option


class RecommendationEngine:
    def __init__(self, store: StateStore, corpus: LocalCorpus, settings: Settings):
        self.store, self.corpus, self.settings = store, corpus, settings
        self.model = ModelGateway(
            settings.model_name,
            settings.model_provider,
            settings.ollama_base_url,
            settings.ollama_reasoning,
            settings.ollama_num_ctx,
            settings.ollama_num_predict,
        )

    def intake_reply(self, user_message: str, requirements: dict, questions: list[dict]) -> tuple[str, str]:
        fallback = (" ".join(question["text"] for question in questions) if questions else
                    "Your requirements are ready. I can now prepare an evidence-backed recommendation.")
        if not self.model.enabled:
            return fallback, "fallback"
        try:
            reply = self.model.text(
                "You are BuildRig, a concise computer recommendation assistant. Always answer in English, "
                "even if the user writes in another language. Respond naturally to the "
                "user using only the parsed requirements supplied below. If clarification_questions is "
                "not empty, ask exactly those questions without answering them or inventing details. If "
                "it is empty, briefly confirm the understood device type, budget and workload, then say "
                "the recommendation can be generated. Do not recommend products yet.",
                {"user_message": user_message, "requirements": for_model(requirements),
                 "clarification_questions": questions},
            )
            return (reply or fallback), "llm" if reply else "fallback"
        except Exception:
            return fallback, "fallback"

    def execute(self, run_id: str, maximum_options: int = 3):
        run = self.store.run(run_id)
        if not run:
            return
        usage_before, started = self.model.snapshot(), time.time()
        try:
            self._progress(run_id, "planning", "Creating the task plan")
            req = self.store.requirements(run["session_id"], run["requirements_version"])
            if run["orchestration_mode"] == "pi":
                result = self._execute_pi(run, req, maximum_options)
            else:
                result = self._execute_dag(run, req, maximum_options)
                if result["outcome"] == "no_feasible_option" and result.get("revisions_exhausted") \
                        and self.settings.pi_runtime_url:
                    result = self._pi_fallback(run, req, maximum_options, result)
            if result.get("orchestration_mode") != "pi" or "model_usage" not in result:
                after = self.model.snapshot()
                result["model_usage"] = {k: after[k] - usage_before[k] for k in after}
            result["duration_seconds"] = round(time.time() - started, 2)
            self.store.set_run(run_id, "completed", "completed", result=result)
            self.store.event(run_id, "completed", {"stage": "completed", "outcome": result["outcome"]})
        except Exception as exc:
            if self.store.is_cancelled(run_id):
                return
            error = {"code": "RUN_FAILED", "message": str(exc)}
            self.store.set_run(run_id, "failed", "failed", error=error)
            self.store.event(run_id, "failed", error)

    def _progress(self, rid: str, stage: str, message: str):
        self.store.set_run(rid, "running", stage)
        self.store.event(rid, "progress", {"stage": stage, "message": message})

    def tools(self) -> list[ToolSpec]:
        return [
            ToolSpec("query_candidates", self.corpus.candidates, "safe", "Read compatible in-stock candidate offers"),
            ToolSpec("get_products", self.corpus.products, "safe", "Read offers for specific product ids"),
            ToolSpec("retrieve_hybrid", self.corpus.search, "safe", "Run BM25, vector and graph retrieval"),
            ToolSpec("validate_option", validate_option, "safe", "Run deterministic validation"),
        ]

    # ------------------------------------------------------------------------------ DAG

    def _execute_dag(self, run: dict, req: dict, maximum_options: int) -> dict:
        rid = run["id"]
        memories = [m for m in self.store.list_memories(run["session_id"]) if m["confirmed"]]
        tools = ToolHarness(self.store, self.tools(), max_calls=200)
        context = AgentContext(rid, req, run["snapshot_id"], memories, tools, self.model if self.model.enabled else None)
        harness = RunHarness(self.store, rid, max_agent_steps=60)

        plan = PlannerAgent(self.model).invoke({"context": context, "plan_version": harness.state.get("plan_version", 0)})
        harness.state["plan_version"] = plan["plan_version"]
        harness.transition("planned", "planner", "plan", plan)

        follow_up, options = self._follow_up(run, req, harness)
        corpus_events_before = len(getattr(self.corpus, "events", []))
        if follow_up and follow_up["reused"]:
            self._progress(rid, "selecting", "Reusing the previous recommendation; only affected parts will change")
            options = carry_over(follow_up.pop("base_options"), req, self.corpus.products)
            harness.transition("previous_options_reused", "replanner", "reuse_previous", {"changes": follow_up["changes"]})
        else:
            if follow_up:
                follow_up.pop("base_options", None)
            self._progress(rid, "selecting", "Selecting compatible candidates from the pinned catalogue snapshot")
        if not options and "desktop_planner" in plan["branches"]:
            options += DesktopPlanningAgent().invoke({"context": context, "maximum_options": maximum_options})["options"]
            harness.transition("desktop_selected", "desktop_planner", "select_candidates", {"count": len(options)})
        if not (follow_up and follow_up["reused"]) and "laptop_selector" in plan["branches"]:
            laptops = LaptopSelectionAgent().invoke({"context": context, "maximum_options": maximum_options})["options"]
            options += laptops
            harness.transition("laptops_selected", "laptop_selector", "select_candidates", {"count": len(laptops)})
        for index, option in enumerate(options):
            option.setdefault("option_id", f"option_{index + 1}_{option['items'][0]['offer_id'] or 'owned'}")
            option["revision_history"] = []

        self._progress(rid, "retrieving_and_validating", "Running hybrid retrieval and deterministic checks")
        accepted, rejected = [], []
        chooser = Chooser(context.model)
        exhausted = False
        for option in options:
            status = self._review_loop(context, harness, option, chooser)
            (accepted if status == "accepted" else rejected).append(option)
            exhausted = exhausted or status == "exhausted"

        self._progress(rid, "explaining", "Explaining the recommendation from verified facts")
        assistant_message, generation_source, limitations = self._explain(context, harness, accepted, rejected)
        if self.settings.retrieval_backend != "neo4j_milvus":
            limitations.append("The local retrieval adapter implements the production contract; Neo4j and Milvus are not active.")
        limitations += self._degradations(context, accepted + rejected, corpus_events_before)
        if follow_up:
            self._describe_follow_up(follow_up, accepted)
        for option in accepted:
            option.pop("tried_offer_ids", None)
        return {"run_id": rid, "status": "completed",
                "outcome": "recommendations_available" if accepted else "no_feasible_option",
                "requirements_version": run["requirements_version"], "snapshot_id": run["snapshot_id"],
                "orchestration_mode": "dag", "options": accepted,
                "rejected_options": [{"option_id": o["option_id"], "device_type": o["device_type"],
                                      "failed_checks": [c for c in o["validation"]["checks"] if c["status"] == "failed"],
                                      "revision_history": o["revision_history"]} for o in rejected],
                "revisions_exhausted": exhausted, "follow_up": follow_up,
                "assistant_message": assistant_message, "generation_source": generation_source,
                "plan": plan, "agent_trace": harness.state["completed_tasks"], "agent_log": context.log,
                "tool_calls": [{"id": x["id"], "agent_role": x["agent_role"], "tool": x["tool_name"], "status": x["status"],
                                "replay_policy": x["replay_policy"]} for x in self.store.tool_calls(rid)],
                "memory_context": {"confirmed_memory_ids": [m["id"] for m in memories]},
                "limitations": limitations}

    def _follow_up(self, run: dict, req: dict, harness: RunHarness) -> tuple[dict | None, list]:
        base_id = harness.state.get("base_run_id")
        if not base_id:
            return None, []
        base = self.store.run(base_id)
        base_req = self.store.requirements(base["session_id"], base["requirements_version"]) or {}
        changes = requirement_changes(base_req, req)
        reuse, reason = reuse_decision(base_req, req, changes)
        base_options = (base.get("result") or {}).get("options", [])
        info = {"base_run_id": base_id, "changes": changes, "reused": reuse and bool(base_options), "reason": reason,
                "base_options": base_options}
        if reuse and not base_options:
            info["reason"] = "the previous run had no accepted option; planning again"
        self.store.event(run["id"], "follow_up", {k: v for k, v in info.items() if k != "base_options"})
        return info, []

    @staticmethod
    def _describe_follow_up(follow_up: dict, accepted: list[dict]) -> None:
        if not follow_up["reused"]:
            return
        for option in accepted:
            kept = [i["name"] for i in option["items"] if i.get("carried_over")]
            changed = [i["name"] for i in option["items"] if not i.get("carried_over")]
            option["follow_up"] = {"kept_parts": kept, "changed_parts": changed}

    def _degradations(self, context: AgentContext, options: list[dict], events_before: int) -> list[str]:
        notes = []
        routes = sorted({f"{d['route']} ({d['error']})" for o in options for d in (o.get("retrieval") or {}).get("degraded_routes", [])})
        if routes:
            notes.append("Retrieval ran without " + ", ".join(routes) + "; evidence may be incomplete.")
        if len(getattr(self.corpus, "events", [])) > events_before:
            notes.append("The Neo4j candidate query failed during this run; candidates came from the pinned snapshot file.")
        if any(e.get("event") in ("chooser_fallback", "laptop_ranking_fallback") for e in context.log):
            notes.append("The local model was unavailable for part selection; deterministic choices were used.")
        return notes

    def _review_loop(self, context: AgentContext, harness: RunHarness, option: dict, chooser: Chooser) -> str:
        """Evidence and validation run in parallel; failed reviews trigger targeted replanning."""
        evidence_agent, review_agent = EvidenceAgent(), ReviewAgent()
        preference_revision_used = False
        for round_number in range(self.settings.max_revisions + 1):
            with ThreadPoolExecutor(max_workers=2) as pool:
                evidence_future = pool.submit(evidence_agent.invoke, {"context": context, "option": option})
                review_future = pool.submit(review_agent.invoke, {"context": context, "option": option,
                                                                  "preference_revision_used": preference_revision_used})
                evidence, review = evidence_future.result(), review_future.result()
            option["evidence"] = evidence["items"]
            option["evidence_queries"] = evidence["queries"]
            option["retrieval"] = {"backend": evidence.get("retrieval_backend"), "degraded_routes": evidence.get("degraded_routes", [])}
            option["validation"] = review["validation"]
            option["review"] = {"decision": review["decision"], "notes": review["review_notes"], "source": review["review_source"]}
            harness.transition("reviewed", "evidence_agent+review_agent", f"review:{option['option_id']}:r{round_number}",
                               {"decision": review["review_decision"], "status": review["validation"]["overall_status"]})
            if review["review_decision"] == "pass":
                return "accepted"
            if review["review_decision"] == "insufficient_information":
                return "rejected"
            if round_number == self.settings.max_revisions:
                return "exhausted"
            if review["validation"]["overall_status"] != "failed":
                preference_revision_used = True
            self.store.event(context.run_id, "replan", {"option_id": option["option_id"], "round": round_number + 1,
                                                        "reason": review["review_notes"],
                                                        "categories": review["change_categories"]})
            revised, changes = revise(option, review["change_categories"], review["validation"],
                                      lambda **kw: context.tools.call(context.run_id, "replanner", "query_candidates", **kw),
                                      context.requirements, chooser)
            harness.state["plan_version"] += 1
            option.update(revised)
            option["revision_history"].append({"round": round_number + 1, "plan_version": harness.state["plan_version"],
                                               "failed_checks": review["validation"]["failed_codes"],
                                               "reason": review["review_notes"], "changes": changes})
            harness.transition("replanned", "replanner", f"replan:{option['option_id']}:r{round_number + 1}",
                               {"changes": changes})
            if not changes or all(c.get("to") is None for c in changes):
                return "exhausted"
        return "exhausted"

    def _explain(self, context, harness, accepted, rejected):
        limitations = []
        if not accepted:
            reasons = sorted({c["reason"] for o in rejected for c in o["validation"]["checks"] if c["status"] == "failed"})
            message = ("I could not find a configuration that satisfies every current requirement. "
                       + (" ".join(reasons[:3]) + " " if reasons else "")
                       + "You can adjust the budget or requirements; hard constraints are never relaxed automatically.")
            return message, "fallback", limitations
        message, source = f"I found {len(accepted)} evidence-backed option" + ("s." if len(accepted) != 1 else "."), "fallback"
        for option in accepted:
            option.setdefault("title", "Catalogue-based candidate")
            option.setdefault("reasons", [i.get("selection_reason") for i in option["items"] if i.get("selection_reason")][:4])
            option.setdefault("trade_offs", ["Unknown checks are listed with the option and should be confirmed before purchase."])
        if context.model_enabled:
            try:
                narrative = ExplanationAgent(context.model).invoke({
                    "requirements": for_model(context.requirements),
                    "options": [{"option_id": o["option_id"], "device_type": o["device_type"], "facts": option_facts(o), "items": [
                        {**{k: i.get(k) for k in ("category", "name", "price", "merchant", "selection_reason")},
                         "specs": {k: v for k, v in (i.get("specs") or {}).items() if v is not None}} for i in o["items"]],
                        "validation": [{"code": c["code"], "status": c["status"], "reason": c["reason"]} for c in o["validation"]["checks"]],
                        "evidence": [{"kind": e["kind"], "excerpt": e["excerpt"][:300]} for e in o["evidence"][:5]]}
                        for o in accepted]})
                by_id = {item["option_id"]: item for item in narrative["options"]}
                for option in accepted:
                    generated = by_id.get(option["option_id"])
                    if generated:
                        option["reasons"], option["trade_offs"] = generated["reasons"], generated["trade_offs"]
                        dropped = guard(option, "reasons") + guard(option, "trade_offs")
                        title_problem = unsupported(generated["title"], option)
                        option["title"] = option.get("title") if title_problem else generated["title"]
                        if title_problem:
                            dropped.append({"field": "title", "text": generated["title"], "reason": title_problem})
                        option["explanation_guard"] = {"dropped": dropped}
                        if not option["reasons"]:
                            option["reasons"] = [i.get("selection_reason") for i in option["items"] if i.get("selection_reason")][:4]
                message, source = narrative["assistant_message"], "llm"
                harness.transition("recommendation_explained", "explanation_agent", "explain", {"option_count": len(accepted)})
            except Exception as exc:
                limitations.append(f"The local model explanation was unavailable; template wording was used ({type(exc).__name__}).")
        if any(o["validation"]["overall_status"] == "unknown" for o in accepted):
            limitations.append("Some compatibility checks are unknown because the source listings lack the required specifications.")
        return message, source, limitations

    # ------------------------------------------------------------------------------ Pi

    def _pi_fallback(self, run: dict, req: dict, maximum_options: int, dag_result: dict) -> dict:
        self.store.event(run["id"], "fallback", {"from": "dag", "to": "pi", "reason": "revision budget exhausted"})
        try:
            result = self._execute_pi(run, req, maximum_options)
        except Exception as exc:
            dag_result["limitations"].append(f"Pi fallback was attempted but failed: {exc}")
            return dag_result
        result["fallback"] = {"from": "dag", "reason": "DAG revision budget exhausted",
                              "dag_rejected_options": dag_result["rejected_options"]}
        return result

    def _execute_pi(self, run: dict, req: dict, maximum_options: int) -> dict:
        if not self.settings.pi_runtime_url:
            raise RuntimeError("Pi orchestration is not configured. Set BUILDRIG_PI_RUNTIME_URL or use orchestration_mode=dag.")
        payload = json.dumps({"run": run, "requirements": req, "maximum_options": maximum_options}).encode()
        request = Request(self.settings.pi_runtime_url.rstrip("/") + "/v1/runs", data=payload,
                          headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urlopen(request, timeout=self.settings.pi_runtime_timeout_seconds) as response:
                result = json.loads(response.read())
        except URLError as exc:
            raise RuntimeError(f"Pi runtime unavailable: {exc.reason}") from exc
        result.setdefault("orchestration_mode", "pi")
        return result
