"""Run coordinator: DAG workflow with Review -> Replan loop and automatic Pi fallback (FR05, FR06).

One run is pinned to one requirements version and one data snapshot. The DAG is:

    Planner -> Desktop Planner and/or Laptop Selector -> (Evidence || Validate) -> Review
            -> [Replan the affected parts, at most ``max_revisions`` rounds] -> Explanation

A budget below the cheapest compatible build in the snapshot is answered before any agent runs.
When every option is still over budget after the revision budget is used up, the cheapest compatible
build is tried once. If options are still failing and a Pi runtime is configured, the run is handed
to Pi with the same tools and constraints. Hard constraints are never relaxed: an unsolvable request
ends as ``no_feasible_option`` with the reasons.
"""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.error import URLError
from urllib.request import Request, urlopen

from backend.agents import (AgentContext, DesktopPlanningAgent, EvidenceAgent, ExplanationAgent, LaptopSelectionAgent,
                            PlannerAgent, ReviewAgent)
from backend.explanation_guard import guard, guard_message, option_facts, unsupported
from backend.harness import RunHarness, ToolHarness, ToolSpec
from backend.model_gateway import ModelGateway
from backend.planning import Chooser, cheapest_desktop_option, feasibility_floor, revise
from backend.requirements_parser import floor_summary, for_model
from backend.revision import carry_over, requirement_changes, reuse_decision
from backend.retrieval import LocalCorpus
from backend.settings import Settings
from backend.store import StateStore
from backend.validation import validate_option


FINAL_REVIEW_TOOL_CALLS = 8      # one validation plus the evidence queries of the last review


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
            result = self._below_floor(run, req)
            if result:
                pass
            elif run["orchestration_mode"] == "pi":
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

    def _below_floor(self, run: dict, req: dict) -> dict | None:
        """The result for a budget that no compatible build in the snapshot can meet, or None if one can.
        Planning, replanning and the Pi fallback cannot change that outcome, only delay it."""
        budget = (req.get("budget") or {}).get("maximum_minor")
        try:
            floor = feasibility_floor(self.corpus, req)
        except Exception:
            floor = None
        if not floor or budget is None or budget >= floor["floor_minor"]:
            return None
        self.store.event(run["id"], "infeasible", {"budget_minor": budget, "floor_minor": floor["floor_minor"]})
        message = (f"S${budget / 100:,.0f} is not enough for this request. {floor_summary(req, floor)} "
                   "Raise the budget or change a requirement. Hard constraints were not relaxed.")
        return {"run_id": run["id"], "status": "completed", "outcome": "no_feasible_option",
                "requirements_version": run["requirements_version"], "snapshot_id": run["snapshot_id"],
                "orchestration_mode": run["orchestration_mode"], "options": [], "rejected_options": [],
                "revisions_exhausted": False, "follow_up": None, "assistant_message": message,
                "generation_source": "fallback", "plan": {"branches": [], "tasks": ["feasibility_check"]},
                "agent_trace": [], "agent_log": [], "tool_calls": [], "memory_context": {"confirmed_memory_ids": []},
                "feasibility": {"budget_minor": budget, "floor_minor": floor["floor_minor"],
                                "graphics_card_floor_minor": floor.get("graphics_card_floor_minor")},
                "limitations": ["The budget is below the cheapest compatible build in the pinned catalogue snapshot, "
                                "so no planning was run."]}

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
        out_of_allowance = False
        for option in options:
            try:
                status = self._review_loop(context, harness, option, chooser)
            except RuntimeError as exc:
                if "budget exhausted" not in str(exc):
                    raise
                # Replanning used up the run's tool or step allowance. Keep what was accepted so far and drop
                # the remaining alternatives instead of failing the whole run.
                out_of_allowance, exhausted = True, True
                if "validation" in option:
                    rejected.append(option)
                break
            (accepted if status == "accepted" else rejected).append(option)
            exhausted = exhausted or status == "exhausted"
        if not accepted:
            rescued = self._cheapest_build(context, harness, rejected, chooser)
            if rescued:
                rejected.remove(rescued)
                accepted.append(rescued)

        self._progress(rid, "explaining", "Explaining the recommendation from verified facts")
        assistant_message, generation_source, limitations = self._explain(context, harness, accepted, rejected)
        if self.settings.retrieval_backend != "neo4j_milvus":
            limitations.append("The local retrieval adapter implements the production contract; Neo4j and Milvus are not active.")
        limitations += self._degradations(context, accepted + rejected, corpus_events_before)
        limitations += list(dict.fromkeys(n for o in accepted for n in o.get("planning_notes") or []))
        if out_of_allowance:
            limitations.append("Replanning used up this run's tool allowance, so not every alternative was explored.")
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

    def _cheapest_build(self, context: AgentContext, harness: RunHarness, rejected: list[dict], chooser: Chooser) -> dict | None:
        """Last step when every option is still over budget: swap one for the cheapest compatible build and
        review it once. Planning aims near the budget, so a few replacement rounds may not reach the floor."""
        option = next((o for o in rejected if o["device_type"] == "desktop"
                       and "budget_limit" in o["validation"]["failed_codes"]), None)
        cheapest = cheapest_desktop_option(self.corpus, context.requirements) if option else None
        if not cheapest:
            return None
        # Replanning may have used the whole allowance; this one review is always allowed to run.
        context.tools.max_calls = max(context.tools.max_calls, len(self.store.tool_calls(context.run_id)) + FINAL_REVIEW_TOOL_CALLS)
        harness.max_agent_steps = max(harness.max_agent_steps, len(harness.state["completed_tasks"]) + 3)
        old = {i["category"]: i for i in option["items"]}
        new = {i["category"]: i for i in cheapest["items"]}
        changes = [{"category": c, "from": old[c]["name"] if c in old else None, "to": new[c]["name"] if c in new else None,
                    "from_price": old[c]["price"] if c in old else None, "to_price": new[c]["price"] if c in new else None}
                   for c in dict.fromkeys(list(old) + list(new))
                   if (old.get(c) or {}).get("offer_id") != (new.get(c) or {}).get("offer_id")]
        reason = ["Revision budget used up while still over budget; switched to the cheapest compatible build."]
        self.store.event(context.run_id, "replan", {"option_id": option["option_id"], "round": len(option["revision_history"]) + 1,
                                                    "reason": reason, "categories": [c["category"] for c in changes]})
        harness.state["plan_version"] += 1
        option["revision_history"].append({"round": len(option["revision_history"]) + 1, "plan_version": harness.state["plan_version"],
                                           "failed_checks": option["validation"]["failed_codes"], "reason": reason,
                                           "changes": changes})
        option.update(cheapest)
        harness.transition("replanned", "replanner", f"cheapest:{option['option_id']}", {"changes": changes})
        return option if self._review_loop(context, harness, option, chooser, final=True) == "accepted" else None

    def _review_loop(self, context: AgentContext, harness: RunHarness, option: dict, chooser: Chooser,
                     final: bool = False) -> str:
        """Evidence and validation run in parallel; failed reviews trigger targeted replanning.
        ``final`` reviews the option as it is: no further replanning, no preference revision."""
        evidence_agent, review_agent = EvidenceAgent(), ReviewAgent()
        preference_revision_used = final
        rounds = 0 if final else self.settings.max_revisions
        for round_number in range(rounds + 1):
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
            harness.transition("reviewed", "evidence_agent+review_agent",
                               f"review:{option['option_id']}:{'final' if final else f'r{round_number}'}",
                               {"decision": review["review_decision"], "status": review["validation"]["overall_status"]})
            if review["review_decision"] == "pass":
                return "accepted"
            if review["review_decision"] == "insufficient_information":
                return "rejected"
            if round_number == rounds:
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
                    "options": [{"option_id": o["option_id"], "device_type": o["device_type"],
                                 "facts": {**option_facts(o), "total_sgd": o["validation"]["total_minor"] / 100,
                                           "within_budget": "budget_limit" not in o["validation"]["failed_codes"],
                                           "unknown_checks": [c["code"] for c in o["validation"]["checks"] if c["status"] == "unknown"]},
                                 "items": [
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
                message, dropped_summary = guard_message(narrative["assistant_message"], accepted)
                source = "llm"
                if dropped_summary:
                    accepted[0].setdefault("explanation_guard", {"dropped": []})["dropped"] += [
                        {"field": "assistant_message", **d} for d in dropped_summary]
                if not message:
                    message = f"I found {len(accepted)} evidence-backed option" + ("s." if len(accepted) != 1 else ".")
                harness.transition("recommendation_explained", "explanation_agent", "explain", {"option_count": len(accepted)})
            except Exception as exc:
                limitations.append(f"The local model explanation was unavailable; template wording was used ({type(exc).__name__}).")
        if any(o["validation"]["overall_status"] == "unknown" for o in accepted):
            limitations.append("Some compatibility checks are unknown because the source listings lack the required specifications.")
        return message, source, limitations

    # ------------------------------------------------------------------------------ Pi

    def _pi_fallback(self, run: dict, req: dict, maximum_options: int, dag_result: dict) -> dict:
        self.store.event(run["id"], "fallback", {"from": "dag", "to": "pi", "reason": "revision budget exhausted"})
        self._progress(run["id"], "handing_over_to_pi_runtime", "The DAG revision budget is used up; the Pi runtime is trying")
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
