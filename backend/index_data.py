"""Index the current data snapshot into Neo4j and Milvus."""
import argparse
from backend.production_retrieval import create_embeddings
from backend.retrieval import LocalCorpus
from backend.settings import settings


def index_neo4j(corpus: LocalCorpus) -> dict:
    if not settings.neo4j_password:
        raise SystemExit("Set NEO4J_PASSWORD before indexing")
    from neo4j import GraphDatabase
    driver = GraphDatabase.driver(settings.neo4j_uri, auth=(settings.neo4j_user, settings.neo4j_password))
    driver.verify_connectivity()
    with driver.session(database=settings.neo4j_database) as session:
        session.run("CREATE CONSTRAINT product_id IF NOT EXISTS FOR (p:Product) REQUIRE p.product_id IS UNIQUE")
        session.run("CREATE CONSTRAINT source_url IF NOT EXISTS FOR (s:Source) REQUIRE s.url IS UNIQUE")
        session.run("CREATE CONSTRAINT offer_id IF NOT EXISTS FOR (o:Offer) REQUIRE o.offer_id IS UNIQUE")
        session.run("CREATE CONSTRAINT review_id IF NOT EXISTS FOR (r:Review) REQUIRE r.review_id IS UNIQUE")
        query = """
        UNWIND $rows AS row
        MERGE (p:Product {product_id: toString(row.product_id)})
          SET p.name=row.name, p.brand=row.brand, p.category=row.category
        MERGE (s:Source {url: row.source_url}) SET s.name=row.store
        MERGE (o:Offer {offer_id: row.id})
          SET o.price_minor=toInteger(toFloat(row.price)*100), o.currency=row.currency,
              o.available=row.available, o.collected_at=row.collected_at,
              o.snapshot_id=$snapshot, o.variant_id=toString(row.variant_id),
              o.evidence_id='price:'+row.id
        MERGE (p)-[:HAS_OFFER]->(o) MERGE (o)-[:FROM_SOURCE]->(s)
        """
        for start in range(0, len(corpus.prices), 500):
            session.run(query, rows=corpus.prices[start:start + 500], snapshot=corpus.snapshot_id)
        review_query = """
        UNWIND $rows AS row
        MERGE (s:Source {url: row.source_url})
          SET s.name=coalesce(row.extraction, 'review_source')
        MERGE (r:Review {review_id: row.id})
          SET r.evidence_id='review:'+row.id, r.product_id=coalesce(row.product_id, ''),
              r.product_name=row.product_name, r.category=row.category,
              r.text=row.text, r.match_level=row.match_level,
              r.rating=row.rating, r.collected_at=row.collected_at,
              r.snapshot_id=$snapshot, r.limitations=row.limitations
        MERGE (r)-[:FROM_SOURCE]->(s)
        WITH row, r
        OPTIONAL MATCH (p:Product {product_id: toString(row.product_id)})
        FOREACH (_ IN CASE WHEN p IS NULL THEN [] ELSE [1] END |
          MERGE (r)-[:REVIEWS]->(p))
        """
        for start in range(0, len(corpus.reviews), 500):
            session.run(review_query, rows=corpus.reviews[start:start + 500], snapshot=corpus.snapshot_id)
    driver.close()
    return {"neo4j_records": len(corpus.prices) + len(corpus.reviews),
            "neo4j_offers": len(corpus.prices), "neo4j_reviews": len(corpus.reviews)}


def index_milvus(corpus: LocalCorpus) -> dict:
    import time
    from langchain_milvus import Milvus, BM25BuiltInFunction
    connection = {"uri": settings.milvus_uri}
    if settings.milvus_token:
        connection["token"] = settings.milvus_token
    elif settings.milvus_user:
        connection["user"] = settings.milvus_user
        connection["password"] = settings.milvus_password
    if settings.milvus_database:
        connection["db_name"] = settings.milvus_database
    def document_ids(rows):
        return [row.metadata["evidence_id"] for row in rows]

    batch_size = max(1, settings.embedding_batch_size)
    first = corpus.documents[:batch_size]
    store = Milvus.from_documents(
        first,
        embedding=create_embeddings(settings),
        builtin_function=BM25BuiltInFunction(output_field_names="sparse"),
        vector_field=["dense", "sparse"],
        text_field="text",
        collection_name=settings.milvus_collection,
        connection_args=connection,
        consistency_level="Bounded",
        drop_old=True,
        ids=document_ids(first),
    )
    print(f"Milvus indexed {len(first)}/{len(corpus.documents)} documents", flush=True)
    for start in range(batch_size, len(corpus.documents), batch_size):
        batch = corpus.documents[start:start + batch_size]
        for attempt in range(1, 4):
            try:
                store.add_documents(batch, ids=document_ids(batch))
                break
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(2 * attempt)
        print(f"Milvus indexed {start + len(batch)}/{len(corpus.documents)} documents", flush=True)
    return {"milvus_documents": len(corpus.documents), "collection": store.collection_name}


def index_current_snapshot(neo4j_only: bool = False, milvus_only: bool = False):
    corpus = LocalCorpus(settings.data_dir)
    result = {"snapshot_id": corpus.snapshot_id}
    if not milvus_only:
        result.update(index_neo4j(corpus))
    if not neo4j_only:
        result.update(index_milvus(corpus))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--neo4j-only", action="store_true")
    group.add_argument("--milvus-only", action="store_true")
    args = parser.parse_args()
    print(index_current_snapshot(neo4j_only=args.neo4j_only, milvus_only=args.milvus_only))
