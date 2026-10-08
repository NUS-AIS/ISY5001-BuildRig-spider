# Knowledge base: Neo4j schema and Milvus collection

Both stores are built from one pinned snapshot (`data/latest.json`) by `python -m backend.index_data`
and verified by `python -m backend.check_consistency`.

## Neo4j graph

```text
(Product)-[:HAS_VARIANT]->(Variant)-[:HAS_OFFER]->(Offer)-[:SOLD_BY]->(Merchant)
                                                  (Offer)-[:FROM_SOURCE]->(Source)
(Variant)-[:HAS_SPEC {method, confidence, evidence}]->(Spec {key, value})
(Variant {category:'cpu'})-[:SOCKET_COMPATIBLE {socket, basis}]->(Variant {category:'motherboard'})
(Variant {category:'motherboard'})-[:MEMORY_COMPATIBLE {memory_type, basis}]->(Variant {category:'ram'})
(Review)-[:ABOUT {match_level, basis}]->(Product)
(Review)-[:FROM_SOURCE]->(Source)
(SnapshotManifest {snapshot_id, status, offers, reviews, chunks, embedding_model, ...})
```

| Node | Key | Main properties |
|---|---|---|
| `Product` | `product_id` | `name`, `brand`, `category` |
| `Variant` | `variant_key` (`store:variant_id`) | `variant`, `sku`, `category`, `spec_*` (extracted values for filtering), `bundle_suspect`, `missing_specs`, `conflicting_specs` |
| `Offer` | `offer_id` | `price_minor` (SGD cents), `currency`, `available`, `collected_at`, `snapshot_id`, `evidence_id` |
| `Merchant` | `name` | |
| `Source` | `url` | |
| `Spec` | `spec_id` (`key=value`) | `key`, `value`; shared by every variant with that value (e.g. one `socket=AM5` node) |
| `Review` | `review_id` | `text`, `product_name`, `match_level`, `snapshot_id`, `evidence_id` |
| `SnapshotManifest` | `snapshot_id` | `status` (`staged` / `published`), counts, embedding model and dimensions |

Design rules:

- Different configurations of a product are separate `Variant` and `Offer` nodes (FR03).
- `HAS_SPEC` records how a value was obtained (`regex`, `rule_inferred`, `llm`) and the quoted evidence.
- Compatibility edges are derived from shared `Spec` nodes and carry their basis; they are evidence
  for explanations, while the decision itself is made by `backend/validation.py`.
- Reviews in the snapshot name only a model family. `ABOUT` links them to the products of that
  family with `match_level = model_family_only`; brand-only reviews are not linked. Nothing turns
  family-level text into exact-SKU evidence.

## Milvus collection

Collection name: `MILVUS_COLLECTION` (default `buildrig_chunks_bge_m3_v2`).

| Field | Type | Notes |
|---|---|---|
| `chunk_id` | VARCHAR, primary key | `price:<offer_id>#<n>` or `review:<id>#<n>` |
| `evidence_id` | VARCHAR | Same id as `Offer.evidence_id` / `Review.evidence_id` in Neo4j |
| `product_id`, `category`, `kind`, `match_level` | VARCHAR | Filters and provenance |
| `snapshot_id`, `embedding_model` | VARCHAR | Every query filters on the pinned snapshot |
| `source_url` | VARCHAR | Link shown to the user |
| `text` | VARCHAR, analyzer enabled | Title + extracted specs + one chunk of the listing |
| `dense` | FLOAT_VECTOR(1024) | bge-m3 embedding, HNSW index, cosine |
| `sparse` | SPARSE_FLOAT_VECTOR | Produced by Milvus' built-in BM25 function from `text` |

Chunks never mix two listings; long descriptions are split into ~1,200-character chunks with
overlap, and every chunk repeats the title and the extracted specifications.

## Retrieval

`backend/production_retrieval.py` runs three independent routes and fuses their rankings with
Reciprocal Rank Fusion in the application (raw scores are never added):

| Route | Store | Finds |
|---|---|---|
| `bm25` | Milvus sparse | exact terms: model numbers, "AM5", "DDR5", software names |
| `dense` | Milvus dense | meaning: "quiet", "portable", "good for CAD" |
| `graph` | Neo4j | family reviews `ABOUT` the products, compatibility facts between them, offers reached through `Spec` nodes |

Candidate selection for planning also comes from Neo4j (`Variant.spec_*` filters on the pinned
snapshot). A failing route is skipped and reported in `degraded_routes`.

## Publishing and consistency

`index_data` marks the manifest `staged`, rebuilds both stores, verifies counts against the snapshot
files and only then marks it `published`. `check_consistency` writes
`data/runs/<snapshot>/consistency_report.json` with counts, orphan chunks, graph evidence without
chunks and chunks from other snapshots. `/api/v1/health` reports `degraded` unless the pinned
snapshot is published.
