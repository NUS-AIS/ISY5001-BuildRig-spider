"""Run the evaluation suite and write results to evaluation/results/.

    python -m evaluation.run_all                         # everything (about 2 hours on an RTX 4060)
    python -m evaluation.run_all --only e3,e5            # selected experiments
    python -m evaluation.run_all --resume                # skip cases already recorded

Requires the local stack (docker compose up -d), an indexed snapshot (python -m backend.index_data)
and a built Pi worker (pi-worker/dist). Results are deterministic up to the local model's sampling
(temperature 0) and are tagged with the model, snapshot and git commit in run_metadata.json.
"""
from __future__ import annotations

import argparse
import json
import logging
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from backend.check_consistency import run as consistency_run
from backend.model_gateway import ModelGateway
from backend.production_retrieval import Neo4jMilvusCorpus
from backend.settings import settings
from evaluation import e1_end_to_end as e1
from evaluation import e3_retrieval, e4_orchestration, e5_data_quality
from evaluation.baseline_single_agent import SingleAgentBaseline
from evaluation.harness import System

RESULTS = Path(__file__).parent / "results"
E2_CASES = ["D01", "D02", "D03", "D04", "D05", "D06", "D07", "D08", "L01", "L02", "L03", "L04", "L05", "L06", "K01", "M01", "M04"]
E4_CASES = ["D01", "D02", "D04", "D07", "L01", "L02", "K01", "M04"]


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines() if l.strip()] if path.exists() else []


def append_jsonl(path: Path, row: dict) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def play_cases(system: System, cases: list[dict], mode: str, path: Path, resume: bool) -> list[dict]:
    done = {r["case_id"]: r for r in load_jsonl(path)} if resume else {}
    if not resume and path.exists():
        path.unlink()
    for case in cases:
        if case["id"] in done:
            continue
        print(f"  [{mode}] {case['id']} {case['turns'][0][:60]}", flush=True)
        record = system.play(case, mode)
        append_jsonl(path, record)
        done[case["id"]] = record
    return [done[c["id"]] for c in cases if c["id"] in done]


def main():
    global RESULTS, E2_CASES, E4_CASES
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="e1,e2,e3,e4,e5,consistency")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--cases", help="comma-separated case ids (smoke runs)")
    parser.add_argument("--results", default=str(RESULTS), help="output folder")
    args = parser.parse_args()
    wanted = set(args.only.split(","))
    RESULTS = Path(args.results)
    if args.cases:
        chosen = args.cases.split(",")
        E2_CASES = [c for c in E2_CASES if c in chosen]
        E4_CASES = [c for c in E4_CASES if c in chosen]
    logging.getLogger("neo4j").setLevel(logging.ERROR)
    logging.getLogger("pymilvus").setLevel(logging.CRITICAL)
    RESULTS.mkdir(exist_ok=True)
    corpus = Neo4jMilvusCorpus(settings.data_dir, settings)
    offers = {r["id"]: r for r in corpus.prices}
    model = ModelGateway(settings.model_name, settings.model_provider, settings.ollama_base_url, False, 8192, 600)
    commit = subprocess.run(["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    meta = {"started_at": datetime.now(timezone.utc).isoformat(), "git_commit": commit, "snapshot_id": corpus.snapshot_id,
            "chat_model": settings.model_name, "embedding_model": settings.embedding_model,
            "milvus_collection": settings.milvus_collection, "max_revisions": settings.max_revisions,
            "experiments": sorted(wanted)}
    (RESULTS / "run_metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    cases = e1.load_cases(args.cases.split(",") if args.cases else None)
    by_id = {c["id"]: c for c in cases}

    system = System() if wanted & {"e1", "e2", "e4"} else None
    try:
        if wanted & {"e1", "e2", "e4"}:
            print("E1: end-to-end cases (DAG)", flush=True)
            records = play_cases(system, cases, "dag", RESULTS / "e1_records.jsonl", args.resume or "e1" not in wanted)
            scores = [e1.score(by_id[r["case_id"]], r, offers, model) for r in records]
            (RESULTS / "e1_end_to_end.json").write_text(json.dumps(
                {"experiment": "E1 end-to-end", "summary": e1.aggregate(scores), "cases": scores}, indent=2, ensure_ascii=False),
                encoding="utf-8")
            with (RESULTS / "judge_items.jsonl").open("w", encoding="utf-8") as fh:
                for s in scores:
                    for item in s.get("judge", []):
                        fh.write(json.dumps({"case_id": s["case_id"], **item}, ensure_ascii=False) + "\n")
            dag_by_id = {s["case_id"]: s for s in scores}

        if "e2" in wanted:
            print("E2: single-agent baseline", flush=True)
            baseline = SingleAgentBaseline(corpus, settings)
            path = RESULTS / "e2_baseline_records.jsonl"
            done = {r["case_id"]: r for r in load_jsonl(path)} if args.resume else {}
            if not args.resume and path.exists():
                path.unlink()
            for cid in E2_CASES:
                if cid in done:
                    continue
                source = next(r for r in records if r["case_id"] == cid)["turns"][-1]
                print(f"  [single] {cid}", flush=True)
                result = baseline.run(source["requirements"], by_id[cid]["expect"]["device_types"][0])
                record = {"case_id": cid, "mode": "single_agent", "turns": [{
                    "text": source["text"], "requirements": source["requirements"], "questions": [], "can_generate": True,
                    "result": result, "seconds": result["duration_seconds"]}]}
                append_jsonl(path, record)
                done[cid] = record
            single = [e1.score(by_id[cid], done[cid], offers, model) for cid in E2_CASES if cid in done]
            multi = [dag_by_id[cid] for cid in E2_CASES if cid in dag_by_id]
            (RESULTS / "e2_multi_vs_single.json").write_text(json.dumps(
                {"experiment": "E2 multi-agent vs single-agent", "cases": E2_CASES,
                 "summary": {"multi_agent_dag": e1.aggregate(multi), "single_agent": e1.aggregate(single)},
                 "single_agent_cases": single}, indent=2, ensure_ascii=False), encoding="utf-8")

        if "e3" in wanted:
            print("E3: retrieval ablation", flush=True)
            e3_retrieval.run(corpus, RESULTS)

        if "e4" in wanted:
            print("E4: DAG vs Pi and fault injection", flush=True)
            pi_records = play_cases(system, [by_id[c] for c in E4_CASES], "pi", RESULTS / "e4_pi_records.jsonl", args.resume)
            pi_scores = [e1.score(by_id[r["case_id"]], r, offers) for r in pi_records]
            with tempfile.TemporaryDirectory() as tmp:
                faults = e4_orchestration.fault_injection(Path(tmp), corpus, settings, model)
            summary = e4_orchestration.summarise([dag_by_id[c] for c in E4_CASES if c in dag_by_id], pi_scores, faults)
            (RESULTS / "e4_orchestration.json").write_text(json.dumps(
                {"experiment": "E4 orchestration and recovery", "summary": summary, "pi_cases": pi_scores, "fault_trials": faults},
                indent=2, ensure_ascii=False), encoding="utf-8")
    finally:
        if system:
            system.close()

    if "e5" in wanted:
        print("E5: data quality", flush=True)
        snapshot_dir = settings.data_dir / json.loads((settings.data_dir / "latest.json").read_text(encoding="utf-8"))["path"]
        report = {"experiment": "E5 data quality", "coverage": e5_data_quality.coverage(snapshot_dir),
                  "llm_vs_rules": e5_data_quality.llm_agreement(corpus, model),
                  "compatibility": e5_data_quality.compatibility_judgements(corpus)}
        (RESULTS / "e5_data_quality.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    if "consistency" in wanted:
        report = consistency_run()
        (RESULTS / "consistency_report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    meta["finished_at"] = datetime.now(timezone.utc).isoformat()
    (RESULTS / "run_metadata.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
    from evaluation.report import build
    build(RESULTS)
    print(f"done; see {RESULTS / 'summary.md'}")


if __name__ == "__main__":
    main()
