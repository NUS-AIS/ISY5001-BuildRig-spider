"""E1: end-to-end task quality on the 40-case test set, scored against each case's expectation."""
from __future__ import annotations

import json
from pathlib import Path

from evaluation.judge import judge_option
from evaluation.metrics import known_conflict, option_constraints_ok, sources_match, unknowns_honest

CASES = Path(__file__).parent / "cases" / "end_to_end.json"


def load_cases(ids: list[str] | None = None) -> list[dict]:
    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    return [c for c in cases if not ids or c["id"] in ids]


def score(case: dict, record: dict, offers: dict, model=None) -> dict:
    expect = case["expect"]
    final = record["turns"][-1]
    result = final.get("result") or {}
    options = result.get("options", [])
    s = {"case_id": case["id"], "scenario": case["scenario"], "fr": case["fr"],
         "seconds": sum(t.get("seconds", 0) for t in record["turns"]),
         "tool_calls": sum(len((t.get("result") or {}).get("tool_calls", [])) for t in record["turns"]),
         "llm_calls": sum(((t.get("result") or {}).get("model_usage") or {}).get("calls", 0) for t in record["turns"]),
         "tokens": sum(((t.get("result") or {}).get("model_usage") or {}).get("input_tokens", 0)
                       + ((t.get("result") or {}).get("model_usage") or {}).get("output_tokens", 0) for t in record["turns"])}
    behaviour = expect["behaviour"]
    if behaviour == "clarify":
        s["task_completed"] = not final["can_generate"] and set(expect["questions"]) <= set(final["questions"])
        s["detail"] = f"questions={final['questions']}"
        return s
    if behaviour == "infeasible":
        s["task_completed"] = result.get("outcome") == "no_feasible_option"
        s["constraint_satisfaction"] = s["task_completed"] or all(
            option_constraints_ok(o, expect, final["requirements"])["within_budget"] for o in options)
        s["detail"] = result.get("outcome") or final["questions"]
        return s
    if behaviour == "not_recommend_unavailable":
        names = [i["name"] for o in options for i in o["items"]]
        s["task_completed"] = not any(expect["unavailable_name"].casefold() in n.casefold() for n in names)
        s["detail"] = result.get("outcome") or final["questions"]
        return s

    recommended = result.get("outcome") == "recommendations_available"
    kinds = sorted({o["device_type"] for o in options})
    s["task_completed"] = recommended and kinds == sorted(expect.get("device_types", kinds)) if behaviour == "recommend" \
        else bool(result.get("outcome")) and (not options or kinds == sorted(expect.get("device_types", kinds)))
    s["detail"] = f"outcome={result.get('outcome')} kinds={kinds} questions={final['questions']}"
    if not options:
        return s
    checks = [option_constraints_ok(o, expect, final["requirements"]) for o in options]
    s["constraint_satisfaction"] = all(all(c.values()) for c in checks)
    s["constraint_detail"] = checks
    s["compatibility_correct"] = all(not known_conflict(o) and c["no_failed_check"] for o, c in zip(options, checks))
    s["source_accuracy"] = all(sources_match(o, offers) for o in options)
    s["unknown_handling"] = all(unknowns_honest(o) for o in options)
    if expect.get("unknown_check"):
        s["unknown_handling"] = s["unknown_handling"] and all(
            next((c["status"] for c in o["validation"]["checks"] if c["code"] == expect["unknown_check"]), "unknown") == "unknown"
            for o in options)
    if expect.get("family_reviews_not_exact"):
        evidence = [e for o in options for e in o.get("evidence", []) if e.get("kind") == "review"]
        s["evidence_levels_respected"] = all(e.get("match_level") != "exact_offer" for e in evidence)
    if "reused" in expect:
        follow = result.get("follow_up") or {}
        first = (record["turns"][0].get("requirements") or {})
        kept = all(final["requirements"].get(k) == first.get(k) for k in ("hard_constraints",)
                   if first.get(k) and k not in [c["field"] for c in follow.get("changes", [])])
        s["adjustment_success"] = follow.get("reused") == expect["reused"] and kept and s["constraint_satisfaction"]
        if expect.get("preference"):
            s["adjustment_success"] = s["adjustment_success"] and expect["preference"] in final["requirements"].get("preferences", [])
    if model is not None:
        scores = [judge_option(model, final["requirements"], o) for o in options]
        valid = [x for x in scores if x]
        if valid:
            s["explanation_quality"] = round(sum(x["overall"] for x in valid) / len(valid), 2)
            s["judge"] = valid
    return s


def aggregate(scores: list[dict]) -> dict:
    def rate(key):
        values = [s[key] for s in scores if key in s]
        return {"rate": round(sum(bool(v) for v in values) / len(values), 3), "n": len(values)} if values else None
    out = {k: rate(k) for k in ("task_completed", "constraint_satisfaction", "compatibility_correct", "source_accuracy",
                                "unknown_handling", "adjustment_success", "evidence_levels_respected")}
    quality = [s["explanation_quality"] for s in scores if "explanation_quality" in s]
    out["explanation_quality_mean"] = round(sum(quality) / len(quality), 2) if quality else None
    for key in ("seconds", "tool_calls", "llm_calls", "tokens"):
        values = [s[key] for s in scores if s.get(key)]
        out[f"mean_{key}"] = round(sum(values) / len(values), 1) if values else None
    by = {}
    for s in scores:
        by.setdefault(s["scenario"], []).append(bool(s.get("task_completed")))
    out["task_completed_by_scenario"] = {k: round(sum(v) / len(v), 3) for k, v in by.items()}
    return out
