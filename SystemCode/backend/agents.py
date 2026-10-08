"""Specialist agents of the DAG workflow.

Each agent owns one decision and reaches data only through the Tool Harness. Where an agent uses
the language model, the model's output is validated by the server (allow-listed roles, offers that
the tools actually returned, decisions that cannot override a failed deterministic check), and a
deterministic fallback keeps the agent working when the model is unavailable.
"""
from dataclasses import dataclass, field
from typing import Any, Literal

from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel

from backend.harness import ToolHarness
from backend.model_gateway import ModelGateway
from backend.planning import Chooser, plan_desktop, plan_laptops


@dataclass
class AgentContext:
    run_id: str
    requirements: dict[str, Any]
    snapshot_id: str
    memories: list[dict[str, Any]]
    tools: ToolHarness
    model: ModelGateway | None = None
    log: list[dict] = field(default_factory=list)

    @property
    def model_enabled(self) -> bool:
        return bool(self.model and self.model.enabled)


class SpecialistAgent:
    role = "specialist"

    def __init__(self, fn):
        self.chain = RunnableLambda(fn)

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.chain.invoke(payload)


# --------------------------------------------------------------------------------- planner

class PlanOutput(BaseModel):
    active_agents: list[str]
    tasks: list[str]
    completion: list[str]


TASK_GRAPH = [
    {"task_id": "select_candidates", "depends_on": []},
    {"task_id": "retrieve_evidence", "depends_on": ["select_candidates"]},
    {"task_id": "validate", "depends_on": ["select_candidates"]},
    {"task_id": "review", "depends_on": ["retrieve_evidence", "validate"]},
    {"task_id": "replan_if_needed", "depends_on": ["review"]},
    {"task_id": "explain", "depends_on": ["review"]},
]


class PlannerAgent(SpecialistAgent):
    role = "planner"

    def __init__(self, model: ModelGateway | None = None):
        self.model = model
        super().__init__(self.plan)

    def plan(self, payload):
        req = payload["context"].requirements
        branches = {"desktop": ["desktop_planner"], "laptop": ["laptop_selector"],
                    "compare": ["desktop_planner", "laptop_selector"]}[req["device_type"]]
        allowed = branches + ["evidence_agent", "review_agent", "explanation_agent"]
        default = PlanOutput(active_agents=allowed, tasks=[t["task_id"] for t in TASK_GRAPH],
                             completion=["budget_checked", "compatibility_checked", "evidence_attributed", "unknowns_exposed"])
        source = "rules"
        if self.model and self.model.enabled:
            try:
                proposed = self.model.structured(
                    "Create a short recommendation task plan. Use only the allowed agent roles and tasks; every "
                    "branch agent listed in allowed_agents must stay active. Never relax hard constraints.",
                    {"requirements": {k: req.get(k) for k in ("device_type", "workloads", "preferences", "hard_constraints")},
                     "allowed_agents": allowed, "allowed_tasks": default.tasks}, PlanOutput)
                if set(proposed.active_agents) <= set(allowed) and set(branches) <= set(proposed.active_agents) \
                        and set(proposed.tasks) <= set(default.tasks) and {"validate", "review"} <= set(proposed.tasks):
                    default, source = proposed, "llm"
            except Exception:
                pass
        graph = [t for t in TASK_GRAPH if t["task_id"] in default.tasks]
        return {"plan_version": payload.get("plan_version", 0) + 1, **default.model_dump(),
                "branches": branches, "task_graph": graph, "plan_source": source}


# --------------------------------------------------------------------------------- selectors

def _query(context: AgentContext, role: str):
    def query(**kwargs):
        return context.tools.call(context.run_id, role, "query_candidates", **kwargs)
    return query


def _products(context: AgentContext, role: str):
    def products(product_ids):
        if not product_ids:
            return []
        return context.tools.call(context.run_id, role, "get_products", product_ids=product_ids)
    return products


class DesktopPlanningAgent(SpecialistAgent):
    role = "desktop_planner"

    def __init__(self):
        super().__init__(self.select)

    def select(self, payload):
        context: AgentContext = payload["context"]
        chooser = Chooser(context.model if context.model_enabled else None)
        options = [plan_desktop(_query(context, self.role), _products(context, self.role), context.requirements, chooser)]
        if payload.get("maximum_options", 1) > 1:
            value = plan_desktop(_query(context, self.role), _products(context, self.role), context.requirements,
                                 chooser, scale=0.85)
            if {i["offer_id"] for i in value["items"]} != {i["offer_id"] for i in options[0]["items"]}:
                value["variant"] = "value"
                options.append(value)
        context.log.extend(chooser.log)
        return {"device_type": "desktop", "options": options}


class LaptopSelectionAgent(SpecialistAgent):
    role = "laptop_selector"

    def __init__(self):
        super().__init__(self.select)

    def select(self, payload):
        context: AgentContext = payload["context"]
        chooser = Chooser(context.model if context.model_enabled else None)
        options = plan_laptops(_query(context, self.role), context.requirements, chooser, payload.get("maximum_options", 3))
        context.log.extend(chooser.log)
        return {"device_type": "laptop", "options": options}


# --------------------------------------------------------------------------------- evidence

class SearchQueries(BaseModel):
    queries: list[str]


class EvidenceAgent(SpecialistAgent):
    role = "evidence_agent"

    def __init__(self):
        super().__init__(self.retrieve)

    def retrieve(self, payload):
        context: AgentContext = payload["context"]
        option = payload["option"]
        req = context.requirements
        product_ids = [x["product_id"] for x in option["items"] if not x.get("owned_by_user")]
        memory_terms = " ".join(str(m["value"]) for m in context.memories if m["confirmed"])
        fallback = " ".join(req.get("workloads", []) + req.get("preferences", [])) + " " + memory_terms + " reliability performance"
        queries, source = [fallback.strip()], "rules"
        if context.model_enabled:
            try:
                generated = context.model.structured(
                    "Write up to three short search queries (keywords, no sentences) to find reviews and specifications "
                    "that show whether these products suit the user's workloads and preferences.",
                    {"workloads": req.get("workloads", []), "preferences": req.get("preferences", []),
                     "products": [x["name"][:80] for x in option["items"] if not x.get("owned_by_user")]}, SearchQueries)
                cleaned = [q.strip()[:200] for q in generated.queries if q.strip()][:3]
                if cleaned:
                    queries, source = cleaned, "llm"
            except Exception:
                pass
        merged: dict[str, dict] = {}
        backend = None
        for query in queries:
            result = context.tools.call(context.run_id, self.role, "retrieve_hybrid", query=query,
                                        categories=[], product_ids=product_ids, top_k=8)
            backend = result.get("retrieval_backend")
            for item in result["items"]:
                if item["evidence_id"] not in merged or item["score"] > merged[item["evidence_id"]]["score"]:
                    merged[item["evidence_id"]] = {**item, "query": query}
        items = sorted(merged.values(), key=lambda x: x["score"], reverse=True)[:10]
        return {"items": items, "queries": queries, "query_source": source, "retrieval_backend": backend}


# --------------------------------------------------------------------------------- review

class ReviewJudgement(BaseModel):
    decision: Literal["pass", "revise", "insufficient_information"]
    change_categories: list[str]
    reason: str


class ReviewAgent(SpecialistAgent):
    role = "review_agent"

    def __init__(self):
        super().__init__(self.review)

    def review(self, payload):
        context: AgentContext = payload["context"]
        option = payload["option"]
        validation = context.tools.call(context.run_id, self.role, "validate_option",
                                        option=option, requirements=context.requirements)
        changeable = sorted({i["category"] for i in option["items"] if not i.get("locked")})
        notes, source = [], "rules"
        if validation["overall_status"] == "failed":
            categories = [c for c in validation["affected_categories"] if c in changeable]
            decision = "revise" if categories else "insufficient_information"
            notes.append("Failed checks: " + ", ".join(validation["failed_codes"]))
        else:
            decision, categories = "pass", []
            if context.model_enabled and not payload.get("preference_revision_used"):
                try:
                    judgement = context.model.structured(
                        "You review a recommended computer configuration. Deterministic checks have already "
                        "passed or are unknown; you must not question them. Decide only whether a part clearly "
                        "contradicts the user's stated workloads or preferences. Answer 'revise' only for a clear "
                        "mismatch and name the categories to change; otherwise answer 'pass'.",
                        {"workloads": context.requirements.get("workloads", []),
                         "preferences": context.requirements.get("preferences", []),
                         "items": [{"category": i["category"], "name": i["name"][:90], "price_sgd": float(i["price"])}
                                   for i in option["items"]],
                         "checks": [{"code": c["code"], "status": c["status"]} for c in validation["checks"]]},
                        ReviewJudgement)
                    source = "llm"
                    if judgement.decision == "revise":
                        allowed = [c for c in judgement.change_categories if c in changeable]
                        if allowed:
                            decision, categories = "revise", allowed
                            notes.append("Preference review: " + judgement.reason)
                except Exception:
                    pass
            if validation["overall_status"] == "unknown":
                notes.append("Unknown checks are preserved and shown to the user.")
        legacy = {"pass": "accept_with_unknowns" if validation["overall_status"] == "unknown" else "accept",
                  "revise": "revise", "insufficient_information": "reject"}[decision]
        return {"decision": legacy, "review_decision": decision, "change_categories": categories,
                "validation": validation, "review_notes": notes, "review_source": source}


# --------------------------------------------------------------------------------- explanation

class OptionNarrative(BaseModel):
    option_id: str
    title: str
    reasons: list[str]
    trade_offs: list[str]


class RecommendationNarrative(BaseModel):
    assistant_message: str
    options: list[OptionNarrative]


class ExplanationAgent(SpecialistAgent):
    role = "explanation_agent"

    def __init__(self, model: ModelGateway):
        self.model = model
        super().__init__(self.explain)

    def explain(self, payload):
        return self.model.structured(
            "You explain computer recommendations to an end user. Always answer in English. Use only the supplied verified "
            "requirements, catalogue items, validation results, and retrieved evidence. Never add or "
            "change a product, price, specification, benchmark, review, or compatibility claim. Treat "
            "unknown validation checks as unknown. Give each option 2-4 concise reasons and 1-3 honest "
            "trade-offs. Keep option_id unchanged. Write assistant_message as a natural response that "
            "summarises the result and mentions unresolved checks when present.",
            payload,
            RecommendationNarrative,
        ).model_dump()
