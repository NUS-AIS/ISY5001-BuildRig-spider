"""Neo4j + Milvus production adapter (FR04, FR05).

* Candidates come from Neo4j: in-stock offers of the pinned snapshot, filtered by the spec values
  stored on Variant nodes. Unknown specs are kept but ranked after known matches.
* Retrieval runs three independent routes and fuses them with application-level RRF:
    bm25  - Milvus sparse BM25 over chunk text (exact model names, "AM5", "DDR5")
    dense - Milvus HNSW over bge-m3 embeddings (meaning: "quiet", "portable")
    graph - Neo4j: family-level reviews ABOUT the products, compatibility facts between the
            products, and offers reached through shared Spec nodes
* A route that fails is skipped and reported in ``degraded_routes``; results are never presented
  as complete when a route was missing.

Imports are lazy so the API can still start in local development mode.
"""
import re
from typing import Any
from langchain_core.embeddings import Embeddings
from backend.retrieval import ROUTES, LocalCorpus, rrf_fuse, vector


class HashEmbeddings(Embeddings):
    """Deterministic development embedding; replace for production quality."""
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return vector(text)


class DimensionCheckedEmbeddings(Embeddings):
    """Fail early when a model and an existing Milvus schema disagree."""
    def __init__(self, delegate: Embeddings, expected_dimensions: int):
        self.delegate = delegate
        self.expected_dimensions = expected_dimensions

    def _check(self, rows: list[list[float]]) -> list[list[float]]:
        for row in rows:
            if len(row) != self.expected_dimensions:
                raise RuntimeError(
                    f"Embedding dimension mismatch: expected {self.expected_dimensions}, got {len(row)}. "
                    "Recreate the Milvus collection after changing embedding models."
                )
        return rows

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._check(self.delegate.embed_documents(texts))

    def embed_query(self, text: str) -> list[float]:
        return self._check([self.delegate.embed_query(text)])[0]


def create_embeddings(settings) -> Embeddings:
    """Build the configured embedding client for both indexing and querying."""
    provider = (settings.embedding_provider or "hash").lower()
    if provider == "ollama":
        if not settings.embedding_model:
            raise RuntimeError("BUILDRIG_EMBEDDING_MODEL is required for Ollama embeddings")
        try:
            from langchain_ollama import OllamaEmbeddings
        except ImportError as exc:
            raise RuntimeError("Install langchain-ollama to use Ollama embeddings") from exc
        delegate = OllamaEmbeddings(
            model=settings.embedding_model,
            base_url=settings.ollama_base_url,
        )
    elif provider == "openai":
        if not settings.embedding_model:
            raise RuntimeError("BUILDRIG_EMBEDDING_MODEL is required for OpenAI embeddings")
        from langchain_openai import OpenAIEmbeddings
        delegate = OpenAIEmbeddings(
            model=settings.embedding_model,
            dimensions=settings.embedding_dimensions,
        )
    elif provider == "hash":
        delegate = HashEmbeddings()
    else:
        raise RuntimeError(f"Unsupported embedding provider: {provider}")
    return DimensionCheckedEmbeddings(delegate, settings.embedding_dimensions)




CANDIDATE_QUERY = """
MATCH (p:Product {category: $category})-[:HAS_VARIANT]->(v:Variant)-[:HAS_OFFER]->(o:Offer {snapshot_id: $sid})
WHERE o.available AND NOT coalesce(v.bundle_suspect, false) AND NOT o.offer_id IN $exclude
  AND ($max IS NULL OR o.price_minor <= $max) AND ($min IS NULL OR o.price_minor >= $min)
  AND ALL(k IN keys($require) WHERE v['spec_' + k] IS NULL OR v['spec_' + k] = $require[k])
  AND ALL(k IN keys($minimum) WHERE v['spec_' + k] IS NULL OR v['spec_' + k] >= $minimum[k])
WITH o, size([k IN keys($require) WHERE v['spec_' + k] IS NULL])
      + size([k IN keys($minimum) WHERE v['spec_' + k] IS NULL]) AS unknowns
RETURN o.offer_id AS id
ORDER BY unknowns ASC, CASE WHEN $descending THEN -o.price_minor ELSE o.price_minor END ASC
LIMIT $limit
"""

GRAPH_PRODUCT_QUERY = """
CALL () {
  MATCH (r:Review)-[a:ABOUT]->(p:Product) WHERE p.product_id IN $pids AND r.snapshot_id = $sid
  RETURN r.evidence_id AS evidence_id, p.product_id AS product_id, p.category AS category,
         r.product_name + ': ' + r.text AS text, a.match_level AS match_level, 'review' AS kind,
         [(r)-[:FROM_SOURCE]->(s) | s.url][0] AS source_url, 0 AS priority
  UNION ALL
  MATCH (p1:Product)-[:HAS_VARIANT]->(v1:Variant)-[e:SOCKET_COMPATIBLE|MEMORY_COMPATIBLE]->(v2:Variant)<-[:HAS_VARIANT]-(p2:Product)
  WHERE p1.product_id IN $pids AND p2.product_id IN $pids
  RETURN 'graph:' + type(e) + ':' + p1.product_id + ':' + p2.product_id AS evidence_id, p1.product_id AS product_id,
         p1.category AS category,
         p1.name + ' and ' + p2.name + ' are linked by ' + toLower(type(e)) + ' (' + e.basis + ': ' +
         coalesce(e.socket, e.memory_type) + ').' AS text, 'derived_from_specs' AS match_level,
         'graph_fact' AS kind, [(v1)-[:HAS_OFFER]->(:Offer)-[:FROM_SOURCE]->(s) | s.url][0] AS source_url, 1 AS priority
  UNION ALL
  MATCH (p:Product)-[:HAS_VARIANT]->(v:Variant)-[:HAS_OFFER]->(o:Offer {snapshot_id: $sid})-[:FROM_SOURCE]->(s:Source)
  WHERE p.product_id IN $pids
  OPTIONAL MATCH (v)-[:HAS_SPEC]->(sp:Spec)
  WITH p, o, s, collect(sp.key + ' ' + sp.value) AS specs
  RETURN o.evidence_id AS evidence_id, p.product_id AS product_id, p.category AS category,
         p.name + '. Specifications: ' + reduce(t = '', x IN specs | t + x + '; ') AS text, 'exact_offer' AS match_level,
         'offer' AS kind, s.url AS source_url, 2 AS priority
}
RETURN evidence_id, product_id, category, text, match_level, kind, source_url
ORDER BY priority ASC LIMIT $limit
"""

GRAPH_SPEC_QUERY = """
MATCH (sp:Spec) WHERE toUpper(sp.value) IN $terms
MATCH (p:Product)-[:HAS_VARIANT]->(v:Variant)-[:HAS_SPEC]->(sp)
WHERE size($categories) = 0 OR p.category IN $categories
MATCH (v)-[:HAS_OFFER]->(o:Offer {snapshot_id: $sid})-[:FROM_SOURCE]->(s:Source)
WITH p, o, s, count(DISTINCT sp) AS matched, collect(DISTINCT sp.key + ' ' + sp.value) AS specs
RETURN o.evidence_id AS evidence_id, p.product_id AS product_id, p.category AS category,
       p.name + '. Matched specifications: ' + reduce(t = '', x IN specs | t + x + '; ') AS text,
       'exact_offer' AS match_level, 'offer' AS kind, s.url AS source_url
ORDER BY matched DESC, o.available DESC, o.price_minor ASC LIMIT $limit
"""

SPEC_TERM = re.compile(r"(?:RTX|RX|GTX|ARC)\s?[A-Z]?\d{3,4}(?:\s?(?:TI SUPER|TI|XTX|XT|GRE|SUPER))?|\b[A-Z]{2,4}\d{1,4}\b|\bDDR[45]\b", re.I)


class Neo4jMilvusCorpus(LocalCorpus):
    def __init__(self, data_dir, settings):
        super().__init__(data_dir)
        if not settings.neo4j_password:
            raise RuntimeError("NEO4J_PASSWORD is required for neo4j_milvus mode")
        try:
            from neo4j import GraphDatabase
            from backend.index_data import milvus_client
        except ImportError as exc:
            raise RuntimeError("Install neo4j and pymilvus for neo4j_milvus mode") from exc
        self.settings = settings
        self.driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
        self.driver.verify_connectivity()
        self.milvus = milvus_client()
        self.collection = settings.milvus_collection
        self.embeddings = create_embeddings(settings)
        self.events: list[dict] = []
        records, _, _ = self.driver.execute_query(
            "MATCH (m:SnapshotManifest {snapshot_id: $sid}) RETURN m.status AS status",
            sid=self.snapshot_id, database_=settings.neo4j_database)
        self.manifest_status = records[0]["status"] if records else "missing"

    def status(self) -> dict:
        return {"snapshot_id": self.snapshot_id, "manifest_status": self.manifest_status, "collection": self.collection}

    # ------------------------------------------------------------------ candidates (Neo4j)

    def candidates(self, category: str, maximum_minor: int | None = None, limit: int = 25,
                   minimum_minor: int | None = None, require: dict | None = None,
                   minimum_specs: dict | None = None, exclude_ids: list[str] | None = None,
                   order: str = "price_asc") -> list[dict]:
        try:
            records, _, _ = self.driver.execute_query(
                CANDIDATE_QUERY, category=category, sid=self.snapshot_id, exclude=list(exclude_ids or []),
                max=maximum_minor, min=minimum_minor, require=require or {}, minimum=minimum_specs or {},
                descending=order == "price_desc", limit=limit, database_=self.settings.neo4j_database)
            return [self.enrich(self.by_id[r["id"]]) for r in records if r["id"] in self.by_id]
        except Exception as exc:
            # The pinned snapshot is also on disk, so planning can continue; the event is reported.
            self.events.append({"route": "neo4j_candidates", "error": type(exc).__name__})
            return super().candidates(category, maximum_minor, limit, minimum_minor, require, minimum_specs,
                                      exclude_ids, order)

    # ------------------------------------------------------------------ retrieval routes

    def _filter(self, categories, product_ids) -> str:
        parts = [f'snapshot_id == "{self.snapshot_id}"']
        if categories:
            parts.append("category in [" + ", ".join(f'"{c}"' for c in categories) + "]")
        if product_ids:
            parts.append("product_id in [" + ", ".join(f'"{p}"' for p in product_ids) + "]")
        return " and ".join(parts)

    def _milvus(self, route: str, query: str, expr: str, limit: int) -> list[tuple[str, str, dict]]:
        fields = ["evidence_id", "product_id", "category", "kind", "match_level", "source_url", "text"]
        if route == "bm25":
            res = self.milvus.search(self.collection, data=[query], anns_field="sparse", limit=limit, filter=expr,
                                     output_fields=fields, search_params={"metric_type": "BM25"})
        else:
            res = self.milvus.search(self.collection, data=[self.embeddings.embed_query(query)], anns_field="dense",
                                     limit=limit, filter=expr, output_fields=fields,
                                     search_params={"metric_type": "COSINE", "params": {"ef": 64}})
        return [(h["entity"]["evidence_id"], h["entity"]["text"], h["entity"]) for h in res[0]]

    def _graph(self, query: str, categories, product_ids, limit: int) -> list[tuple[str, str, dict]]:
        db = self.settings.neo4j_database
        if product_ids:
            records, _, _ = self.driver.execute_query(GRAPH_PRODUCT_QUERY, pids=list(product_ids), sid=self.snapshot_id,
                                                      limit=limit, database_=db)
        else:
            terms = sorted({re.sub(r"\s+", " ", t.upper()) for t in SPEC_TERM.findall(query)})
            if not terms:
                return []
            records, _, _ = self.driver.execute_query(GRAPH_SPEC_QUERY, terms=terms, categories=list(categories or []),
                                                      sid=self.snapshot_id, limit=limit, database_=db)
        return [(r["evidence_id"], r["text"], dict(r)) for r in records]

    def search(self, query: str, categories: list[str] | None = None, product_ids: list[str] | None = None,
               top_k: int = 8, routes: list[str] | None = None) -> dict[str, Any]:
        routes = routes or list(ROUTES)
        expr, depth = self._filter(categories, product_ids), top_k * 3
        hits, degraded = {}, []
        for route in routes:
            try:
                hits[route] = (self._graph(query, categories, product_ids, depth) if route == "graph"
                               else self._milvus(route, query, expr, depth))
            except Exception as exc:
                degraded.append({"route": route, "error": type(exc).__name__})
        return {"snapshot_id": self.snapshot_id, "retrieval_backend": "neo4j_milvus", "routes": routes,
                "degraded_routes": degraded, "items": rrf_fuse(hits, top_k)}
