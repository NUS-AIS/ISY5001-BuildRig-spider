"""Follow-up adjustments: compare two requirement versions and reuse the previous run (FR06, FR08).

When the user changes one requirement, the previous options are carried over and only the parts the
change affects are re-planned by the normal Review -> Replan loop. Confirmed constraints that the
user did not touch stay exactly as they were.

Decision table:
  device type changed                 -> plan from scratch
  budget raised by more than 15 %     -> plan from scratch (otherwise the extra money is never used)
  anything else                       -> reuse the previous options; validation then flags what no
                                         longer holds (over budget, too little memory, a new locked
                                         part) and the replanner changes only those categories
"""
from __future__ import annotations

from copy import deepcopy

from backend.planning import make_item, owned_item

TRACKED = ("device_type", "budget", "workloads", "preferences", "hard_constraints", "locked_product_ids", "owned_components")


def requirement_changes(before: dict, after: dict) -> list[dict]:
    changes = []
    for key in TRACKED:
        old, new = before.get(key), after.get(key)
        if key == "budget":
            old, new = (old or {}).get("maximum_minor"), (new or {}).get("maximum_minor")
        if key == "owned_components":
            old, new = sorted(o.get("mention", "") for o in old or []), sorted(o.get("mention", "") for o in new or [])
        if (old or None) != (new or None):
            changes.append({"field": key, "before": old, "after": new})
    return changes


def reuse_decision(before: dict, after: dict, changes: list[dict]) -> tuple[bool, str]:
    fields = {c["field"] for c in changes}
    if "device_type" in fields:
        return False, "device type changed"
    if "budget" in fields:
        old = (before.get("budget") or {}).get("maximum_minor") or 0
        new = (after.get("budget") or {}).get("maximum_minor") or 0
        if old and new > old * 1.15:
            return False, "budget raised by more than 15%, planning again to use it"
    if not changes:
        return True, "no requirement changed"
    return True, "reusing unaffected parts; only parts that no longer satisfy the requirements are replanned"


def carry_over(base_options: list[dict], requirements: dict, products) -> list[dict]:
    """Copy the previous options, mark their parts as carried over and insert newly locked parts."""
    locked_rows = {row["category"]: row for row in products(requirements.get("locked_product_ids", []))}
    owned = {o["category"]: o for o in requirements.get("owned_components", []) if o.get("category")}
    carried = []
    for index, old in enumerate(base_options):
        option = deepcopy(old)
        for key in ("validation", "review", "evidence", "evidence_queries", "explanation_guard", "title",
                    "reasons", "trade_offs", "tried_offer_ids"):
            option.pop(key, None)
        option["reused_from_option"] = old["option_id"]
        option["revision_history"] = []
        items = []
        for item in option["items"]:
            item["carried_over"] = True
            replacement = locked_rows.get(item["category"])
            if item["category"] in owned and not item.get("owned_by_user"):
                items.append(owned_item(owned[item["category"]]))
            elif replacement and item.get("offer_id") != replacement["id"]:
                new = make_item(replacement, locked=True)
                new["selection_reason"], new["selected_by"] = "Locked by the user in a follow-up message.", "user"
                items.append(new)
            else:
                item["locked"] = item.get("locked") or str(item.get("product_id")) in requirements.get("locked_product_ids", [])
                items.append(item)
        option["items"] = items
        option["option_id"] = f"option_{index + 1}_r_{items[0]['offer_id'] or 'owned'}"
        carried.append(option)
    return carried
