"""E4: orchestration and recovery.

Part A compares the DAG workflow with the Pi runtime on the same cases (run through the API).
Part B injects faults into otherwise valid builds and measures whether the Review -> Replan loop
repairs them, how many rounds it needs, and whether it changes only the parts the fault concerns.
"""
from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path

from backend.agents import AgentContext
from backend.harness import RunHarness, ToolHarness
from backend.models import SessionCreate
from backend.orchestrator import RecommendationEngine
from backend.planning import Chooser, make_item, plan_desktop
from backend.store import StateStore
from backend.validation import validate_option

PROFILES = [("gaming", 300000), ("SolidWorks", 250000), ("deep learning", 450000), ("gaming", 200000), ("video editing", 350000)]
FAULTS = ["socket_mismatch", "memory_mismatch", "psu_too_small", "over_budget"]
EXPECTED_SCOPE = {"socket_mismatch": {"motherboard", "cpu"}, "memory_mismatch": {"ram", "motherboard"},
                  "psu_too_small": {"psu"}, "over_budget": None}


def inject(option: dict, fault: str, corpus) -> dict | None:
    items = {i["category"]: i for i in option["items"]}
    pick = None
    if fault == "socket_mismatch" and "motherboard" in items:
        socket = items["motherboard"]["specs"].get("socket")
        pick = next((r for r in corpus.candidates("cpu", limit=60) if r["specs"].get("socket") not in (None, socket)), None)
    elif fault == "memory_mismatch" and "motherboard" in items:
        memory = items["motherboard"]["specs"].get("memory_type")
        pick = next((r for r in corpus.candidates("ram", limit=80) if r["specs"].get("memory_type") not in (None, memory)), None)
    elif fault == "psu_too_small":
        pick = next((r for r in corpus.candidates("psu", limit=80, order="price_asc") if (r["specs"].get("wattage_w") or 9999) < 500), None)
    elif fault == "over_budget" and "gpu" in items:
        pick = corpus.candidates("gpu", limit=1, order="price_desc")[0]
    if not pick:
        return None
    broken = {**option, "items": [make_item(pick) if i["category"] == pick["category"] else i for i in option["items"]]}
    broken["option_id"], broken["revision_history"] = f"fault_{fault}", []
    return broken


def fault_injection(store_dir: Path, corpus, settings, model) -> list[dict]:
    rows = []
    for revisions in (settings.max_revisions, 0):
        engine = RecommendationEngine(StateStore(store_dir / f"faults_{revisions}.db"), corpus,
                                      replace(settings, max_revisions=revisions, pi_runtime_url=None))
        engine.model = model
        for workload, budget in PROFILES:
            req = {"device_type": "desktop", "budget": {"currency": "SGD", "maximum_minor": budget}, "workloads": [workload],
                   "preferences": [], "hard_constraints": {}, "locked_product_ids": [], "locked_items": [], "owned_components": []}
            base = plan_desktop(corpus.candidates, corpus.products, req, Chooser())
            for fault in FAULTS:
                broken = inject(base, fault, corpus)
                if not broken:
                    continue
                before_failed = validate_option(broken, req)["failed_codes"]
                session = engine.store.create_session(SessionCreate().model_dump())
                engine.store.update_requirements(session["id"], 0, req, "ready")
                run = engine.store.create_run(session["id"], 1, corpus.snapshot_id, "dag")
                ctx = AgentContext(run["id"], req, corpus.snapshot_id, [], ToolHarness(engine.store, engine.tools(), 200), model)
                original = {i["category"]: i["offer_id"] for i in broken["items"]}
                status = engine._review_loop(ctx, RunHarness(engine.store, run["id"], 60), broken, Chooser(model))
                changed = {i["category"] for i in broken["items"] if original.get(i["category"]) != i["offer_id"]}
                scope = EXPECTED_SCOPE[fault]
                rows.append({"max_revisions": revisions, "workload": workload, "budget_sgd": budget / 100, "fault": fault,
                             "failed_before": before_failed, "status": status,
                             "recovered": status == "accepted" and broken["validation"]["overall_status"] != "failed",
                             "rounds": len(broken["revision_history"]), "changed_categories": sorted(changed),
                             "targeted": not changed or scope is None or changed <= scope,
                             "total_after_sgd": broken["validation"]["total_minor"] / 100})
    return rows


def summarise(dag_scores: list[dict], pi_scores: list[dict], faults: list[dict]) -> dict:
    def rates(scores):
        def mean(key):
            values = [s[key] for s in scores if s.get(key) is not None]
            return round(sum(values) / len(values), 2) if values else None
        return {"cases": len(scores),
                "task_completed": round(sum(bool(s.get("task_completed")) for s in scores) / max(len(scores), 1), 3),
                "constraint_satisfaction": round(sum(bool(s.get("constraint_satisfaction")) for s in scores) / max(len(scores), 1), 3),
                "mean_seconds": mean("seconds"), "mean_tool_calls": mean("tool_calls"), "mean_llm_calls": mean("llm_calls"),
                "mean_tokens": mean("tokens")}
    by = {}
    for row in faults:
        key = ("with_replan" if row["max_revisions"] else "without_replan")
        by.setdefault(key, {}).setdefault(row["fault"], []).append(row)
    recovery = {mode: {fault: {"trials": len(r), "recovery_rate": round(sum(x["recovered"] for x in r) / len(r), 3),
                               "mean_rounds": round(sum(x["rounds"] for x in r) / len(r), 2),
                               "targeted_rate": round(sum(x["targeted"] for x in r) / len(r), 3)}
                       for fault, r in faults_by.items()} for mode, faults_by in by.items()}
    return {"dag_vs_pi": {"dag": rates(dag_scores), "pi": rates(pi_scores)}, "fault_recovery": recovery}
