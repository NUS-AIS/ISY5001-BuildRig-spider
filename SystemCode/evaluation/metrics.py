"""Metric helpers shared by the experiments."""
from __future__ import annotations

import math
import re
from decimal import Decimal

from backend.validation import validate_option


# ------------------------------------------------------------------------------ retrieval

def _text(row: dict) -> str:
    return re.sub(r"\s+", " ", f"{row.get('name', '')} {row.get('description', '')} {row.get('product_name', '')} {row.get('text', '')}").casefold()


def offer_relevant(row: dict, specs: dict, rule: dict) -> bool:
    if row["category"] not in rule.get("category", [row["category"]]):
        return False
    text = _text(row)
    if any(t not in text for t in rule.get("all", [])):
        return False
    if rule.get("any") and not any(t in text for t in rule["any"]):
        return False
    if any(t in text for t in rule.get("none", [])):
        return False
    return all(specs.get(k) == v for k, v in rule.get("spec", {}).items())


def review_relevant(review: dict, rule: dict) -> bool:
    if rule.get("spec") or review.get("category") not in rule.get("category", []):
        return False
    name = (review.get("product_name") or "").casefold()
    return bool(rule.get("all")) and all(t in name for t in rule["all"])


def precision_at_k(flags: list[bool], k: int) -> float:
    return sum(flags[:k]) / k


def recall_at_k(flags: list[bool], k: int, total_relevant: int) -> float:
    """Capped recall: relevant items found in the top k over the most that could fit in k."""
    return sum(flags[:k]) / min(total_relevant, k) if total_relevant else 0.0


def ndcg_at_k(flags: list[bool], k: int, total_relevant: int) -> float:
    dcg = sum(1 / math.log2(i + 2) for i, rel in enumerate(flags[:k]) if rel)
    ideal = sum(1 / math.log2(i + 2) for i in range(min(total_relevant, k)))
    return dcg / ideal if ideal else 0.0


# ------------------------------------------------------------------------------ recommendations

def option_constraints_ok(option: dict, expect: dict, requirements: dict) -> dict:
    """Hard constraints of one returned option, checked independently of the system's own verdict."""
    total = sum(Decimal(str(i["price"])) for i in option["items"] if not i.get("owned_by_user"))
    out = {"within_budget": expect.get("budget_sgd") is None or total <= Decimal(expect["budget_sgd"])}
    if expect.get("min_memory_gb"):
        item = next((i for i in option["items"] if i["category"] in ("ram", "laptop")), None)
        size = (item or {}).get("specs", {}).get("capacity_gb" if (item or {}).get("category") == "ram" else "ram_gb")
        out["memory"] = size is not None and size >= expect["min_memory_gb"]
    if expect.get("min_storage_gb"):
        item = next((i for i in option["items"] if i["category"] in ("ssd", "laptop")), None)
        size = (item or {}).get("specs", {}).get("capacity_gb" if (item or {}).get("category") == "ssd" else "storage_gb")
        out["storage"] = size is not None and size >= expect["min_storage_gb"] * 0.95
    if expect.get("locked_name"):
        squash = lambda s: re.sub(r"[\s\-™®]", "", s).casefold()
        out["locked_kept"] = any(squash(expect["locked_name"]) in squash(i["name"]) for i in option["items"])
    if expect.get("owned_category"):
        out["owned_reused"] = any(i.get("owned_by_user") and i["category"] == expect["owned_category"] for i in option["items"])
    recheck = validate_option(option, requirements)
    out["no_failed_check"] = recheck["overall_status"] != "failed"
    return out


def known_conflict(option: dict) -> bool:
    """Independent, name-based sanity check: an Intel CPU on an AMD chipset (or the reverse)."""
    cpu = next((i["name"].upper() for i in option["items"] if i["category"] == "cpu"), "")
    board = next((i["name"].upper() for i in option["items"] if i["category"] == "motherboard"), "")
    if not cpu or not board:
        return False
    # Chipset names overlap in shape (AMD B650 vs Intel B660), so list them explicitly.
    amd_board = bool(re.search(r"\b(?:A320|A520|A620|B350|B450|B550|B650|B840|B850|X370|X470|X570|X670|X870)E?M?\b|\bAM[45]\b", board))
    intel_board = bool(re.search(r"\b(?:H610|B660|H670|Z690|B760|H770|Z790|H810|B860|Z890)M?\b|\bLGA\s?\d{4}", board))
    return ("INTEL" in cpu or re.search(r"\bI[3579]-|ULTRA", cpu)) and amd_board and not intel_board or \
        ("RYZEN" in cpu and intel_board and not amd_board)


def unknowns_honest(option: dict) -> bool:
    """A check that compared a missing spec must be 'unknown', never 'passed'."""
    for check in option.get("validation", {}).get("checks", []):
        if check["status"] == "passed" and check.get("inputs") and None in check["inputs"].values():
            return False
    return True


def sources_match(option: dict, offers: dict) -> bool:
    for item in option["items"]:
        if item.get("owned_by_user"):
            continue
        row = offers.get(item.get("offer_id"))
        if not row or Decimal(str(row["price"])) != Decimal(str(item["price"])) or row["source_url"] != item.get("source_url"):
            return False
    return True
