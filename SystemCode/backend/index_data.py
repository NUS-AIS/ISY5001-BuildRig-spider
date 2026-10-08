"""Index the pinned data snapshot into Neo4j (knowledge graph) and Milvus (dense + BM25 chunks).

Publishing protocol (no cross-store transactions exist, so the manifest is the switch):
  1. mark SnapshotManifest(status="staged")
  2. MERGE the graph in Neo4j (idempotent, safe to re-run)
  3. rebuild the Milvus collection for this embedding model
  4. verify counts in both stores
  5. mark SnapshotManifest(status="published") only if step 4 passed

Graph model (FR03):
  (Product)-[:HAS_VARIANT]->(Variant)-[:HAS_OFFER]->(Offer)-[:SOLD_BY]->(Merchant)
  (Offer)-[:FROM_SOURCE]->(Source)
  (Variant)-[:HAS_SPEC {method, confidence, evidence}]->(Spec {key, value})   shared spec nodes
  (Variant:cpu)-[:SOCKET_COMPATIBLE]->(Variant:motherboard)                    derived, with basis
  (Variant:motherboard)-[:MEMORY_COMPATIBLE]->(Variant:ram)                    derived, with basis
  (Review)-[:ABOUT {match_level}]->(Product), (Review)-[:FROM_SOURCE]->(Source)

Usage (from SystemCode/):  python -m backend.index_data [--neo4j-only | --milvus-only]
"""
import argparse
import json
import time
from datetime import datetime, timezone

from backend.retrieval import LocalCorpus
from backend.settings import settings

GRAPH_SPEC_KEYS = ("socket", "memory_type", "form_factor", "max_form_factor", "chip", "integrated_graphics")


def neo4j_driver():
    if not settings.neo4j_password:
        raise SystemExit("Set NEO4J_PASSWORD before indexing")
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
    driver.verify_connectivity()
    return driver


def milvus_client():
    from pymilvus import MilvusClient
    kwargs = {"uri": settings.milvus_uri}
    if settings.milvus_token:
        kwargs["token"] = settings.milvus_token
    elif settings.milvus_user:
        kwargs["user"], kwargs["password"] = settings.milvus_user, settings.milvus_password
    if settings.milvus_database and settings.milvus_database != "default":
        kwargs["db_name"] = settings.milvus_database
    return MilvusClient(**kwargs)


def set_manifest(driver, snapshot_id: str, **fields):
    driver.execute_query("MERGE (m:SnapshotManifest {snapshot_id: $sid}) SET m += $fields",
                         sid=snapshot_id, fields={**fields, "updated_at": datetime.now(timezone.utc).isoformat()},
                         database_=settings.neo4j_database)


# ----------------------------------------------------------------------------------- Neo4j

CONSTRAINTS = [
    "CREATE CONSTRAINT product_id IF NOT EXISTS FOR (p:Product) REQUIRE p.product_id IS UNIQUE",
    "CREATE CONSTRAINT variant_key IF NOT EXISTS FOR (v:Variant) REQUIRE v.variant_key IS UNIQUE",
    "CREATE CONSTRAINT offer_id IF NOT EXISTS FOR (o:Offer) REQUIRE o.offer_id IS UNIQUE",
    "CREATE CONSTRAINT source_url IF NOT EXISTS FOR (s:Source) REQUIRE s.url IS UNIQUE",
    "CREATE CONSTRAINT merchant_name IF NOT EXISTS FOR (m:Merchant) REQUIRE m.name IS UNIQUE",
    "CREATE CONSTRAINT spec_id IF NOT EXISTS FOR (s:Spec) REQUIRE s.spec_id IS UNIQUE",
    "CREATE CONSTRAINT review_id IF NOT EXISTS FOR (r:Review) REQUIRE r.review_id IS UNIQUE",
    "CREATE CONSTRAINT manifest_id IF NOT EXISTS FOR (m:SnapshotManifest) REQUIRE m.snapshot_id IS UNIQUE",
    "CREATE INDEX offer_snapshot IF NOT EXISTS FOR (o:Offer) ON (o.snapshot_id)",
    "CREATE INDEX product_category IF NOT EXISTS FOR (p:Product) ON (p.category)",
]

OFFER_QUERY = """
UNWIND $rows AS row
MERGE (p:Product {product_id: row.product_id})
  SET p.name = row.name, p.brand = row.brand, p.category = row.category
MERGE (v:Variant {variant_key: row.variant_key})
  SET v.variant_id = row.variant_id, v.variant = row.variant, v.sku = row.sku, v.category = row.category,
      v.bundle_suspect = row.bundle_suspect, v.missing_specs = row.missing, v.conflicting_specs = row.conflicts,
      v += row.spec_values
MERGE (p)-[:HAS_VARIANT]->(v)
MERGE (o:Offer {offer_id: row.offer_id})
  SET o.price_minor = row.price_minor, o.currency = row.currency, o.available = row.available,
      o.collected_at = row.collected_at, o.source_updated_at = row.source_updated_at,
      o.snapshot_id = $snapshot, o.evidence_id = 'price:' + row.offer_id
MERGE (v)-[:HAS_OFFER]->(o)
MERGE (m:Merchant {name: row.store})
MERGE (o)-[:SOLD_BY]->(m)
MERGE (s:Source {url: row.source_url})
MERGE (o)-[:FROM_SOURCE]->(s)
WITH v, row
UNWIND row.graph_specs AS spec
MERGE (sp:Spec {spec_id: spec.key + '=' + spec.value}) SET sp.key = spec.key, sp.value = spec.value
MERGE (v)-[r:HAS_SPEC]->(sp) SET r.method = spec.method, r.confidence = spec.confidence, r.evidence = spec.evidence
"""

REVIEW_QUERY = """
UNWIND $rows AS row
MERGE (s:Source {url: row.source_url})
MERGE (r:Review {review_id: row.id})
  SET r.evidence_id = 'review:' + row.id, r.product_id = coalesce(row.product_id, ''),
      r.product_name = row.product_name, r.category = row.category, r.text = row.text,
      r.match_level = coalesce(row.match_level, 'unknown'), r.collected_at = row.collected_at,
      r.snapshot_id = $snapshot
MERGE (r)-[:FROM_SOURCE]->(s)
"""

# Reviews only name a model family ("AMD Radeon RX 9070 XT (board model unknown)"). They are linked to
# every product of that family with the match level kept on the relationship, so graph retrieval can
# reach them while nothing upgrades them to exact-SKU evidence. Brand-only reviews are not linked.
ABOUT_QUERY = """
UNWIND $links AS link
MATCH (r:Review {review_id: link.review_id}), (p:Product {product_id: link.product_id})
MERGE (r)-[a:ABOUT]->(p) SET a.match_level = link.match_level, a.basis = link.basis
"""


def review_links(corpus: LocalCorpus) -> list[dict]:
    import re
    products = {}
    for row in corpus.prices:
        products.setdefault(str(row.get("product_id") or row["id"]), row)
    links = []
    for review in corpus.reviews:
        level = review.get("match_level") or "unknown"
        if review.get("product_id"):
            links.append({"review_id": review["id"], "product_id": str(review["product_id"]), "match_level": level,
                          "basis": "explicit product id"})
            continue
        if level == "brand_only":
            continue
        family = re.sub(r"\(.*?\)", "", review.get("product_name") or "").casefold()
        tokens = [t for t in re.findall(r"[a-z0-9]+", family) if t not in ("amd", "intel", "nvidia")]
        if len(tokens) < 2:
            continue
        for pid, row in products.items():
            name = re.sub(r"[™®©\s]", "", row["name"]).casefold()
            if row["category"] == review.get("category") and all(t in name for t in tokens):
                links.append({"review_id": review["id"], "product_id": pid, "match_level": level,
                              "basis": "model family name match"})
    return links

DERIVED_EDGES = [
    """MATCH (c:Variant {category: 'cpu'})-[:HAS_SPEC]->(s:Spec {key: 'socket'})<-[:HAS_SPEC]-(b:Variant {category: 'motherboard'})
       MERGE (c)-[e:SOCKET_COMPATIBLE]->(b) SET e.socket = s.value, e.basis = 'shared socket spec'""",
    """MATCH (b:Variant {category: 'motherboard'})-[:HAS_SPEC]->(s:Spec {key: 'memory_type'})<-[:HAS_SPEC]-(r:Variant {category: 'ram'})
       MERGE (b)-[e:MEMORY_COMPATIBLE]->(r) SET e.memory_type = s.value, e.basis = 'shared memory generation'""",
]


def offer_rows(corpus: LocalCorpus) -> list[dict]:
    rows = []
    for row in corpus.prices:
        meta = corpus.spec_meta.get(row["id"], {})
        specs = meta.get("specs", {})
        values = {k: v["value"] for k, v in specs.items() if v}
        graph_specs = [{"key": k, "value": str(v["value"]), "method": v["method"], "confidence": v["confidence"],
                        "evidence": v["evidence"]} for k, v in specs.items() if v and k in GRAPH_SPEC_KEYS]
        rows.append({"product_id": str(row.get("product_id") or row["id"]), "name": row["name"], "brand": row.get("brand"),
                     "category": row["category"], "variant_key": f"{row['store']}:{row.get('variant_id') or row['id']}",
                     "variant_id": str(row.get("variant_id") or ""), "variant": row.get("variant"), "sku": row.get("sku"),
                     "offer_id": row["id"], "price_minor": int(round(float(row["price"]) * 100)),
                     "currency": row.get("currency"), "available": bool(row.get("available")),
                     "collected_at": row.get("collected_at"), "source_updated_at": row.get("source_updated_at"),
                     "store": row["store"], "source_url": row["source_url"],
                     "bundle_suspect": bool(meta.get("flags", {}).get("bundle_suspect")),
                     "missing": meta.get("flags", {}).get("missing", []),
                     "conflicts": list(meta.get("flags", {}).get("conflict", {})),
                     "spec_values": {f"spec_{k}": v for k, v in values.items()}, "graph_specs": graph_specs})
    return rows


def index_neo4j(corpus: LocalCorpus, driver) -> dict:
    db = settings.neo4j_database
    for statement in CONSTRAINTS:
        driver.execute_query(statement, database_=db)
    rows = offer_rows(corpus)
    for start in range(0, len(rows), 300):
        driver.execute_query(OFFER_QUERY, rows=rows[start:start + 300], snapshot=corpus.snapshot_id, database_=db)
    driver.execute_query(REVIEW_QUERY, rows=corpus.reviews, snapshot=corpus.snapshot_id, database_=db)
    driver.execute_query(ABOUT_QUERY, links=review_links(corpus), database_=db)
    for statement in DERIVED_EDGES:
        driver.execute_query(statement, database_=db)
    counts = neo4j_counts(driver, corpus.snapshot_id)
    print(f"Neo4j: {counts}", flush=True)
    return counts


def neo4j_counts(driver, snapshot_id: str) -> dict:
    query = """
    CALL () { MATCH (o:Offer {snapshot_id: $sid}) RETURN count(o) AS offers }
    CALL () { MATCH (r:Review {snapshot_id: $sid}) RETURN count(r) AS reviews }
    CALL () { MATCH (p:Product) RETURN count(p) AS products }
    CALL () { MATCH (v:Variant) RETURN count(v) AS variants }
    CALL () { MATCH (s:Spec) RETURN count(s) AS spec_nodes }
    CALL () { MATCH ()-[e:SOCKET_COMPATIBLE]->() RETURN count(e) AS socket_edges }
    CALL () { MATCH ()-[e:MEMORY_COMPATIBLE]->() RETURN count(e) AS memory_edges }
    CALL () { MATCH ()-[e:ABOUT]->() RETURN count(e) AS review_links }
    RETURN offers, reviews, products, variants, spec_nodes, socket_edges, memory_edges, review_links
    """
    records, _, _ = driver.execute_query(query, sid=snapshot_id, database_=settings.neo4j_database)
    return dict(records[0])


# ----------------------------------------------------------------------------------- Milvus

def create_collection(client, name: str, dims: int):
    from pymilvus import DataType, Function, FunctionType
    if client.has_collection(name):
        client.drop_collection(name)
    schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
    schema.add_field("chunk_id", DataType.VARCHAR, is_primary=True, max_length=128)
    for field, length in (("evidence_id", 96), ("product_id", 64), ("category", 32), ("kind", 16),
                          ("match_level", 32), ("snapshot_id", 40), ("embedding_model", 64)):
        schema.add_field(field, DataType.VARCHAR, max_length=length)
    schema.add_field("source_url", DataType.VARCHAR, max_length=2048)
    schema.add_field("text", DataType.VARCHAR, max_length=8192, enable_analyzer=True)
    schema.add_field("dense", DataType.FLOAT_VECTOR, dim=dims)
    schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)
    schema.add_function(Function(name="bm25", function_type=FunctionType.BM25,
                                 input_field_names=["text"], output_field_names=["sparse"]))
    index = client.prepare_index_params()
    index.add_index("dense", index_type="HNSW", metric_type="COSINE", params={"M": 16, "efConstruction": 200})
    index.add_index("sparse", index_type="SPARSE_INVERTED_INDEX", metric_type="BM25")
    client.create_collection(name, schema=schema, index_params=index)


def index_milvus(corpus: LocalCorpus, client) -> dict:
    from backend.production_retrieval import create_embeddings
    embeddings = create_embeddings(settings)
    name = settings.milvus_collection
    create_collection(client, name, settings.embedding_dimensions)
    docs = corpus.documents
    batch = max(1, settings.embedding_batch_size)
    started = time.time()
    for start in range(0, len(docs), batch):
        chunk = docs[start:start + batch]
        vectors = embeddings.embed_documents([d.page_content for d in chunk])
        client.insert(name, [{**{k: str(d.metadata.get(k) or "") for k in
                                 ("chunk_id", "evidence_id", "product_id", "category", "kind", "match_level",
                                  "snapshot_id", "source_url")},
                              "embedding_model": settings.embedding_model or settings.embedding_provider,
                              "text": d.page_content[:8000], "dense": v} for d, v in zip(chunk, vectors)])
        done = start + len(chunk)
        if done % (batch * 20) == 0 or done == len(docs):
            print(f"Milvus: {done}/{len(docs)} chunks embedded ({time.time() - started:.0f}s)", flush=True)
    client.flush(name)
    client.load_collection(name)
    count = client.query(name, filter="", output_fields=["count(*)"])[0]["count(*)"]
    return {"milvus_collection": name, "milvus_chunks": count, "milvus_expected": len(docs)}


# ----------------------------------------------------------------------------------- publish

def index_current_snapshot(neo4j_only: bool = False, milvus_only: bool = False) -> dict:
    corpus = LocalCorpus(settings.data_dir)
    driver = neo4j_driver()
    result = {"snapshot_id": corpus.snapshot_id}
    set_manifest(driver, corpus.snapshot_id, status="staged", embedding_model=settings.embedding_model,
                 embedding_dimensions=settings.embedding_dimensions, milvus_collection=settings.milvus_collection)
    if not milvus_only:
        result["neo4j"] = index_neo4j(corpus, driver)
    if not neo4j_only:
        result["milvus"] = index_milvus(corpus, milvus_client())
    # Verify both stores against the snapshot, including the one that was not rebuilt this time.
    neo = result.get("neo4j") or neo4j_counts(driver, corpus.snapshot_id)
    client = milvus_client()
    chunks = client.query(settings.milvus_collection, filter=f'snapshot_id == "{corpus.snapshot_id}"',
                          output_fields=["count(*)"])[0]["count(*)"] if client.has_collection(settings.milvus_collection) else 0
    ok = neo["offers"] == len(corpus.prices) and neo["reviews"] == len(corpus.reviews) and chunks == len(corpus.documents)
    set_manifest(driver, corpus.snapshot_id, status="published" if ok else "staged",
                 offers=neo["offers"], reviews=neo["reviews"], chunks=chunks)
    result["published"] = ok
    driver.close()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Index the pinned snapshot into Neo4j and Milvus")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--neo4j-only", action="store_true")
    group.add_argument("--milvus-only", action="store_true")
    args = parser.parse_args()
    print(json.dumps(index_current_snapshot(neo4j_only=args.neo4j_only, milvus_only=args.milvus_only), indent=2))
