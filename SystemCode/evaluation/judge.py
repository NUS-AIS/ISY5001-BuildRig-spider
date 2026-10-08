"""LLM-as-judge for explanation quality. Scores are a secondary signal: every judged item is saved so a
person can spot-check a sample (see results/judge_items.jsonl)."""
from __future__ import annotations

from pydantic import BaseModel, Field

from backend.requirements_parser import for_model

RUBRIC = (
    "You grade the explanation of a computer recommendation. Use ONLY the supplied requirements, items and checks. "
    "Score each criterion from 1 (poor) to 5 (excellent): grounded = every reason is supported by the listed items, "
    "prices or checks and nothing is invented; relevant = the reasons address the user's workloads, budget and "
    "preferences; honest = unknown or failed checks and trade-offs are acknowledged rather than hidden. "
    "Be strict; give 5 only when there is nothing to criticise."
)


class Grade(BaseModel):
    grounded: int = Field(ge=1, le=5)
    relevant: int = Field(ge=1, le=5)
    honest: int = Field(ge=1, le=5)
    comment: str


def judge_option(model, requirements: dict, option: dict) -> dict | None:
    payload = {
        "requirements": for_model(requirements),
        "items": [{"category": i["category"], "name": i["name"][:90], "price_sgd": float(i["price"])} for i in option["items"]],
        "checks": [{"code": c["code"], "status": c["status"]} for c in option.get("validation", {}).get("checks", [])],
        "explanation": {"title": option.get("title"), "reasons": option.get("reasons", []), "trade_offs": option.get("trade_offs", [])},
    }
    try:
        grade = model.structured(RUBRIC, payload, Grade)
    except Exception:
        return None
    overall = round((grade.grounded + grade.relevant + grade.honest) / 3, 2)
    return {"grounded": grade.grounded, "relevant": grade.relevant, "honest": grade.honest, "overall": overall,
            "comment": grade.comment, "explanation": payload["explanation"]}
