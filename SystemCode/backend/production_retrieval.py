"""Neo4j + Milvus production adapter.

Imports are lazy so the API can still start in local development mode. Milvus
provides dense + BM25 retrieval; Neo4j provides graph/structured retrieval.
The application combines all three ranked lists with RRF.
"""
import json
from typing import Any
from langchain_core.embeddings import Embeddings
from backend.retrieval import LocalCorpus, vector


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


class Neo4jMilvusCorpus(LocalCorpus):
    def __init__(self, data_dir, settings):
        super().__init__(data_dir)
        if not settings.neo4j_password:
            raise RuntimeError("NEO4J_PASSWORD is required for neo4j_milvus mode")
        try:
            from neo4j import GraphDatabase
            from langchain_milvus import Milvus, BM25BuiltInFunction
        except ImportError as exc:
            raise RuntimeError("Install neo4j, pymilvus and langchain-milvus for neo4j_milvus mode") from exc
        self.settings = settings
        self.driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
        self.driver.verify_connectivity()
        connection_args = {"uri": settings.milvus_uri}
        if settings.milvus_token:
            connection_args["token"] = settings.milvus_token
        elif settings.milvus_user:
            connection_args["user"] = settings.milvus_user
            connection_args["password"] = settings.milvus_password
        if settings.milvus_database:
            connection_args["db_name"] = settings.milvus_database
        self.vector_store = Milvus(
            embedding_function=create_embeddings(settings),
            builtin_function=BM25BuiltInFunction(output_field_names="sparse"),
            vector_field=["dense", "sparse"],
            text_field="text",
            collection_name=settings.milvus_collection,
            connection_args=connection_args,
            consistency_level="Bounded",
        )

    def candidates(self, category: str, maximum_minor: int | None = None, limit: int = 25) -> list[dict]:
        query = """
        MATCH (p:Product)-[:HAS_OFFER]->(o:Offer)-[:FROM_SOURCE]->(s:Source)
        WHERE p.category = $category AND o.snapshot_id = $snapshot
          AND o.available = true AND ($maximum IS NULL OR o.price_minor <= $maximum)
        RETURN p.product_id AS product_id, p.name AS name, p.brand AS brand,
               p.category AS category, o.offer_id AS id, o.variant_id AS variant_id,
               toString(o.price_minor / 100.0) AS price, o.currency AS currency,
               o.available AS available, s.name AS store, s.url AS source_url,
               o.collected_at AS collected_at
        ORDER BY o.price_minor ASC LIMIT $limit
        """
        records, _, _ = self.driver.execute_query(
            query, category=category, snapshot=self.snapshot_id,
            maximum=maximum_minor, limit=limit,
            database_=self.settings.neo4j_database,
        )
        return [dict(record) for record in records]

    def search(self, query: str, categories=None, product_ids=None, top_k: int = 8) -> dict[str, Any]:
        # Milvus performs its internal dense + sparse RRF. We preserve route
        # provenance at the application boundary and then fuse graph results.
        expression = f'snapshot_id == "{self.snapshot_id}"'
        if categories:
            expression += f" and category in {json.dumps(categories)}"
        if product_ids:
            expression += f" and product_id in {json.dumps(product_ids)}"
        docs = self.vector_store.similarity_search(
            query, k=top_k * 2, expr=expression, ranker_type="rrf", ranker_params={"k": 60})
        milvus_hits = [(d.metadata.get("evidence_id"), d) for d in docs
                       if (not categories or d.metadata.get("category") in categories)
                       and (not product_ids or d.metadata.get("product_id") in product_ids)]
        graph_query = """
        CALL () {
          MATCH (p:Product)-[:HAS_OFFER]->(e:Offer)-[:FROM_SOURCE]->(s:Source)
          WHERE e.snapshot_id = $snapshot
            AND (size($categories)=0 OR p.category IN $categories)
            AND (size($product_ids)=0 OR p.product_id IN $product_ids)
          RETURN e.evidence_id AS evidence_id, p.product_id AS product_id,
                 p.category AS category, p.name AS excerpt, s.url AS source_url,
                 'offer' AS kind
          UNION ALL
          MATCH (e:Review)-[:FROM_SOURCE]->(s:Source)
          WHERE e.snapshot_id = $snapshot
            AND (size($categories)=0 OR e.category IN $categories)
            AND (size($product_ids)=0 OR e.product_id IN $product_ids
                 OR e.product_id IS NULL OR e.product_id = '')
          RETURN e.evidence_id AS evidence_id, e.product_id AS product_id,
                 e.category AS category, e.text AS excerpt, s.url AS source_url,
                 'review' AS kind
        }
        RETURN evidence_id, product_id, category, excerpt, source_url, kind
        LIMIT $limit
        """
        rows, _, _ = self.driver.execute_query(
            graph_query, snapshot=self.snapshot_id,
            categories=categories or [], product_ids=product_ids or [], limit=top_k * 2,
            database_=self.settings.neo4j_database,
        )
        fused: dict[str, dict] = {}
        for rank, (eid, doc) in enumerate(milvus_hits, 1):
            if not eid:
                continue
            fused[eid] = {"evidence_id": eid, "score": 1 / (60 + rank), "route_ranks": {"bm25_vector": rank},
                          "kind": doc.metadata.get("kind"), "product_id": doc.metadata.get("product_id"),
                          "category": doc.metadata.get("category"), "source_url": doc.metadata.get("source_url"),
                          "excerpt": doc.page_content[:700]}
        for rank, row in enumerate(rows, 1):
            eid = row["evidence_id"]
            hit = fused.setdefault(eid, {"evidence_id": eid, "score": 0, "route_ranks": {}, "kind": row["kind"],
                                         "product_id": row["product_id"], "category": row["category"],
                                         "source_url": row["source_url"], "excerpt": row["excerpt"]})
            hit["score"] += 1 / (60 + rank); hit["route_ranks"]["graph"] = rank
        items = sorted(fused.values(), key=lambda x: x["score"], reverse=True)[:top_k]
        return {"snapshot_id": self.snapshot_id, "retrieval_backend": "neo4j_milvus", "items": items}
