"""Deterministic guard for model-written explanations.

The explanation model may only restate verified facts. After it writes reasons and trade-offs,
sentences that name a product model that is not in the option, or that contradict the option's
graphics setup, are dropped and reported instead of shown to the user.
"""
import re

MODEL_TOKEN = re.compile(
    r"\b(?:RTX|GTX|RX)\s?\d{3,4}\s?(?:TI|SUPER|XTX|XT|GRE)?\b|\bRYZEN\s?\d\s?\d{4}[A-Z0-9]*\b"
    r"|\bI[3579]-?\d{4,5}[A-Z]*\b|\bCORE\s?ULTRA\s?[3579]\s?\d{3}[A-Z]*\b", re.I)
INTEGRATED_INSTEAD = re.compile(r"integrated graphics[^.]*(?:no need|reduc|without|instead|replac|sufficient)|"
                                r"(?:no|without)[^.]*(?:dedicated|discrete) (?:gpu|graphics)", re.I)


def _squash(text: str) -> str:
    return re.sub(r"[\s\-™®]", "", text).upper()


def option_facts(option: dict) -> dict:
    categories = {i["category"] for i in option["items"]}
    return {"has_graphics_card": "gpu" in categories,
            "uses_integrated_graphics_only": option.get("device_type") == "desktop" and "gpu" not in categories,
            "owned_parts": [i["name"] for i in option["items"] if i.get("owned_by_user")]}


def unsupported(sentence: str, option: dict) -> str | None:
    names = _squash(" ".join(i["name"] for i in option["items"]))
    for token in MODEL_TOKEN.findall(sentence):
        if _squash(token) not in names:
            return f"names a model not in this option: {token}"
    if option_facts(option)["has_graphics_card"] and INTEGRATED_INSTEAD.search(sentence):
        return "suggests integrated graphics replaces the included graphics card"
    return None


def guard(option: dict, field: str) -> list[dict]:
    """Filter option[field] in place; return the dropped sentences with the reason."""
    kept, dropped = [], []
    for sentence in option.get(field) or []:
        problem = unsupported(sentence, option)
        if problem:
            dropped.append({"field": field, "text": sentence, "reason": problem})
        else:
            kept.append(sentence)
    option[field] = kept
    return dropped


OVER_BUDGET = re.compile(r"over (?:the |your )?budget|exceed\w* (?:the |your )?budget|above (?:the |your )?budget", re.I)
ALL_PASSED = re.compile(r"all (?:compatibility )?checks? (?:have |are )?passed|fully (?:verified|compatible)|no unresolved", re.I)


def guard_message(message: str, options: list[dict]) -> tuple[str, list[dict]]:
    """Filter the run-level summary: every sentence must be consistent with all options' verified facts."""
    names = _squash(" ".join(i["name"] for o in options for i in o["items"]))
    any_over = any("budget_limit" in o.get("validation", {}).get("failed_codes", []) for o in options)
    any_unknown = any(c["status"] == "unknown" for o in options for c in o.get("validation", {}).get("checks", []))
    kept, dropped = [], []
    for sentence in re.split(r"(?<=[.!?])\s+", message.strip()):
        problem = None
        for token in MODEL_TOKEN.findall(sentence):
            if _squash(token) not in names:
                problem = f"names a model not in any option: {token}"
        if not problem and OVER_BUDGET.search(sentence) and not any_over:
            problem = "claims an option is over budget, but every option passed the budget check"
        if not problem and ALL_PASSED.search(sentence) and any_unknown:
            problem = "claims all checks passed, but some checks are unknown"
        (dropped if problem else kept).append({"text": sentence, "reason": problem} if problem else sentence)
    return " ".join(kept), dropped
