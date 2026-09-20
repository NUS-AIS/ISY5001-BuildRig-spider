from decimal import Decimal
from typing import Any


def validate_option(option: dict[str, Any], requirements: dict[str, Any]) -> dict[str, Any]:
    checks = []
    total = sum(Decimal(str(item["price"])) * int(item.get("quantity", 1)) for item in option.get("items", []))
    budget = requirements.get("budget", {}).get("maximum_minor")
    if budget is not None:
        checks.append({"code": "budget_limit", "status": "passed" if int(total * 100) <= budget else "failed",
                       "actual_minor": int(total * 100), "limit_minor": budget})
    required_categories = set(option.get("required_categories", []))
    actual = {item["category"] for item in option.get("items", [])}
    missing = sorted(required_categories - actual)
    checks.append({"code": "configuration_completeness", "status": "passed" if not missing else "failed", "missing": missing})
    if option.get("device_type") == "desktop":
        checks.append({"code": "component_compatibility", "status": "unknown",
                       "reason": "The collected catalogue does not yet contain all socket, BIOS, clearance and power fields."})
    else:
        checks.append({"code": "regional_configuration_match", "status": "unknown",
                       "reason": "The exact regional configuration must be confirmed on the merchant page."})
    status = "failed" if any(x["status"] == "failed" for x in checks) else "unknown" if any(x["status"] == "unknown" for x in checks) else "passed"
    return {"overall_status": status, "total_minor": int(total * 100), "checks": checks}
