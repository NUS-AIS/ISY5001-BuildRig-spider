"""Cross-store consistency report for the pinned snapshot (FR04, quality requirement).

Checks that Neo4j and Milvus describe the same snapshot: counts, orphan chunks whose product or
evidence is missing from the graph, graph evidence without any chunk, foreign snapshot ids, and the
manifest status. Writes consistency_report.json into the snapshot folder and prints a summary.

Usage (from SystemCode/):  python -m backend.check_consistency
"""
import json
from collections import Counter
from datetime import datetime, timezone

from backend.index_data import milvus_client, neo4j_driver
from backend.retrieval import LocalCorpus
from backend.settings import settings


def milvus_rows(client, collection: str) -> list[dict]:
    rows, iterator = [], client.query_iterator(collection, batch_size=1000, filter="",
                                               output_fields=["chunk_id", "evidence_id", "product_id", "snapshot_id", "kind"])
    while True:
        batch = iterator.next()
        if not batch:
            break
        rows.extend(batch)
    iterator.close()
    return rows


def run() -> dict:
    corpus = LocalCorpus(settings.data_dir)
    sid = corpus.snapshot_id
    driver, client = neo4j_driver(), milvus_client()
    db = settings.neo4j_database
    graph_evidence = {r["e"] for r in driver.execute_query(
        "MATCH (o:Offer {snapshot_id: $sid}) RETURN o.evidence_id AS e UNION MATCH (r:Review {snapshot_id: $sid}) RETURN r.evidence_id AS e",
        sid=sid, database_=db).records}
    graph_products = {r["p"] for r in driver.execute_query("MATCH (p:Product) RETURN p.product_id AS p", database_=db).records}
    manifest = driver.execute_query("MATCH (m:SnapshotManifest {snapshot_id: $sid}) RETURN m {.*} AS m", sid=sid,
                                    database_=db).records
    stale_offers = driver.execute_query("MATCH (o:Offer) WHERE o.snapshot_id <> $sid RETURN count(o) AS n",
                                        sid=sid, database_=db).records[0]["n"]
    driver.close()

    rows = milvus_rows(client, settings.milvus_collection)
    chunk_evidence = {r["evidence_id"] for r in rows}
    foreign = Counter(r["snapshot_id"] for r in rows if r["snapshot_id"] != sid)
    orphan_evidence = sorted(chunk_evidence - graph_evidence)
    orphan_products = sorted({r["product_id"] for r in rows if r["product_id"] and r["product_id"] not in graph_products})
    uncovered = sorted(graph_evidence - chunk_evidence)
    report = {
        "checked_at": datetime.now(timezone.utc).isoformat(), "snapshot_id": sid,
        "manifest": manifest[0]["m"] if manifest else None,
        "snapshot_files": {"offers": len(corpus.prices), "reviews": len(corpus.reviews), "chunks": len(corpus.documents)},
        "neo4j": {"evidence_items": len(graph_evidence), "products": len(graph_products), "offers_from_other_snapshots": stale_offers},
        "milvus": {"collection": settings.milvus_collection, "chunks": len(rows), "evidence_items": len(chunk_evidence),
                   "chunks_by_kind": dict(Counter(r["kind"] for r in rows))},
        "problems": {"orphan_chunks_evidence": orphan_evidence[:20], "orphan_chunks_evidence_count": len(orphan_evidence),
                     "orphan_chunk_products": orphan_products[:20], "orphan_chunk_products_count": len(orphan_products),
                     "graph_evidence_without_chunks": uncovered[:20], "graph_evidence_without_chunks_count": len(uncovered),
                     "chunks_from_other_snapshots": dict(foreign)},
    }
    report["consistent"] = (not orphan_evidence and not orphan_products and not uncovered and not foreign
                            and len(rows) == len(corpus.documents)
                            and len(graph_evidence) == len(corpus.prices) + len(corpus.reviews)
                            and bool(manifest) and manifest[0]["m"].get("status") == "published")
    path = settings.data_dir / json.loads((settings.data_dir / "latest.json").read_text(encoding="utf-8"))["path"] / "consistency_report.json"
    path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


if __name__ == "__main__":
    result = run()
    print(json.dumps({k: result[k] for k in ("snapshot_id", "consistent", "snapshot_files", "neo4j", "milvus")}, indent=2))
    print("problems:", json.dumps({k: v for k, v in result["problems"].items() if k.endswith("count") or k.endswith("snapshots")}))
