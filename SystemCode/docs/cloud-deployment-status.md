# Cloud deployment status

Verified on 2026-09-20:

- Neo4j Aura connectivity: passed.
- Neo4j indexed records: 2,251 (2,236 offers and 15 reviews).
- Zilliz Cloud connectivity: passed.
- Milvus collection: `buildrig_evidence_bge_m3_v1`.
- Milvus persisted rows: 1,120. The remaining source documents were intentionally not indexed at the user's request.
- Dense embedding model: local Ollama `bge-m3`, 1,024 dimensions.
- Sparse retrieval: Milvus BM25 built-in function.
- Final fusion: Milvus dense/BM25 RRF plus application-level Neo4j RRF.
- FastAPI health: `ok` with `neo4j_milvus` retrieval.
- DAG end-to-end cloud run: passed.
- Pi Agent Runtime end-to-end cloud run: passed.

Credentials and full cloud endpoints are stored only in the ignored `.env` file and are not recorded here.
