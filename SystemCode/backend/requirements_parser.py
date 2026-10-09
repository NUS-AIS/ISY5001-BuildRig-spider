"""Natural-language requirement understanding (FR07).

Two layers work together:

* **Rules** (regex) always run. They are fast, deterministic and are the authority for the
  budget number, so a model can never invent or change the user's budget.
* **LLM structured extraction** (when a model is configured) adds what rules miss: workloads
  phrased freely, soft preferences, components the user insists on buying ("locked") and
  components the user already owns. Its numbers are only accepted when they literally occur in
  the user's message.

The merged result is a requirements dict plus a list of clarification questions. A request is
ready for planning only when the question list is empty.
"""
import re
from copy import deepcopy
from typing import Literal

from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel, Field

COMPARE_PATTERNS = ("台式还是笔记本", "笔记本还是台式", "desktop or laptop", "laptop or desktop", "desktop vs laptop",
                    "laptop vs desktop", "not sure whether", "compare a desktop", "compare desktop")
MINIMUM_FEASIBLE_MINOR = {"desktop": 60000, "laptop": 50000, "compare": 60000}
PREFERENCE_PATTERNS = {
    "quiet": r"\bquiet\b|\bsilent\b|low[- ]noise|静音|安静",
    "white": r"\bwhite\b|白色",
    "RGB lighting": r"\brgb\b|灯效",
    "portable": r"\bportable\b|\blight(?:weight)?\b|轻薄|便携",
    "compact": r"\bcompact\b|small form factor|\bsff\b|小机箱",
}
KNOWN_WORKLOADS = ("SolidWorks", "MATLAB", "ANSYS", "AutoCAD", "Blender", "Premiere", "gaming", "video editing",
                   "programming", "machine learning", "deep learning", "university", "3D modelling", "office",
                   "streaming", "编程", "游戏", "办公", "剪辑")


# ------------------------------------------------------------------------------ rule layer

def _parse(payload: dict) -> dict:
    text = payload["text"]
    req = deepcopy(payload.get("current") or {})
    lower = text.casefold()
    normalized = re.sub(r"(?<=\d),(?=\d)", "", lower)
    if any(p in lower for p in COMPARE_PATTERNS):
        req["device_type"] = "compare"
    elif any(x in lower for x in ("笔记本", "laptop", "notebook")):
        req["device_type"] = "laptop"
    elif any(x in lower for x in ("台式", "组装", "装机", "desktop", "workstation")) or re.search(r"\bpc\b", lower):
        req["device_type"] = "desktop"
    money = (re.search(r"(?:预算|budget|max(?:imum)? budget|under|up to|maximum|below|within)\s*(?:是|为|is|of|:)?\s*(?:s\$|sgd|\$)?\s*([1-9]\d{2,5})(?:\.\d{1,2})?", normalized)
             or re.search(r"(?:s\$|sgd|\$)\s*([1-9]\d{2,5})(?:\.\d{1,2})?", normalized)
             or re.search(r"([1-9]\d{2,5})(?:\.\d{1,2})?\s*(?:新币|新加坡元|sgd|dollars)", normalized))
    if not money and not req.get("budget"):
        money = re.fullmatch(r"\s*(?:s\$|sgd|\$)?\s*([1-9]\d{2,5})(?:\.\d{1,2})?\s*(?:sgd)?\s*", normalized)
    if money:
        req["budget"] = {"currency": "SGD", "maximum_minor": int(money.group(1)) * 100, "is_hard_limit": True}
    ram = re.search(r"(\d{1,3})\s*gb\s*(?:内存|ram|memory)", lower) or re.search(r"(?:内存|ram|memory)\s*(?:of\s*)?(\d{1,3})\s*gb", lower)
    if not ram and not re.search(r"\d\s*(?:tb|gb)\s*(?:ssd|storage|硬盘|存储)|rtx|gtx|\brx\s?\d|vram|gddr|显卡|显存", lower):
        ram = re.search(r"(\d{1,3})\s*gb", lower)
    if ram and int(ram.group(1)) in (8, 16, 24, 32, 48, 64, 96, 128):
        req.setdefault("hard_constraints", {})["minimum_memory_gb"] = int(ram.group(1))
    storage = re.search(r"(\d(?:\.\d)?)\s*tb\s*(?:ssd|storage|硬盘|存储)?|(\d{3,4})\s*gb\s*(?:ssd|storage|硬盘|存储)", lower)
    if storage:
        gb = int(float(storage.group(1)) * 1024) if storage.group(1) else int(storage.group(2))
        req.setdefault("hard_constraints", {})["minimum_storage_gb"] = gb
    workloads = req.setdefault("workloads", [])
    for name in KNOWN_WORKLOADS:
        if name.casefold() in lower and name not in workloads:
            workloads.append(name)
    preferences = req.setdefault("preferences", [])
    for label, pattern in PREFERENCE_PATTERNS.items():
        if re.search(pattern, lower) and label not in preferences:
            preferences.append(label)
    req.setdefault("locked_product_ids", [])
    req.setdefault("locked_items", [])
    req.setdefault("owned_components", [])
    req.setdefault("preferences", [])
    req.setdefault("cost_scope", {"monitor": "unspecified", "delivery": "unspecified", "assembly": "unspecified", "operating_system": "unspecified"})
    if "不包含显示器" in text or "without monitor" in lower or "no monitor" in lower:
        req["cost_scope"]["monitor"] = "excluded"
    return req


requirement_chain = RunnableLambda(_parse)


# ------------------------------------------------------------------------------ LLM layer

class RequirementExtraction(BaseModel):
    device_type: Literal["desktop", "laptop", "compare"] | None = Field(
        None, description="'compare' when the user is undecided between a desktop and a laptop")
    budget_sgd: float | None = Field(None, description="maximum budget in Singapore dollars, only if stated")
    workloads: list[str] = Field(default_factory=list, description="software or activities, e.g. gaming, SolidWorks")
    minimum_memory_gb: int | None = None
    minimum_storage_gb: int | None = None
    preferences: list[str] = Field(default_factory=list, description="soft wishes, e.g. quiet, portable, white, RGB")
    must_buy_components: list[str] = Field(default_factory=list, description="specific parts the user insists on buying")
    owned_components: list[str] = Field(default_factory=list, description="parts the user already has and will reuse")


EXTRACTION_PROMPT = (
    "You convert a computer shopper's latest message into structured requirements. Extract only what the "
    "message states; use null or an empty list for anything not mentioned. Do not infer a budget, memory size "
    "or device type that is not written. 'must_buy_components' are specific products the user wants included "
    "in the purchase; 'owned_components' are parts the user says they already have. Copy component names "
    "as written by the user. If previously_recommended_parts is given, a request to keep or lock one of those "
    "parts means it stays in the purchase (must_buy_components), not that the user owns it. The message may be "
    "in English or Chinese."
)


# Capacities, small numbers and category words do not identify a product ("64GB RAM", "2 TB SSD").
GENERIC_TOKEN = re.compile(r"\d{1,2}|\d+(?:gb|tb|w|mhz|mt|hz|mm|cm)|gb|tb|ddr\d|gddr\d|ram|memory|ssd|nvme|hdd|storage|drive")
OWNERSHIP = re.compile(r"already (?:have|own|got|has)|\bi (?:have|own)\b|i've got|\bre-?use\b|\bexisting\b|"
                       r"\bmy (?:old|current|own)\b|已有|已经有|我有|现有|旧的", re.I)


def _specific_mention(mention: str, text: str) -> bool:
    """A component mention must name a concrete model and come from the message itself, so a model cannot turn
    "I need a computer" or a capacity such as "64GB RAM" into a locked product."""
    tokens = re.findall(r"[a-z0-9]+", mention.casefold())
    if not any(re.search(r"\d", t) and not GENERIC_TOKEN.fullmatch(t) for t in tokens):
        return False
    squashed = re.sub(r"\s+", "", text.casefold())
    return all(t in squashed for t in tokens)


def _owned_mention(mention: str, text: str) -> bool:
    """An owned part needs no model number ("I already have a 2TB SSD"), but it must come from the message
    and name a recognisable component type."""
    tokens = re.findall(r"[a-z0-9]+", mention.casefold())
    squashed = re.sub(r"\s+", "", text.casefold())
    return bool(tokens) and all(t in squashed for t in tokens) and _category_of(mention, []) is not None


def _number_in_text(number: float | int | None, text: str) -> bool:
    if number is None:
        return False
    plain = re.sub(r"(?<=\d),(?=\d)", "", text)
    return re.search(rf"(?<!\d){int(number)}(?!\d)", plain) is not None


def _match_catalogue(mention: str, catalogue) -> list[dict]:
    """Offers whose title contains every meaningful token of the mention."""
    if not catalogue or not mention.strip():
        return []
    stop = {"the", "a", "an", "my", "i", "nvidia", "geforce", "amd", "intel", "card", "graphics", "gpu", "cpu"}
    tokens = [t for t in re.findall(r"[a-z0-9]+", mention.casefold()) if t not in stop]
    if not tokens:
        return []
    hits = []
    for row in getattr(catalogue, "prices", []):
        name = re.sub(r"[™®©]", "", row.get("name", "")).casefold()
        compact = re.sub(r"\s+", "", name)
        if all(t in name or t in compact for t in tokens):
            hits.append(row)
    return hits


def _category_of(mention: str, hits: list[dict]) -> str | None:
    if hits:
        counts: dict[str, int] = {}
        for row in hits:
            counts[row["category"]] = counts.get(row["category"], 0) + 1
        return max(counts, key=counts.get)
    lower = mention.casefold()
    for pattern, category in ((r"rtx|gtx|radeon|\brx\s?\d", "gpu"), (r"ryzen|core\s?i\d|core ultra", "cpu"),
                              (r"ddr\d|ram|memory", "ram"), (r"ssd|nvme", "ssd"), (r"psu|power supply|\d+\s?w\b", "psu"),
                              (r"motherboard|\b[abxhz]\d{3}\b", "motherboard"), (r"case|chassis", "case"),
                              (r"monitor", "monitor")):
        if re.search(pattern, lower):
            return category
    return None


def _owned_component(mention: str, catalogue) -> dict:
    from backend.ingest.specs import extract_offer
    hits = _match_catalogue(mention, catalogue)
    category = _category_of(mention, hits)
    record = {"mention": mention, "category": category, "specs": {}}
    if category:
        extracted = extract_offer({"id": "owned", "category": category, "name": mention, "variant": "", "description": ""})
        if extracted:
            record["specs"] = {k: v["value"] for k, v in extracted["specs"].items() if v}
    return record


def _recent_match(mention: str, recent_items: list[dict]) -> dict | None:
    """The previously recommended part a mention refers to, if exactly one fits."""
    tokens = [t for t in re.findall(r"[a-z0-9]+", mention.casefold()) if len(t) > 1]
    hits = {i["product_id"]: i for i in recent_items
            if tokens and all(t in re.sub(r"[™®©]", "", i["name"]).casefold() for t in tokens)}
    return next(iter(hits.values())) if len(hits) == 1 else None


def _apply_llm(req: dict, text: str, model, catalogue, questions: list[dict], regex_budget_set: bool,
               recent_items: list[dict]) -> None:
    payload = {"message": text}
    if recent_items:
        payload["previously_recommended_parts"] = [i["name"] for i in recent_items[:12]]
    extracted: RequirementExtraction = model.structured(EXTRACTION_PROMPT, payload, RequirementExtraction)
    # "Keep the <part you recommended>" means keep buying it, not that the user already owns it.
    moved = [m for m in extracted.owned_components if _recent_match(m, recent_items)]
    extracted.owned_components = [m for m in extracted.owned_components if m not in moved]
    extracted.must_buy_components = extracted.must_buy_components + moved
    if extracted.device_type and not req.get("device_type"):
        req["device_type"] = extracted.device_type
    if extracted.budget_sgd is not None:
        llm_minor = int(round(extracted.budget_sgd * 100))
        if regex_budget_set and abs(llm_minor - req["budget"]["maximum_minor"]) > 100:
            questions.append({"question_id": "q_budget_confirm",
                              "text": f"Is your maximum budget S${req['budget']['maximum_minor'] / 100:,.0f} or S${extracted.budget_sgd:,.0f}?",
                              "options": [{"label": f"S${v:,.0f}", "value": f"My maximum budget is S${v:,.0f}."}
                                          for v in (req["budget"]["maximum_minor"] / 100, extracted.budget_sgd)]})
        elif not regex_budget_set and _number_in_text(extracted.budget_sgd, text):
            req["budget"] = {"currency": "SGD", "maximum_minor": llm_minor, "is_hard_limit": True}
    constraints = req.setdefault("hard_constraints", {})
    for field in ("minimum_memory_gb", "minimum_storage_gb"):
        value = getattr(extracted, field)
        if not value or field in constraints:
            continue
        stated = _number_in_text(value, text) or (
            field == "minimum_storage_gb" and value % 1024 == 0 and _number_in_text(value // 1024, text))
        if stated:
            constraints[field] = value
    seen = {w.casefold() for w in req["workloads"]}
    req["workloads"] += [w for w in extracted.workloads if w.casefold() not in seen]
    seen = {p.casefold() for p in req["preferences"]}
    req["preferences"] += [p for p in extracted.preferences if p.casefold() not in seen]

    if not OWNERSHIP.search(text):          # "a PC with 128GB RAM" is a requirement, not a part the user owns
        extracted.owned_components = []
    for mention in filter(lambda m: _owned_mention(m, text), extracted.owned_components):
        if not any(o["mention"].casefold() == mention.casefold() for o in req["owned_components"]):
            req["owned_components"].append(_owned_component(mention, catalogue))
    owned = {o["mention"].casefold() for o in req["owned_components"]}
    must_buy = [m for m in extracted.must_buy_components if _specific_mention(m, text) and m.casefold() not in owned]
    for i, mention in enumerate(must_buy):
        recent = _recent_match(mention, recent_items)
        if recent:        # the user means the part we just recommended: lock exactly that product
            if recent["product_id"] not in req["locked_product_ids"]:
                req["locked_product_ids"].append(recent["product_id"])
                req["locked_items"].append({"mention": mention, **recent})
            continue
        hits = _match_catalogue(mention, catalogue)
        products = {str(r.get("product_id") or r["id"]): r for r in hits}
        if len(products) == 1:
            pid, row = next(iter(products.items()))
            if pid not in req["locked_product_ids"]:
                req["locked_product_ids"].append(pid)
                req["locked_items"].append({"mention": mention, "product_id": pid, "name": row["name"], "category": row["category"]})
        elif len(products) > 1:
            choices = sorted(products.values(), key=lambda r: float(r["price"]))[:4]
            questions.append({"question_id": f"q_locked_{i}",
                              "text": f"Several listings match \"{mention}\". Which one do you want?",
                              "options": [{"label": f"{r['name'][:60]} (S${float(r['price']):,.0f})",
                                           "value": f"I want to buy the {r['name']}."} for r in choices]})
        else:
            req.setdefault("unresolved_components", []).append(mention)


# ------------------------------------------------------------------------------ public API

def clarification_questions(req: dict) -> list[dict]:
    questions = []
    if not req.get("device_type"):
        questions.append({
            "question_id": "q_device_type",
            "text": "Would you like a desktop PC, a laptop, or a comparison of both?",
            "options": [
                {"label": "Desktop PC", "value": "I want a desktop PC."},
                {"label": "Laptop", "value": "I want a laptop."},
                {"label": "Compare both", "value": "I am not sure whether to buy a desktop or laptop, please compare."},
            ],
        })
    if not req.get("budget"):
        questions.append({
            "question_id": "q_budget",
            "text": "What is your maximum budget in Singapore dollars?",
            "options": [{"label": f"S${v:,}", "value": f"My maximum budget is S${v:,}."} for v in (1200, 1800, 2500, 3500)],
        })
    elif req.get("device_type") in MINIMUM_FEASIBLE_MINOR and \
            req["budget"]["maximum_minor"] < MINIMUM_FEASIBLE_MINOR[req["device_type"]] and not req.get("budget_low_acknowledged"):
        floor = MINIMUM_FEASIBLE_MINOR[req["device_type"]] / 100
        questions.append({
            "question_id": "q_budget_low",
            "text": f"S${req['budget']['maximum_minor'] / 100:,.0f} is below what a complete {req['device_type']} usually costs "
                    f"in the current Singapore catalogue (about S${floor:,.0f}). Would you like to raise the budget?",
            "options": [{"label": f"Raise to S${floor:,.0f}", "value": f"My maximum budget is S${floor:,.0f}."},
                        {"label": "Keep my budget", "value": "Keep my budget and show what is possible."}],
        })
    return questions


def parse_requirements(text: str, current: dict, model=None, catalogue=None,
                       recent_items: list[dict] | None = None) -> tuple[dict, list[dict]]:
    before = deepcopy(current or {})
    req = requirement_chain.invoke({"text": text, "current": current})
    if "keep my budget" in text.casefold():
        req["budget_low_acknowledged"] = True
    questions: list[dict] = []
    regex_budget_set = bool(req.get("budget")) and req.get("budget") != before.get("budget")
    req["understanding_source"] = "rules"
    if text.strip() and model is not None and getattr(model, "enabled", False):
        try:
            _apply_llm(req, text, model, catalogue, questions, regex_budget_set, recent_items or [])
            req["understanding_source"] = "llm+rules"
        except Exception as exc:  # model down or malformed output: the rule layer result stands
            req["understanding_source"] = f"rules (model unavailable: {type(exc).__name__})"
    known = {o.get("category") for o in req.get("owned_components", [])}
    for mention in _owned_by_rule(text):     # deterministic backstop when the model misses an owned part
        record = _owned_component(mention, catalogue)
        if record["category"] and record["category"] not in known:
            req.setdefault("owned_components", []).append(record)
            known.add(record["category"])
    return req, questions + clarification_questions(req)


OWNED_PHRASE = re.compile(r"(?:already (?:have|own|got)|\bi (?:have|own)|i've got|已经有|已有|我有)\s*"
                          r"(?:an?\s+|one\s+|my\s+|一张|一块|一个|一条|一套)?([^,.;!?，。；]+)", re.I)


def _owned_by_rule(text: str) -> list[str]:
    """Parts introduced by an ownership phrase ("I already have a 2TB SSD, reuse it")."""
    out = []
    for match in OWNED_PHRASE.finditer(text):
        clause = re.split(r"\b(?:but|so|that|which|to|for)\b|，|帮|请", match.group(1), maxsplit=1)[0]
        for part in re.split(r"\band\b|和", clause):
            mention = re.sub(r"^\s*(?:an?|one|my)\s+", "", part.strip(), flags=re.I)
            if mention and _category_of(mention, []) is not None:
                out.append(mention)
    return out


def for_model(req: dict) -> dict:
    """A readable view of the requirements for prompts: money in Singapore dollars, no internal fields."""
    budget = (req.get("budget") or {}).get("maximum_minor")
    view = {"device_type": req.get("device_type"),
            "budget_sgd_max": budget / 100 if budget is not None else None,
            "workloads": req.get("workloads", []), "preferences": req.get("preferences", []),
            "hard_constraints": req.get("hard_constraints", {}),
            "locked_parts": [i.get("name") or i.get("mention") for i in req.get("locked_items", [])],
            "owned_parts": [o.get("mention") for o in req.get("owned_components", [])]}
    return {k: v for k, v in view.items() if v not in (None, [], {})}
