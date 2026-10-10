"""Deterministic validation of a candidate configuration (FR05).

Every rule is an independent function that returns ``passed``, ``failed`` or ``unknown`` with a
human-readable reason, the spec values it used, and the categories a replanner should change
when it fails. Missing specifications always produce ``unknown`` - never ``passed``.

Items may carry a ``specs`` mapping of plain values (``{"socket": "AM5", ...}``) attached from
the snapshot's specs.jsonl. Money is handled with Decimal.
"""
from decimal import Decimal
from typing import Any, Callable

from backend.attributes import LABELS, active, derive, filters_for, verdict

FORM_FACTOR_RANK = {"Mini-ITX": 1, "Micro-ATX": 2, "ATX": 3, "E-ATX": 4}
PSU_BASE_LOAD_W = 100      # motherboard, memory, storage, fans
PSU_HEADROOM = Decimal("1.2")


def _check(code: str, status: str, reason: str, inputs: dict | None = None, affected: list[str] | None = None) -> dict:
    return {"code": code, "status": status, "reason": reason, "inputs": inputs or {}, "affected_categories": affected or []}


def _first(option: dict, category: str) -> dict | None:
    return next((item for item in option.get("items", []) if item.get("category") == category), None)


def _spec(item: dict | None, field: str):
    if not item:
        return None
    return (item.get("specs") or {}).get(field)


def _name(item: dict | None) -> str:
    return (item or {}).get("name", "?")


def total_minor(option: dict) -> int:
    total = sum(Decimal(str(item["price"])) * int(item.get("quantity", 1)) for item in option.get("items", [])
                if not item.get("owned_by_user"))
    return int(total * 100)


# ------------------------------------------------------------------------------------ rules

def rule_budget(option, req):
    budget = (req.get("budget") or {}).get("maximum_minor")
    if budget is None:
        return None
    actual = total_minor(option)
    priced = sorted((i for i in option.get("items", []) if not i.get("locked") and not i.get("owned_by_user")),
                    key=lambda i: Decimal(str(i["price"])), reverse=True)
    status = "passed" if actual <= budget else "failed"
    reason = (f"Total S${actual / 100:,.2f} is within the S${budget / 100:,.2f} budget." if status == "passed"
              else f"Total S${actual / 100:,.2f} exceeds the S${budget / 100:,.2f} budget by S${(actual - budget) / 100:,.2f}.")
    return _check("budget_limit", status, reason, {"actual_minor": actual, "limit_minor": budget},
                  [priced[0]["category"]] if priced and status == "failed" else [])


def rule_in_stock(option, req):
    missing = [i for i in option.get("items", []) if not i.get("owned_by_user")
               and i.get("availability") not in (None, "in_stock_at_collection")]
    if not missing:
        return _check("in_stock", "passed", "Every purchased item was in stock when the catalogue was collected.")
    return _check("in_stock", "failed", "Out of stock at collection: " + ", ".join(_name(i) for i in missing),
                  {"items": [i.get("offer_id") for i in missing]}, sorted({i["category"] for i in missing}))


def rule_completeness(option, req):
    required = set(option.get("required_categories", []))
    actual = {i["category"] for i in option.get("items", [])}
    missing = sorted(required - actual)
    if not missing:
        return _check("configuration_completeness", "passed", "All required component categories are present.")
    return _check("configuration_completeness", "failed", "Missing components: " + ", ".join(missing),
                  {"missing": missing}, missing)


def rule_locked_items(option, req):
    locked = [str(x) for x in req.get("locked_product_ids") or []]
    if not locked:
        return None
    present = {str(i.get("product_id")) for i in option.get("items", [])}
    lost = [x for x in locked if x not in present]
    if not lost:
        return _check("locked_items_kept", "passed", "Every component the user locked is still in the configuration.")
    return _check("locked_items_kept", "failed", "Locked components were replaced: " + ", ".join(lost), {"lost": lost})


def rule_socket(option, req):
    cpu, board = _first(option, "cpu"), _first(option, "motherboard")
    if not cpu or not board:
        return None
    a, b = _spec(cpu, "socket"), _spec(board, "socket")
    inputs = {"cpu_socket": a, "motherboard_socket": b}
    if a is None or b is None:
        return _check("cpu_motherboard_socket", "unknown", "Socket is not stated for "
                      + (" and ".join(n for n, v in ((_name(cpu), a), (_name(board), b)) if v is None)) + ".", inputs)
    if a == b:
        return _check("cpu_motherboard_socket", "passed", f"CPU and motherboard both use {a}.", inputs)
    return _check("cpu_motherboard_socket", "failed", f"CPU socket {a} does not fit a {b} motherboard.", inputs,
                  ["motherboard"] if not board.get("locked") else ["cpu"])


def rule_memory_type(option, req):
    board, ram = _first(option, "motherboard"), _first(option, "ram")
    if not board or not ram:
        return None
    a, b = _spec(board, "memory_type"), _spec(ram, "memory_type")
    inputs = {"motherboard_memory": a, "ram_memory": b}
    if a is None or b is None:
        return _check("motherboard_memory_type", "unknown", "Memory generation is not stated for the motherboard or the RAM.", inputs)
    if a == b:
        return _check("motherboard_memory_type", "passed", f"Motherboard and RAM are both {a}.", inputs)
    return _check("motherboard_memory_type", "failed", f"{b} memory cannot be used on a {a} motherboard.", inputs,
                  ["ram"] if not ram.get("locked") else ["motherboard"])


def rule_psu_headroom(option, req):
    psu, cpu, gpu = _first(option, "psu"), _first(option, "cpu"), _first(option, "gpu")
    if not psu or not cpu:
        return None
    watt, cpu_w = _spec(psu, "wattage_w"), _spec(cpu, "tdp_w")
    gpu_w = _spec(gpu, "tdp_w") if gpu else 0
    inputs = {"psu_w": watt, "cpu_tdp_w": cpu_w, "gpu_tdp_w": gpu_w}
    if watt is None or cpu_w is None or gpu_w is None:
        return _check("psu_headroom", "unknown", "Power draw or PSU rating is not stated, so headroom cannot be checked.", inputs)
    needed = int((Decimal(cpu_w) + Decimal(gpu_w) + PSU_BASE_LOAD_W) * PSU_HEADROOM)
    inputs["required_w"] = needed
    if watt >= needed:
        return _check("psu_headroom", "passed", f"{watt} W PSU covers the estimated {needed} W requirement.", inputs)
    return _check("psu_headroom", "failed", f"{watt} W PSU is below the estimated {needed} W requirement.", inputs, ["psu"])


def rule_gpu_clearance(option, req):
    gpu, case = _first(option, "gpu"), _first(option, "case")
    if not gpu or not case:
        return None
    length, room = _spec(gpu, "length_mm"), _spec(case, "max_gpu_length_mm")
    inputs = {"gpu_length_mm": length, "case_max_gpu_length_mm": room}
    if length is None or room is None:
        return _check("gpu_case_clearance", "unknown", "Graphics card length or case clearance is not stated.", inputs)
    if length <= room:
        return _check("gpu_case_clearance", "passed", f"{length} mm card fits the {room} mm clearance.", inputs)
    return _check("gpu_case_clearance", "failed", f"{length} mm card is longer than the {room} mm clearance.", inputs,
                  ["case"] if not case.get("locked") else ["gpu"])


def rule_form_factor(option, req):
    board, case = _first(option, "motherboard"), _first(option, "case")
    if not board or not case:
        return None
    a, b = _spec(board, "form_factor"), _spec(case, "max_form_factor")
    inputs = {"motherboard_form_factor": a, "case_max_form_factor": b}
    if a is None or b is None:
        return _check("case_form_factor", "unknown", "Motherboard size or case support is not stated.", inputs)
    if FORM_FACTOR_RANK[a] <= FORM_FACTOR_RANK[b]:
        return _check("case_form_factor", "passed", f"{a} motherboard fits a case that takes up to {b}.", inputs)
    return _check("case_form_factor", "failed", f"{a} motherboard does not fit a case limited to {b}.", inputs, ["case"])


def rule_display_output(option, req):
    if option.get("device_type") != "desktop" or _first(option, "gpu"):
        return None
    cpu = _first(option, "cpu")
    if not cpu:
        return None
    igpu = _spec(cpu, "integrated_graphics")
    inputs = {"cpu_integrated_graphics": igpu, "graphics_card": "none in this build"}
    if igpu is None:
        return _check("display_output", "unknown", "No graphics card, and it is not stated whether the CPU has integrated graphics.", inputs)
    if igpu:
        return _check("display_output", "passed", "No graphics card needed: the CPU has integrated graphics.", inputs)
    return _check("display_output", "failed", f"{_name(cpu)} has no integrated graphics and no graphics card is included.",
                  inputs, ["cpu"])


def rule_memory_capacity(option, req):
    minimum = (req.get("hard_constraints") or {}).get("minimum_memory_gb")
    if not minimum:
        return None
    item = _first(option, "ram") or _first(option, "laptop")
    if not item:
        return None
    size = _spec(item, "capacity_gb") if item["category"] == "ram" else _spec(item, "ram_gb")
    inputs = {"memory_gb": size, "minimum_gb": minimum}
    if size is None:
        return _check("minimum_memory", "unknown", "Memory size is not stated.", inputs)
    if size >= minimum:
        return _check("minimum_memory", "passed", f"{size} GB meets the {minimum} GB requirement.", inputs)
    return _check("minimum_memory", "failed", f"{size} GB is below the required {minimum} GB.", inputs, [item["category"]])


def rule_storage_capacity(option, req):
    minimum = (req.get("hard_constraints") or {}).get("minimum_storage_gb")
    if not minimum:
        return None
    item = _first(option, "ssd") or _first(option, "laptop")
    if not item:
        return None
    size = _spec(item, "capacity_gb") if item["category"] == "ssd" else _spec(item, "storage_gb")
    inputs = {"storage_gb": size, "minimum_gb": minimum}
    if size is None:
        return _check("minimum_storage", "unknown", "Storage size is not stated.", inputs)
    if size >= minimum:
        return _check("minimum_storage", "passed", f"{size} GB meets the {minimum} GB requirement.", inputs)
    return _check("minimum_storage", "failed", f"{size} GB is below the required {minimum} GB.", inputs, [item["category"]])


def rule_gpu_memory(option, req):
    minimum = (req.get("hard_constraints") or {}).get("minimum_gpu_memory_gb")
    if not minimum or option.get("device_type") != "desktop":
        return None
    gpu = _first(option, "gpu")
    inputs = {"gpu_memory_gb": _spec(gpu, "vram_gb"), "minimum_gb": minimum}
    if not gpu:
        return _check("minimum_gpu_memory", "failed", f"A graphics card with at least {minimum} GB is required, but this "
                      "build has none.", inputs, ["gpu"])
    size = inputs["gpu_memory_gb"]
    if size is None:
        return _check("minimum_gpu_memory", "unknown", "Graphics memory is not stated for the graphics card.", inputs)
    if size >= minimum:
        return _check("minimum_gpu_memory", "passed", f"{size} GB graphics memory meets the {minimum} GB requirement.", inputs)
    return _check("minimum_gpu_memory", "failed", f"{size} GB graphics memory is below the required {minimum} GB.", inputs, ["gpu"])


# Requirements about what a part is (maker, chip, store, Wi-Fi, screen): filter key -> check code.
ATTRIBUTE_CHECKS = {"gpu_vendor": "gpu_brand", "min_gpu_model": "minimum_gpu_model", "cpu_maker": "processor_brand",
                    "wifi": "wifi", "max_form_factor": "maximum_size", "cooler_kind": "cooling_type",
                    "exclude_brands": "excluded_brands", "stores": "allowed_stores", "brands": "laptop_brand",
                    "screen_inches": "screen_size", "oled": "oled_screen", "dedicated_gpu": "dedicated_graphics"}
REQUIREMENT_OF = {"min_gpu_model": "minimum_gpu_model", "max_form_factor": "maximum_form_factor", "exclude_brands": "excluded_brands",
                  "brands": "laptop_brands", "screen_inches": "laptop_screen_inches", "oled": "laptop_oled",
                  "dedicated_gpu": "laptop_dedicated_gpu"}


def rules_stated_requirements(option, req) -> list[dict]:
    """One check per requirement the user stated about a part's maker, model, store or features. A listing
    that does not say (no chip name, no screen size) gives ``unknown``, never ``passed``."""
    wanted = active(req)
    checks: dict[str, dict] = {}
    for item in option.get("items", []):
        if item.get("owned_by_user"):
            continue
        have = derive(item)
        for key, value in filters_for(item["category"], {**req, "hard_constraints": wanted}).items():
            result = verdict(have, {key: value})
            said = LABELS[REQUIREMENT_OF.get(key, key)][1](value)
            check = checks.setdefault(ATTRIBUTE_CHECKS[key], {"status": "passed", "failed": [], "unknown": [], "said": said, "affected": []})
            if result is False:
                check["failed"].append(_name(item)); check["affected"].append(item["category"])
            elif result is None:
                check["unknown"].append(_name(item))
    out = []
    for code, c in checks.items():
        if c["failed"]:
            out.append(_check(code, "failed", f"You asked for {c['said']}; this does not hold for " + ", ".join(n[:60] for n in c["failed"]) + ".",
                              {"requirement": c["said"]}, sorted(set(c["affected"]))))
        elif c["unknown"]:
            out.append(_check(code, "unknown", f"You asked for {c['said']}; the listing does not say for " + ", ".join(n[:60] for n in c["unknown"]) + ".",
                              {"requirement": c["said"]}))
        else:
            out.append(_check(code, "passed", f"Meets your requirement: {c['said']}.", {"requirement": c["said"]}))
    return out


def rule_no_graphics_card(option, req):
    if not active(req).get("no_graphics_card") or option.get("device_type") != "desktop":
        return None
    gpu = _first(option, "gpu")
    if gpu and not gpu.get("locked") and not gpu.get("owned_by_user"):
        return _check("no_graphics_card", "failed", f"You asked for no graphics card, but the build includes {_name(gpu)[:60]}.", {}, ["gpu"])
    return _check("no_graphics_card", "passed", "No graphics card is bought, as you asked.")


def rule_platform(option, req):
    """The platform (socket), memory generation and power supply size the user named."""
    wanted, out = active(req), []
    cpu, board, psu = _first(option, "cpu"), _first(option, "motherboard"), _first(option, "psu")
    if wanted.get("socket") and (cpu or board):
        have = [(i, _spec(i, "socket")) for i in (cpu, board) if i]
        wrong = [i for i, s in have if s is not None and s != wanted["socket"]]
        if wrong:
            out.append(_check("platform_socket", "failed", f"You asked for the {wanted['socket']} platform, but {_name(wrong[0])[:60]} "
                              f"uses {_spec(wrong[0], 'socket')}.", {"wanted": wanted["socket"]},
                              sorted({i["category"] for i in wrong if not i.get("locked")})))
        elif any(s is None for _, s in have):
            out.append(_check("platform_socket", "unknown", f"You asked for the {wanted['socket']} platform; a socket is not stated.", {"wanted": wanted["socket"]}))
        else:
            out.append(_check("platform_socket", "passed", f"Processor and motherboard are {wanted['socket']}, as you asked.", {"wanted": wanted["socket"]}))
    if wanted.get("memory_type") and board:
        have = _spec(board, "memory_type")
        status = "unknown" if have is None else "passed" if have == wanted["memory_type"] else "failed"
        reason = {"unknown": "the motherboard does not state its memory generation.", "passed": "the motherboard uses it.",
                  "failed": f"the motherboard uses {have}."}[status]
        out.append(_check("memory_generation", status, f"You asked for {wanted['memory_type']} memory; {reason}", {"wanted": wanted["memory_type"]},
                          ["motherboard"] if status == "failed" else []))
    if wanted.get("minimum_psu_watts") and psu:
        have = _spec(psu, "wattage_w")
        status = "unknown" if have is None else "passed" if have >= wanted["minimum_psu_watts"] else "failed"
        out.append(_check("minimum_psu_wattage", status, f"You asked for at least {wanted['minimum_psu_watts']} W; "
                          + ("the power supply does not state its rating." if have is None else f"the power supply is {have} W."),
                          {"wanted_w": wanted["minimum_psu_watts"], "psu_w": have}, ["psu"] if status == "failed" else []))
    return out


def rule_no_bundle_listing(option, req):
    bundles = [i for i in option.get("items", []) if (i.get("flags") or {}).get("bundle_suspect")]
    if not bundles:
        return None
    return _check("single_component_listings", "failed",
                  "These listings bundle several components and cannot be priced as one part: "
                  + ", ".join(_name(i) for i in bundles), {}, sorted({i["category"] for i in bundles}))


def rule_laptop_regional_match(option, req):
    if option.get("device_type") != "laptop":
        return None
    return _check("regional_configuration_match", "unknown",
                  "The exact regional configuration (keyboard layout, warranty) must be confirmed on the merchant page.")


RULES: list[Callable[[dict, dict], dict | list[dict] | None]] = [
    rule_budget, rule_in_stock, rule_completeness, rule_locked_items, rule_no_bundle_listing,
    rule_socket, rule_memory_type, rule_psu_headroom, rule_gpu_clearance, rule_form_factor, rule_display_output,
    rule_memory_capacity, rule_storage_capacity, rule_gpu_memory, rule_no_graphics_card, rule_platform,
    rules_stated_requirements, rule_laptop_regional_match,
]


def validate_option(option: dict[str, Any], requirements: dict[str, Any]) -> dict[str, Any]:
    checks = []
    for rule in RULES:           # a rule yields one check, several, or none when it does not apply
        result = rule(option, requirements)
        checks += result if isinstance(result, list) else [result] if result else []
    status = ("failed" if any(c["status"] == "failed" for c in checks)
              else "unknown" if any(c["status"] == "unknown" for c in checks) else "passed")
    failed = [c for c in checks if c["status"] == "failed"]
    affected = sorted({cat for c in failed for cat in c["affected_categories"]})
    return {"overall_status": status, "total_minor": total_minor(option), "checks": checks,
            "failed_codes": [c["code"] for c in failed], "affected_categories": affected}
