"""Deterministic guard for model-written explanations.

The explanation model may only restate verified facts. After it writes reasons and trade-offs,
sentences that name a product model or processor class that is not in the option, or that contradict
the option's graphics setup, are dropped and reported instead of shown to the user.
"""
import re

from backend.attributes import derive

MODEL_TOKEN = re.compile(
    r"\b(?:RTX|GTX|RX)\s?\d{3,4}\s?(?:TI|SUPER|XTX|XT|GRE)?\b|\bRYZEN\s?\d\s?\d{4}[A-Z0-9]*\b"
    r"|\bI[3579]-?\d{4,5}[A-Z]*\b|\bCORE\s?ULTRA\s?[3579]\s?\d{3}[A-Z]*\b", re.I)
INTEGRATED_INSTEAD = re.compile(r"integrated graphics[^.]*(?:no need|reduc|without|instead|replac|sufficient)|"
                                r"\b(?:uses?|using|relies on|runs on|depends on)\s+(?:only\s+)?(?:the\s+)?(?:(?:cpu|processor)(?:'s)?\s+)?integrated graphics|"
                                r"(?:no|without)[^.]*(?:dedicated|discrete) (?:gpu|graphics)", re.I)


# "includes a dedicated Intel GPU", "due to the discrete graphics card": the option is said to have one. A comparison
# ("not as fast as a dedicated GPU", "if you later need a dedicated GPU") is not such a claim.
DEDICATED_CLAIM = re.compile(r"\b(?:includes?|including|has|have|features?|featuring|offers?|provides?|comes with|equipped with|"
                             r"(?:due|thanks) to|because of|powered by|uses|using)\s+(?:an?\s+|the\s+|its\s+)?(?:[a-z]+\s+){0,2}?"
                             r"(?:dedicated|discrete)\s+(?:[a-z]+\s+){0,2}?(?:gpu|graphics)", re.I)
NO_DEDICATED = re.compile(r"\b(?:no|not|without|lacks?|lacking|missing|absence of|instead of|rather than|n't)\b[^.]{0,40}"
                          r"(?:dedicated|discrete)|(?:dedicated|discrete)[^.]{0,40}\b(?:is|are) (?:not|absent|missing)", re.I)
CORE_CLASS = re.compile(r"\b(?:core\s+)?i([3579])\b(?!-?\d)", re.I)          # "an Intel Core i7" with no model number


def _squash(text: str) -> str:
    return re.sub(r"[\s\-™®]", "", text).upper()


def option_facts(option: dict) -> dict:
    categories = {i["category"] for i in option["items"]}
    laptop = next((i for i in option["items"] if i["category"] == "laptop"), None)
    return {"has_graphics_card": "gpu" in categories,
            "has_dedicated_graphics": "gpu" in categories or bool(laptop and derive(laptop)["dedicated_gpu"]),
            "uses_integrated_graphics_only": option.get("device_type") == "desktop" and "gpu" not in categories,
            "owned_parts": [i["name"] for i in option["items"] if i.get("owned_by_user")]}


def unsupported(sentence: str, option: dict) -> str | None:
    names = _squash(" ".join(i["name"] for i in option["items"]))
    for token in MODEL_TOKEN.findall(sentence):
        if _squash(token) not in names:
            return f"names a model not in this option: {token}"
    facts = option_facts(option)
    if facts["has_graphics_card"] and INTEGRATED_INSTEAD.search(sentence):
        return "suggests integrated graphics replaces the included graphics card"
    if not facts["has_dedicated_graphics"] and DEDICATED_CLAIM.search(sentence) and not NO_DEDICATED.search(sentence):
        return "claims dedicated graphics, but this option has none"
    return _wrong_class(sentence, " ".join(i["name"] for i in option["items"]))


def _wrong_class(sentence: str, names: str) -> str | None:
    """"Core i7" said of a "Core 7 240H" or an i5: the class must be written that way in a listing."""
    for digit in CORE_CLASS.findall(sentence):
        if not re.search(rf"(?<![a-z])i{digit}(?![a-z])", names, re.I):
            return f"names a processor class not in this option: i{digit}"
    return None


# Preferences only a listing's own words can confirm: there is no noise figure or colour field in the catalogue.
PREFERENCE_WORDS = {"quiet": r"quiet|silent|low[- ]noise|noiseless", "white": r"\bwhite\b|\bsnow\b",
                    "RGB lighting": r"\ba?rgb\b"}


def preference_notes(option: dict, preferences: list[str]) -> tuple[list[str], list[str], list[dict]]:
    """(reasons, trade_offs, dropped) for the user's quiet / white / RGB wishes. A wish is confirmed only by
    items whose listing says so; sentences that claim it for an option where no listing does are removed
    from the option's reasons and trade-offs, and one plain trade-off says it could not be checked."""
    reasons, trade_offs, dropped = [], [], []
    bought = [i for i in option["items"] if not i.get("owned_by_user")]
    for wish in preferences:
        words = PREFERENCE_WORDS.get(wish)
        if not words:
            continue
        named = [i["name"][:50].strip() for i in bought if re.search(words, i["name"], re.I)]
        if named:
            reasons.append(f"Your preference ({wish}): the listing says so for {', '.join(named[:3])}.")
            continue
        for field in ("reasons", "trade_offs"):
            kept = []
            for sentence in option.get(field) or []:
                claims = re.search(words, sentence, re.I) and not HEDGED.search(sentence)
                if claims:
                    dropped.append({"field": field, "text": sentence, "reason": f"claims '{wish}', which no listing in this option states"})
                else:
                    kept.append(sentence)
            option[field] = kept
        trade_offs.append(f"You asked for {wish}: none of these listings says so, and the catalogue has no figure for it, "
                          "so it could not be checked.")
    return reasons, trade_offs, dropped


HEDGED = re.compile(r"\b(?:not|no|unknown|unclear|cannot|can't|louder|noisier|may|might|could)\b", re.I)


def preference_summary(message: str, options: list[dict], preferences: list[str]) -> tuple[str, list[dict]]:
    """The run-level summary without sentences that claim a quiet / white / RGB wish no listing in any option states."""
    names = " ".join(i["name"] for o in options for i in o["items"] if not i.get("owned_by_user"))
    unconfirmed = {wish: PREFERENCE_WORDS[wish] for wish in preferences
                   if wish in PREFERENCE_WORDS and not re.search(PREFERENCE_WORDS[wish], names, re.I)}
    kept, dropped = [], []
    for sentence in re.split(r"(?<=[.!?])\s+", (message or "").strip()):
        wish = next((w for w, words in unconfirmed.items() if re.search(words, sentence, re.I) and not HEDGED.search(sentence)), None)
        if wish:
            dropped.append({"text": sentence, "reason": f"claims '{wish}', which no listing states"})
        else:
            kept.append(sentence)
    return " ".join(kept), dropped


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
    any_dedicated = any(option_facts(o)["has_dedicated_graphics"] for o in options)
    all_names = " ".join(i["name"] for o in options for i in o["items"])
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
        if not problem and not any_dedicated and DEDICATED_CLAIM.search(sentence) and not NO_DEDICATED.search(sentence):
            problem = "claims dedicated graphics, but no option has any"
        problem = problem or _wrong_class(sentence, all_names)
        (dropped if problem else kept).append({"text": sentence, "reason": problem} if problem else sentence)
    return " ".join(kept), dropped
