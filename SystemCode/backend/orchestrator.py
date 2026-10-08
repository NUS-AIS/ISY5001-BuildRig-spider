import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import URLError
from backend.retrieval import LocalCorpus
from backend.settings import Settings
from backend.store import StateStore
from backend.validation import validate_option
from backend.agents import AgentContext, PlannerAgent, DesktopPlanningAgent, LaptopSelectionAgent, EvidenceAgent, ReviewAgent, ExplanationAgent
from backend.harness import RunHarness, ToolHarness, ToolSpec
from backend.model_gateway import ModelGateway


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
                {"user_message": user_message, "requirements": requirements,
                 "clarification_questions": questions},
            )
            return (reply or fallback), "llm" if reply else "fallback"
        except Exception:
            return fallback, "fallback"

    def execute(self, run_id: str, maximum_options: int = 3):
        run = self.store.run(run_id)
        if not run:
            return
        try:
            self._progress(run_id, "planning", "Creating the task plan")
            req = self.store.requirements(run["session_id"], run["requirements_version"])
            if run["orchestration_mode"] == "pi":
                result = self._execute_pi(run, req, maximum_options)
            else:
                result = self._execute_dag(run, req, maximum_options)
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

    def _execute_dag(self, run: dict, req: dict, maximum_options: int) -> dict:
        rid = run["id"]
        memories = [m for m in self.store.list_memories(run["session_id"]) if m["confirmed"]]
        tools = ToolHarness(self.store, [
            ToolSpec("query_candidates", self.corpus.candidates, "safe", "Read candidate offers"),
            ToolSpec("retrieve_hybrid", self.corpus.search, "safe", "Run BM25, vector and graph retrieval"),
            ToolSpec("validate_option", validate_option, "safe", "Run deterministic validation"),
        ])
        context = AgentContext(rid, req, run["snapshot_id"], memories, tools)
        harness = RunHarness(self.store, rid)
        self._progress(rid, "selecting", "Selecting candidates from the pinned catalogue snapshot")
        plan = PlannerAgent(self.model).invoke({"context": context, "plan_version": harness.state.get("plan_version", 0)})
        harness.state["plan_version"] = plan["plan_version"]
        harness.transition("planned", "planner", "plan", plan)
        selection = (DesktopPlanningAgent().invoke({"context": context}) if req["device_type"] == "desktop"
                     else LaptopSelectionAgent().invoke({"context": context, "maximum_options": maximum_options}))
        selected_role = "desktop_planner" if selection["device_type"] == "desktop" else "laptop_selector"
        harness.transition("candidates_selected", selected_role, "select_candidates",
                           {"count": len(selection["rows"])})
        if selection["device_type"] == "laptop":
            options = [self._option("laptop", [row], req) for row in selection["rows"]]
        else:
            options = [self._option("desktop", selection["rows"], req, selection["required_categories"])] if selection["rows"] else []
        self._progress(rid, "retrieving_and_validating", "Running BM25, vector and graph retrieval and deterministic checks")
        evidence_agent, review_agent = EvidenceAgent(), ReviewAgent()
        for option in options:
            # Evidence retrieval and deterministic validation have no dependency
            # on each other, so this DAG fan-out is executed concurrently.
            with ThreadPoolExecutor(max_workers=2) as pool:
                evidence_future = pool.submit(evidence_agent.invoke, {"context": context, "option": option})
                review_future = pool.submit(review_agent.invoke, {"context": context, "option": option})
                evidence, review = evidence_future.result(), review_future.result()
            option["evidence"] = evidence["items"]
            option["validation"] = review["validation"]
            option["review"] = {"decision": review["decision"], "notes": review["review_notes"]}
            self.store.usage(rid, "evidence_agent", "agent_invocation", len(req.get("workloads", [])), len(option["evidence"]))
        harness.transition("evidence_and_validation_complete", "evidence_agent+review_agent", "retrieve_validate",
                           {"option_count": len(options)})
        self._progress(rid, "reviewing", "Reviewing constraints, evidence coverage and unknown checks")
        feasible = [x for x in options if x["validation"]["overall_status"] != "failed"]
        assistant_message = ("I could not find a configuration that satisfies every current requirement. "
                             "Try adjusting the budget or requirements.")
        generation_source = "fallback"
        if feasible:
            assistant_message = (f"I found {len(feasible)} evidence-backed option" +
                                 ("s." if len(feasible) != 1 else "."))
            if self.model.enabled:
                try:
                    narrative = ExplanationAgent(self.model).invoke({
                        "requirements": req,
                        "options": [{
                            "option_id": option["option_id"],
                            "items": option["items"],
                            "validation": option["validation"],
                            "evidence": option["evidence"],
                        } for option in feasible],
                    })
                    by_id = {item["option_id"]: item for item in narrative["options"]}
                    for option in feasible:
                        generated = by_id.get(option["option_id"])
                        if generated:
                            option["title"] = generated["title"]
                            option["reasons"] = generated["reasons"]
                            option["trade_offs"] = generated["trade_offs"]
                    assistant_message = narrative["assistant_message"]
                    generation_source = "llm"
                    harness.transition("recommendation_explained", "explanation_agent", "explain",
                                       {"option_count": len(feasible)})
                except Exception as exc:
                    limitations = [f"The local model explanation was unavailable; template wording was used ({type(exc).__name__})."]
                else:
                    limitations = []
            else:
                limitations = []
        else:
            limitations = []
        limitations.append("Compatibility remains unknown where the source catalogue lacks required specifications.")
        if self.settings.retrieval_backend != "neo4j_milvus":
            limitations.append("The local retrieval adapter implements the production contract; Neo4j and Milvus are not active.")
        return {"run_id": rid, "status": "completed", "outcome": "recommendations_available" if feasible else "no_feasible_option",
                "requirements_version": run["requirements_version"], "snapshot_id": run["snapshot_id"],
                "orchestration_mode": "dag", "options": feasible,
                "assistant_message": assistant_message, "generation_source": generation_source,
                "plan": plan, "agent_trace": harness.state["completed_tasks"],
                "tool_calls": [{"id": x["id"], "agent_role": x["agent_role"], "tool": x["tool_name"], "status": x["status"], "replay_policy": x["replay_policy"]} for x in self.store.tool_calls(rid)],
                "memory_context": {"confirmed_memory_ids": [m["id"] for m in memories]},
                "limitations": limitations}

    def _option(self, device_type: str, rows: list[dict], req: dict, required=None) -> dict:
        items = [{"category": x["category"], "product_id": str(x.get("product_id") or x["id"]), "offer_id": x["id"],
                  "name": x["name"], "quantity": 1, "price": x["price"], "currency": x["currency"],
                  "merchant": x["store"], "source_url": x["source_url"], "collected_at": x["collected_at"],
                  "availability": "in_stock_at_collection" if x.get("available") else "unavailable"} for x in rows]
        return {"option_id": "option_" + rows[0]["id"], "device_type": device_type, "title": "Catalogue-based candidate",
                "items": items, "required_categories": required or ["laptop"],
                "reasons": [f"Selected from in-stock catalogue offers under the stated SGD {req['budget']['maximum_minor']/100:.2f} budget."],
                "trade_offs": ["This is an initial evidence-backed candidate, not a benchmark-derived optimum."]}

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
