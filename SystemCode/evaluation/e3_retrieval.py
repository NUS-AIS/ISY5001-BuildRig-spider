"""E3: retrieval ablation on the production stores (Neo4j + Milvus)."""
from __future__ import annotations

import json
import time
from pathlib import Path

from evaluation.metrics import ndcg_at_k, offer_relevant, precision_at_k, recall_at_k, review_relevant

VARIANTS = {
    "dense": {"routes": ["dense"]},
    "bm25": {"routes": ["bm25"]},
    "bm25+dense": {"routes": ["bm25", "dense"]},
    "bm25+dense+graph": {"routes": ["bm25", "dense", "graph"]},
    "hybrid+neo4j_constraint": {"routes": ["bm25", "dense", "graph"], "constrained": True},
}


def run(corpus, out_dir: Path, k_values=(5, 10)) -> dict:
    queries = json.loads((Path(__file__).parent / "retrieval" / "queries.json").read_text(encoding="utf-8"))["queries"]
    offers = {r["id"]: r for r in corpus.prices}
    reviews = {r["id"]: r for r in corpus.reviews}
    rows = []
    for q in queries:
        rule = q["relevant"]
        relevant_ids = {f"price:{oid}" for oid, r in offers.items() if offer_relevant(r, corpus.specs.get(oid, {}), rule)}
        relevant_ids |= {f"review:{rid}" for rid, r in reviews.items() if review_relevant(r, rule)}
        for name, variant in VARIANTS.items():
            started = time.time()
            categories = rule["category"] if variant.get("constrained") else None
            result = corpus.search(q["query"], categories=categories, top_k=max(k_values), routes=variant["routes"])
            latency = time.time() - started
            flags = [item["evidence_id"] in relevant_ids for item in result["items"]]
            top = result["items"][:5]
            row = {"query_id": q["id"], "type": q["type"], "variant": name, "relevant_total": len(relevant_ids),
                   "latency_s": round(latency, 3), "degraded": result.get("degraded_routes", []),
                   "top5": [{"evidence_id": i["evidence_id"], "relevant": f, "excerpt": i["excerpt"][:80]}
                            for i, f in zip(top, flags)]}
            for k in k_values:
                row[f"recall@{k}"] = round(recall_at_k(flags, k, len(relevant_ids)), 4)
                row[f"ndcg@{k}"] = round(ndcg_at_k(flags, k, len(relevant_ids)), 4)
                row[f"precision@{k}"] = round(precision_at_k(flags, k), 4)
            rows.append(row)
    summary = {}
    for name in VARIANTS:
        for qtype in ("all", "model", "spec", "semantic"):
            sel = [r for r in rows if r["variant"] == name and (qtype == "all" or r["type"] == qtype)]
            summary.setdefault(name, {})[qtype] = {m: round(sum(r[m] for r in sel) / len(sel), 4)
                                                  for m in ("recall@5", "recall@10", "ndcg@5", "ndcg@10", "precision@5")}
        sel = [r for r in rows if r["variant"] == name]
        summary[name]["latency_s"] = round(sum(r["latency_s"] for r in sel) / len(sel), 3)
    report = {"experiment": "E3 retrieval ablation", "queries": len(queries), "variants": list(VARIANTS),
              "summary": summary, "per_query": rows}
    (out_dir / "e3_retrieval_ablation.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report
