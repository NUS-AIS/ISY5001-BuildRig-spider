"""Compatibility-aware component selection and targeted replanning (FR05, FR06).

Selection follows the physical dependency chain of a desktop build:

    CPU -> motherboard (same socket) -> RAM (same memory generation) -> GPU
        -> PSU (enough wattage for CPU + GPU) -> case (fits board size and card length) -> SSD -> cooler

For every category the catalogue tool returns a short list of compatible, in-stock offers near
the category's budget share. The language model then chooses from that list for the user's
workloads and preferences; its choice is only accepted if it is one of the listed offers. Without
a model (or if it fails) a deterministic choice is used, so the pipeline never depends on the
model being available.

Replanning changes only the categories a failed check points at, keeps every other part, never
touches locked or owned parts, and excludes offers that were already tried.
"""
from __future__ import annotations

import re

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

from pydantic import BaseModel

DESKTOP_ORDER = ["cpu", "motherboard", "ram", "gpu", "psu", "case", "ssd", "cooler"]
FORM_FACTOR_RANK = {"Mini-ITX": 1, "Micro-ATX": 2, "ATX": 3, "E-ATX": 4}

PROFILES = {
    "gaming": {"gpu": .40, "cpu": .17, "motherboard": .10, "ram": .07, "ssd": .07, "psu": .07, "case": .07, "cooler": .05},
    "workstation": {"cpu": .25, "gpu": .30, "motherboard": .11, "ram": .10, "ssd": .08, "psu": .07, "case": .05, "cooler": .04},
    "ai": {"gpu": .45, "cpu": .15, "motherboard": .09, "ram": .10, "ssd": .08, "psu": .07, "case": .03, "cooler": .03},
    "general": {"cpu": .22, "gpu": .25, "motherboard": .13, "ram": .09, "ssd": .10, "psu": .08, "case": .08, "cooler": .05},
}
WORKSTATION_WORDS = ("solidworks", "ansys", "matlab", "autocad", "blender", "video", "premiere", "3d", "剪辑", "render")
AI_WORDS = ("machine learning", "deep learning", "ai", "llm", "pytorch", "training")
GAMING_WORDS = ("gaming", "game", "游戏")


def profile_for(requirements: dict) -> str:
    text = " ".join(requirements.get("workloads", [])).casefold()
    if any(w in text for w in AI_WORDS):
        return "ai"
    if any(w in text for w in WORKSTATION_WORDS):
        return "workstation"
    if any(w in text for w in GAMING_WORDS):
        return "gaming"
    return "general"


def minor(row: dict) -> int:
    return int(Decimal(str(row["price"])) * 100)


def make_item(row: dict, locked: bool = False) -> dict:
    return {"category": row["category"], "product_id": str(row.get("product_id") or row["id"]), "offer_id": row["id"],
            "name": row["name"], "quantity": 1, "price": row["price"], "currency": row.get("currency", "SGD"),
            "merchant": row.get("store"), "source_url": row.get("source_url"), "collected_at": row.get("collected_at"),
            "availability": "in_stock_at_collection" if row.get("available") else "unavailable",
            "specs": row.get("specs", {}), "flags": row.get("flags", {}), "locked": locked}


def owned_item(owned: dict) -> dict:
    return {"category": owned["category"], "product_id": f"owned:{owned['mention']}", "offer_id": None,
            "name": f"{owned['mention']} (already owned)", "quantity": 1, "price": "0", "currency": "SGD",
            "merchant": None, "source_url": None, "collected_at": None, "availability": None,
            "specs": owned.get("specs", {}), "flags": {}, "locked": True, "owned_by_user": True}


# ---------------------------------------------------------------------------------- choosing

class Pick(BaseModel):
    category: str
    offer_id: str
    reason: str


class Picks(BaseModel):
    picks: list[Pick]


CHOOSER_PROMPT = (
    "You are a PC build specialist in Singapore. For every category, choose exactly one offer_id from that "
    "category's shortlist. Prefer what best serves the user's workloads and stated preferences, and keep "
    "the combined price within remaining_budget_sgd. Only use offer_ids that appear in the shortlists. "
    "Give a one-sentence reason per pick that refers only to the listed name, price and specs."
)


@dataclass
class Chooser:
    """Picks one offer per category from shortlists, with the model if available."""
    model: Any = None
    log: list[dict] = field(default_factory=list)

    def choose(self, shortlists: dict[str, list[dict]], requirements: dict, remaining_minor: int) -> dict[str, tuple[dict, str, str]]:
        shortlists = {c: rows for c, rows in shortlists.items() if rows}
        chosen = {c: (rows[0], "Highest-priced compatible offer within this part's budget share.", "rules")
                  for c, rows in shortlists.items()}
        if not shortlists or not (self.model and getattr(self.model, "enabled", False)):
            return chosen
        payload = {"workloads": requirements.get("workloads", []), "preferences": requirements.get("preferences", []),
                   "remaining_budget_sgd": remaining_minor / 100,
                   "shortlists": {c: [{"offer_id": r["id"], "name": r["name"][:90], "price_sgd": float(r["price"]),
                                       "specs": {k: v for k, v in r.get("specs", {}).items() if v is not None}}
                                      for r in rows] for c, rows in shortlists.items()}}
        try:
            picks = self.model.structured(CHOOSER_PROMPT, payload, Picks).picks
        except Exception as exc:
            self.log.append({"event": "chooser_fallback", "error": type(exc).__name__})
            return chosen
        for pick in picks:
            row = next((r for r in shortlists.get(pick.category, []) if r["id"] == pick.offer_id), None)
            if row:      # the model may only choose from what the tools returned
                chosen[pick.category] = (row, pick.reason, "llm")
            else:
                self.log.append({"event": "rejected_model_pick", "category": pick.category, "offer_id": pick.offer_id})
        return chosen


# ---------------------------------------------------------------------------------- shortlists

def required_psu_watts(items: dict[str, dict]) -> int | None:
    cpu_w = (items.get("cpu") or {}).get("specs", {}).get("tdp_w")
    gpu = items.get("gpu")
    gpu_w = gpu["specs"].get("tdp_w") if gpu else 0
    if cpu_w is None or gpu_w is None:
        return None
    return int((cpu_w + gpu_w + 100) * 1.2)


# Listings filed under a component category that are really accessories for it. A real component's name
# describes itself first ("... Mid-Tower Case - Tempered Glass Side Panel"), so the accessory word must lead.
ACCESSORY = {
    "case": re.compile(r"^(?:(?!\bcase\b|\bchassis\b|tower).)*\b(?:vertical (?:base|gpu kit)|front panel|"
                       r"side panel|riser|bracket|dust filter|stand)\b", re.I),
    "ssd": re.compile(r"\bheatsink\b|\benclosure\b|\badapter\b", re.I),
    "gpu": re.compile(r"\b(?:riser|bracket|support|holder|backplate)\b", re.I),
}
PRIMARY_SPEC = {"case": "max_form_factor", "ssd": "capacity_gb", "gpu": "vram_gb"}


def is_accessory(row: dict) -> bool:
    """True for an accessory listed under a component category (a case base, an SSD heatsink): its name
    reads as an accessory and it lacks the category's primary specification."""
    pattern, key = ACCESSORY.get(row.get("category")), PRIMARY_SPEC.get(row.get("category"))
    return bool(pattern and pattern.search(row.get("name", ""))) and row.get("specs", {}).get(key) is None


def shortlist(query: Callable, category: str, share_minor: int, items: dict[str, dict], requirements: dict,
              exclude: list[str], limit: int = 5, hard_max: int | None = None,
              extra_require: dict | None = None) -> list[dict]:
    """Compatible in-stock offers for one category, nearest to (but not far above) its budget share.
    ``hard_max`` caps every price window (used when replanning to fix an overspend)."""
    require, minimum = dict(extra_require or {}), {}
    cpu, board, gpu = items.get("cpu"), items.get("motherboard"), items.get("gpu")
    constraints = requirements.get("hard_constraints") or {}
    if category == "motherboard" and cpu and cpu["specs"].get("socket"):
        require["socket"] = cpu["specs"]["socket"]
    if category == "cpu" and board and board["specs"].get("socket"):
        require["socket"] = board["specs"]["socket"]
    if category == "ram":
        if board and board["specs"].get("memory_type"):
            require["memory_type"] = board["specs"]["memory_type"]
        minimum["capacity_gb"] = constraints.get("minimum_memory_gb") or 16
    if category == "psu" and required_psu_watts(items):
        minimum["wattage_w"] = required_psu_watts(items)
    if category == "case" and gpu and gpu["specs"].get("length_mm"):
        minimum["max_gpu_length_mm"] = gpu["specs"]["length_mm"]
    if category == "ssd":
        minimum["capacity_gb"] = constraints.get("minimum_storage_gb") or 500
    rows = []
    # Stay inside the share first; only then allow a small overshoot; finally take the cheapest match.
    for low, high in ((0.35, 1.0), (0.0, 1.15), (0.0, None)):
        if high and share_minor <= 0:        # an empty price window cannot match anything
            continue
        ceiling = int(share_minor * high) if high else None
        if hard_max is not None:
            ceiling = min(ceiling, hard_max) if ceiling is not None else hard_max
        rows = query(category=category, maximum_minor=ceiling,
                     minimum_minor=int(share_minor * low) if low else None, require=require or None,
                     minimum_specs=minimum or None, exclude_ids=exclude, order="price_desc" if high else "price_asc",
                     limit=limit * 3)
        rows = [r for r in rows if not is_accessory(r)]
        if category == "case" and board and board["specs"].get("form_factor"):
            need = FORM_FACTOR_RANK[board["specs"]["form_factor"]]
            rows = [r for r in rows if FORM_FACTOR_RANK.get(r["specs"].get("max_form_factor"), 0) >= need
                    or r["specs"].get("max_form_factor") is None]
        if rows:
            break
    known_first = sorted(rows, key=lambda r: any(r["specs"].get(k) is None for k in list(require) + list(minimum)))
    return known_first[:limit]


# ---------------------------------------------------------------------------------- desktop

def plan_desktop(query: Callable, products: Callable, requirements: dict, chooser: Chooser,
                 profile: str | None = None, scale: float = 1.0) -> dict:
    budget = int(requirements["budget"]["maximum_minor"] * scale)
    items: dict[str, dict] = {}
    reasons: dict[str, str] = {}
    sources: dict[str, str] = {}
    for owned in requirements.get("owned_components", []):
        if owned.get("category") in DESKTOP_ORDER:
            items[owned["category"]] = owned_item(owned)
            reasons[owned["category"]] = "Already owned by the user; reused at no cost."
    for row in products(requirements.get("locked_product_ids", [])):
        if row["category"] in DESKTOP_ORDER and row["category"] not in items:
            items[row["category"]] = make_item(row, locked=True)
            reasons[row["category"]] = "Locked by the user."
    profile = profile or profile_for(requirements)
    shares = PROFILES[profile]
    # Everyday use does not need a graphics card when the CPU has integrated graphics.
    integrated = profile == "general" and "gpu" not in items
    spent = sum(minor(i) for i in items.values() if not i.get("owned_by_user"))
    free = max(budget - spent, 0)
    notes = []
    if not integrated and "gpu" not in items:
        # If even the cheapest in-stock card plus the cheapest other parts cannot fit, a graphics card is
        # impossible within this budget; build on integrated graphics and say so instead of failing.
        floor = cheapest_build_minor(query, requirements, items, DESKTOP_ORDER)
        if floor is not None and floor > free:
            integrated = True
            notes.append(f"No build with a graphics card fits this budget: the cheapest compatible one costs "
                         f"S${floor / 100:,.0f}. This build uses the processor's integrated graphics, which suits "
                         f"light or older games only.")
    required = [c for c in DESKTOP_ORDER if not (integrated and c == "gpu")]
    open_cats = [c for c in required if c not in items]
    weight = sum(shares[c] for c in open_cats) or 1
    share = {c: int(free * shares[c] / weight) for c in open_cats}

    # Stage 1: the parts that define performance; stage 2: everything that must fit around them.
    for stage in (["cpu", "gpu"], ["motherboard", "ram", "psu", "case", "ssd", "cooler"]):
        # Re-split what is actually left, so savings or overspend in stage 1 carry into stage 2.
        pending = [c for c in open_cats if c not in items]
        left = free - sum(minor(items[c]) for c in open_cats if c in items)
        pending_weight = sum(shares[c] for c in pending) or 1
        share.update({c: max(int(left * shares[c] / pending_weight), 0) for c in pending})
        lists = {}
        for category in [c for c in stage if c in open_cats]:
            extra = {"integrated_graphics": True} if integrated and category == "cpu" else None
            lists[category] = shortlist(query, category, share[category], items, requirements, [], extra_require=extra)
        remaining = free - sum(minor(items[c]) for c in open_cats if c in items)
        for category, (row, reason, source) in chooser.choose(lists, requirements, remaining).items():
            items[category] = make_item(row)
            reasons[category], sources[category] = reason, source
        # Board-dependent parts must be re-checked once the board is known (RAM generation, case size).
        if stage[0] == "motherboard" and "motherboard" in items and "ram" in items and not items["ram"].get("locked"):
            board_mem = items["motherboard"]["specs"].get("memory_type")
            if board_mem and items["ram"]["specs"].get("memory_type") not in (None, board_mem):
                fixed = shortlist(query, "ram", share.get("ram", 0), items, requirements, [items["ram"]["offer_id"]])
                if fixed:
                    items["ram"] = make_item(fixed[0])
                    reasons["ram"], sources["ram"] = "Matched to the motherboard's memory generation.", "rules"
    ordered = [items[c] for c in DESKTOP_ORDER if c in items]
    for item in ordered:
        item["selection_reason"] = reasons.get(item["category"], "")
        item["selected_by"] = sources.get(item["category"], "user")
    return {"device_type": "desktop", "profile": profile, "items": ordered, "required_categories": required,
            "integrated_graphics_build": integrated, "shares_minor": share, "planning_notes": notes}


def cheapest_build_minor(query: Callable, requirements: dict, fixed: dict, categories: list[str],
                         extra_require: dict | None = None) -> int | None:
    """Price of the cheapest compatible build: each open category takes its cheapest compatible offer,
    following the same compatibility chain as planning. None if some category has no offer."""
    items = dict(fixed)
    for category in categories:
        if category in items:
            continue
        rows = shortlist(query, category, 0, items, requirements, [],
                         extra_require=extra_require if category == "cpu" else None)
        if not rows:
            return None
        items[category] = make_item(min(rows, key=minor))
    return sum(minor(i) for c, i in items.items() if c not in fixed)


# ---------------------------------------------------------------------------------- laptop

class LaptopRanking(BaseModel):
    ranked_offer_ids: list[str]
    reasons: dict[str, str]


def plan_laptops(query: Callable, requirements: dict, chooser: Chooser, maximum_options: int) -> list[dict]:
    constraints = requirements.get("hard_constraints") or {}
    minimum = {k: v for k, v in (("ram_gb", constraints.get("minimum_memory_gb")),
                                 ("storage_gb", constraints.get("minimum_storage_gb"))) if v}
    rows = query(category="laptop", maximum_minor=requirements["budget"]["maximum_minor"], minimum_specs=minimum or None,
                 order="price_desc", limit=10)
    order, reasons, source = [r["id"] for r in rows], {}, "rules"
    model = chooser.model
    if rows and model and getattr(model, "enabled", False):
        payload = {"workloads": requirements.get("workloads", []), "preferences": requirements.get("preferences", []),
                   "budget_sgd": requirements["budget"]["maximum_minor"] / 100,
                   "laptops": [{"offer_id": r["id"], "name": r["name"][:110], "price_sgd": float(r["price"]),
                                "specs": {k: v for k, v in r.get("specs", {}).items() if v is not None}} for r in rows]}
        try:
            ranking = model.structured(
                "Rank these in-stock Singapore laptops for the user's workloads and preferences, best first. "
                "Only use the given offer_ids. Give a one-sentence reason per laptop based only on the listed "
                "name, price and specs.", payload, LaptopRanking)
            valid = [oid for oid in ranking.ranked_offer_ids if oid in order]
            if valid:
                order = valid + [oid for oid in order if oid not in valid]
                reasons, source = ranking.reasons, "llm"
        except Exception as exc:
            chooser.log.append({"event": "laptop_ranking_fallback", "error": type(exc).__name__})
    by_id = {r["id"]: r for r in rows}
    options = []
    for oid in order[:maximum_options]:
        item = make_item(by_id[oid])
        item["selection_reason"] = reasons.get(oid, "Highest-specified in-stock laptop within the budget.")
        item["selected_by"] = source
        options.append({"device_type": "laptop", "profile": "laptop", "items": [item], "required_categories": ["laptop"]})
    return options


# ---------------------------------------------------------------------------------- replanning

def revise(option: dict, categories: list[str], validation: dict, query: Callable, requirements: dict,
           chooser: Chooser) -> tuple[dict, list[dict]]:
    """Return a new option where only ``categories`` are re-selected, plus a change log."""
    items = {i["category"]: i for i in option["items"]}
    tried = {c: list(v) for c, v in option.get("tried_offer_ids", {}).items()}
    changeable = [c for c in DESKTOP_ORDER + ["laptop"] if c in categories and c in items and not items[c].get("locked")]
    if not changeable:
        return option, []
    budget = requirements["budget"]["maximum_minor"]
    over = max(validation.get("total_minor", 0) - budget, 0)
    shares = PROFILES.get(option.get("profile"), PROFILES["general"])
    kept_cost = sum(minor(i) for c, i in items.items() if c not in changeable and not i.get("owned_by_user"))
    free = max(budget - kept_cost, 0)
    weight = sum(shares.get(c, 1) for c in changeable) or 1
    changes = []

    def replace_part(category: str, share: int, hard_max: int | None) -> bool:
        old = items.pop(category)
        tried.setdefault(category, []).append(old["offer_id"])
        extra = {"integrated_graphics": True} if option.get("integrated_graphics_build") and category == "cpu" else None
        lists = {category: shortlist(query, category, max(share, 0), items, requirements, tried[category],
                                     hard_max=hard_max, extra_require=extra)}
        picked = chooser.choose(lists, requirements, free).get(category)
        if not picked or (hard_max is not None and minor(picked[0]) > hard_max):
            items[category] = old
            return False
        row, reason, source = picked
        items[category] = make_item(row)
        items[category]["selection_reason"], items[category]["selected_by"] = reason, source
        changes.append({"category": category, "from": old["name"], "to": row["name"],
                        "from_price": old["price"], "to_price": row["price"]})
        return True

    if over and "budget_limit" in validation.get("failed_codes", []):
        # Fix an overspend with strictly cheaper parts. First look for one replacement that closes the whole
        # gap (the part the check named, then the most expensive others); if none exists, take the largest
        # saving available this round and let the next round continue. Never pick something pricier.
        others = sorted((c for c, i in items.items()
                         if not i.get("locked") and not i.get("owned_by_user") and c not in changeable),
                        key=lambda c: minor(items[c]), reverse=True)
        candidates = [c for c in changeable if not items[c].get("owned_by_user")] + others
        fixed = False
        for category in candidates:
            old_minor = minor(items[category])
            if old_minor - over > 0 and replace_part(category, old_minor - over, hard_max=old_minor - over):
                fixed = True
                break
        if not fixed:
            # No single part closes the gap: within this round, keep replacing the part whose cheapest
            # compatible alternative saves most until the total fits or nothing cheaper is left.
            def best_saving(category: str) -> int:
                others_now = {c: i for c, i in items.items() if c != category}
                extra = {"integrated_graphics": True} if option.get("integrated_graphics_build") and category == "cpu" else None
                rows = shortlist(query, category, 0, others_now, requirements,
                                 tried.get(category, []) + [items[category]["offer_id"]], hard_max=minor(items[category]) - 1,
                                 extra_require=extra)
                return minor(items[category]) - min((minor(r) for r in rows), default=minor(items[category]))
            savings = {c: best_saving(c) for c in candidates}      # one query per part, computed once
            remaining_over = over
            ranked = sorted((c for c in candidates if savings[c] > 0), key=savings.get, reverse=True)
            closing = [c for c in ranked if savings[c] >= remaining_over]
            # Prefer the smallest single saving that closes the gap; otherwise take the largest savings in turn.
            for category in sorted(closing, key=savings.get) + [c for c in ranked if c not in closing]:
                if remaining_over <= 0:
                    break
                old_minor = minor(items[category])
                cap = old_minor - remaining_over if savings[category] >= remaining_over else old_minor - 1
                if replace_part(category, 0 if cap == old_minor - 1 else cap, hard_max=cap):
                    remaining_over -= old_minor - minor(items[category])
    else:
        for category in changeable:
            if not replace_part(category, int(free * shares.get(category, 1) / weight), hard_max=None):
                changes.append({"category": category, "from": items[category]["name"], "to": None,
                                "note": "no compatible alternative available"})
    new = {**option, "items": [items[c] for c in DESKTOP_ORDER + ["laptop"] if c in items], "tried_offer_ids": tried}
    return new, changes


def assemble_option(corpus, device_type: str, offer_ids: list[str], requirements: dict) -> dict:
    """Build an option on the server from offer ids (plus owned parts) so no runtime can invent items."""
    rows = [corpus.offer(oid) for oid in offer_ids]
    if any(r is None for r in rows):
        raise KeyError([oid for oid, r in zip(offer_ids, rows) if r is None])
    locked = {str(p) for p in requirements.get("locked_product_ids", [])}
    items = [make_item(r, locked=str(r.get("product_id") or r["id"]) in locked) for r in rows]
    if device_type == "desktop":
        have = {i["category"] for i in items}
        items += [owned_item(o) for o in requirements.get("owned_components", [])
                  if o.get("category") in DESKTOP_ORDER and o["category"] not in have]
    integrated = device_type == "desktop" and not any(i["category"] == "gpu" for i in items)
    required = ["laptop"] if device_type == "laptop" else [c for c in DESKTOP_ORDER if not (integrated and c == "gpu")]
    order = DESKTOP_ORDER + ["laptop"]
    items.sort(key=lambda i: order.index(i["category"]) if i["category"] in order else len(order))
    return {"device_type": device_type, "items": items, "required_categories": required,
            "integrated_graphics_build": integrated}
