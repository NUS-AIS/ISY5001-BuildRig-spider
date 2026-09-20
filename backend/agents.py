from dataclasses import dataclass
from typing import Any
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel
from backend.harness import ToolHarness
from backend.model_gateway import ModelGateway


@dataclass
class AgentContext:
    run_id: str
    requirements: dict[str, Any]
    snapshot_id: str
    memories: list[dict[str, Any]]
    tools: ToolHarness


class SpecialistAgent:
    role = "specialist"
    def __init__(self, fn):
        self.chain = RunnableLambda(fn)

    def invoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.chain.invoke(payload)


class PlanOutput(BaseModel):
    active_agents: list[str]
    tasks: list[str]
    completion: list[str]


class OptionNarrative(BaseModel):
    option_id: str
    title: str
    reasons: list[str]
    trade_offs: list[str]


class RecommendationNarrative(BaseModel):
    assistant_message: str
    options: list[OptionNarrative]


class PlannerAgent(SpecialistAgent):
    role = "planner"
    def __init__(self, model: ModelGateway | None = None):
        self.model = model
        super().__init__(self.plan)

    def plan(self, payload):
        req = payload["context"].requirements
        branch = req["device_type"]
        allowed = ["desktop_planner"] if branch == "desktop" else ["laptop_selector"]
        default = PlanOutput(active_agents=allowed,
                             tasks=["select_candidates", "retrieve_evidence", "validate", "review"],
                             completion=["budget_checked", "evidence_attributed", "unknowns_exposed"])
        if self.model and self.model.enabled:
            proposed = self.model.structured(
                "Create a short recommendation task plan. Use only the allowed agent roles and tasks. Never relax hard constraints.",
                {"requirements": req, "allowed_agents": allowed, "allowed_tasks": default.tasks}, PlanOutput)
            if set(proposed.active_agents) <= set(allowed) and set(proposed.tasks) <= set(default.tasks):
                default = proposed
        return {"plan_version": payload.get("plan_version", 0) + 1, **default.model_dump()}


class DesktopPlanningAgent(SpecialistAgent):
    role = "desktop_planner"
    categories = ["cpu", "motherboard", "ram", "ssd", "gpu", "psu", "case", "cooler"]
    allocation = {"cpu": .18, "motherboard": .10, "ram": .07, "ssd": .07, "gpu": .36, "psu": .07, "case": .08, "cooler": .07}
    def __init__(self): super().__init__(self.select)
    def select(self, payload):
        context: AgentContext = payload["context"]; budget = context.requirements["budget"]["maximum_minor"]
        selected = []
        for category in self.categories:
            rows = context.tools.call(context.run_id, self.role, "query_candidates", category=category,
                                      maximum_minor=int(budget * self.allocation[category]), limit=1)
            if rows: selected.append(rows[0])
        return {"device_type": "desktop", "rows": selected, "required_categories": self.categories}


class LaptopSelectionAgent(SpecialistAgent):
    role = "laptop_selector"
    def __init__(self): super().__init__(self.select)
    @staticmethod
    def select(payload):
        context: AgentContext = payload["context"]
        rows = context.tools.call(context.run_id, "laptop_selector", "query_candidates", category="laptop",
                                  maximum_minor=context.requirements["budget"]["maximum_minor"], limit=payload["maximum_options"])
        return {"device_type": "laptop", "rows": rows, "required_categories": ["laptop"]}


class EvidenceAgent(SpecialistAgent):
    role = "evidence_agent"
    def __init__(self): super().__init__(self.retrieve)
    @staticmethod
    def retrieve(payload):
        context: AgentContext = payload["context"]; option = payload["option"]
        memory_terms = " ".join(str(m["value"]) for m in context.memories if m["confirmed"])
        query = " ".join(context.requirements.get("workloads", [])) + " " + memory_terms + " reliability performance"
        return context.tools.call(context.run_id, "evidence_agent", "retrieve_hybrid", query=query,
                                  categories=[], product_ids=[x["product_id"] for x in option["items"]], top_k=8)


class ReviewAgent(SpecialistAgent):
    role = "review_agent"
    def __init__(self): super().__init__(self.review)
    @staticmethod
    def review(payload):
        context: AgentContext = payload["context"]; option = payload["option"]
        validation = context.tools.call(context.run_id, "review_agent", "validate_option",
                                        option=option, requirements=context.requirements)
        decision = "reject" if validation["overall_status"] == "failed" else "accept_with_unknowns" if validation["overall_status"] == "unknown" else "accept"
        return {"decision": decision, "validation": validation,
                "review_notes": ["Unknown checks are preserved and must be shown to the user."] if decision == "accept_with_unknowns" else []}


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
