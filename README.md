## SECTION 1 : PROJECT TITLE
## BuildRig - A RAG-Based Multi-Agent System for PC Build and Laptop Recommendations in Singapore

---

## SECTION 2 : EXECUTIVE SUMMARY / PAPER ABSTRACT

PC hardware prices in Singapore change quickly and many parts are out of stock, so a configuration found
online is often outdated, unavailable or incompatible. BuildRig is a conversational recommender that turns a
plain-language request ("a quiet gaming desktop under S$3,000 with 32GB RAM") into an in-stock, compatible
configuration priced from a pinned snapshot of two Singapore retailers, with every price, link and reason
traceable to its source and collection date.

The system combines:

- **Knowledge discovery** - a polite Shopify crawler, specification extraction in three tiers (regex, domain
  rules, and an LLM tier that must quote its evidence) and data-quality flags for missing or conflicting values.
- **Knowledge graph and hybrid RAG** - Neo4j (products, variants, offers, shared spec nodes, derived
  compatibility edges, family-level reviews) and Milvus (bge-m3 dense + BM25 sparse chunks), fused with
  Reciprocal Rank Fusion together with a graph route.
- **Decision automation** - a deterministic rule engine for budget, stock and compatibility that reports
  `passed`, `failed` or `unknown` with reasons; missing specifications are never treated as passed.
- **Multi-agent reasoning** - a DAG of specialist agents (planner, desktop planner, laptop selector, evidence,
  review, explanation) running a Plan -> Execute -> Review -> Replan loop, with a Pi Agent Core supervisor that
  takes over when the DAG's revision budget is exhausted. Follow-up messages reuse the previous run and change
  only the parts a new requirement affects.

All models run locally through Ollama (qwen3:8b for chat, bge-m3 for embeddings). The evaluation suite
covers end-to-end quality, a multi-agent vs single-agent comparison, a retrieval ablation, orchestration and
fault recovery, data quality and cross-store consistency (`SystemCode/evaluation/results/summary.md`).

---

## SECTION 3 : CREDITS / PROJECT CONTRIBUTION

| Official Full Name | Student ID | Work Items (Who Did What) | Email (Optional) |
| :------------ |:---------------:| :-----| :-----|
| Lai Wendi | A0352629X | *to be completed by the team* | |
| Tang Shiyan | A0329902B | *to be completed by the team* | |
| Xia Yan | A0352818X | *to be completed by the team* | |
| Zhou Jiasheng | A0315819X | *to be completed by the team* | |

---

## SECTION 4 : VIDEO OF SYSTEM MODELLING & USE CASE DEMO

*Links to the promotion video and the system design video will be added here (see `Video/`).*

---

## SECTION 5 : USER GUIDE

All commands run from `SystemCode/` unless stated otherwise.

### [ 1 ] Requirements

| Tool | Version | Notes |
|---|---|---|
| Docker Desktop | 4.x with Compose v2 | Runs Neo4j, Milvus and Ollama. On Windows use the WSL2 backend for GPU support. |
| Python | 3.12 | Backend, crawler, ingestion, evaluation |
| Node.js | 22.19 or newer | Vue front end and the Pi worker |
| NVIDIA GPU (recommended) | 8 GB VRAM | qwen3:8b with a 12k context fits an RTX 4060 Laptop GPU. Without a GPU the system works but is slow. |

### [ 2 ] Start the infrastructure

```bash
cp .env.example .env            # Windows: copy .env.example .env  (then set BUILDRIG_INTERNAL_API_TOKEN to a random string)
docker compose up -d            # Neo4j, Milvus, Ollama; the first start downloads qwen3:8b and bge-m3 (~6 GB)
docker compose logs -f ollama-init   # wait until both models are pulled
```

On a machine without an NVIDIA GPU (e.g. macOS), start only the databases with
`docker compose up -d neo4j milvus`, install Ollama natively, run `ollama pull qwen3:8b` and
`ollama pull bge-m3`, and keep `OLLAMA_BASE_URL=http://127.0.0.1:11434` in `.env`. A native Ollama must also
serve the context length in `BUILDRIG_OLLAMA_NUM_CTX`: set the environment variable
`OLLAMA_CONTEXT_LENGTH=8192` for the Ollama server and restart it (on Windows, `start-buildrig.bat` does this).

To try the system without Docker at all, copy `.env.local.example` instead: it reads the snapshot files
directly (no Neo4j or Milvus), so skip step 4. Retrieval then runs on a simplified stand-in.

### [ 3 ] Install the application

```bash
python -m venv ../.venv
../.venv/Scripts/activate          # macOS/Linux: source ../.venv/bin/activate
pip install -r requirements.txt
cd frontend && npm install && cd ..
cd pi-worker && npm install && npx tsc -p tsconfig.json && cd ..
```

### [ 4 ] Load the knowledge base

The repository ships a complete catalogue snapshot (`data/runs/20260906T124528641415Z`, pinned by
`data/latest.json`) including extracted specifications, so no crawling is needed to run the system.

```bash
python -m backend.index_data          # Neo4j graph + Milvus chunks; marks the snapshot "published" (~2 min)
python -m backend.check_consistency   # expect "consistent": true
```

Optional - refresh the data (be patient: one store rate-limits to about one request per few minutes):

```bash
python -m crawler.collect --max-pages 10 --review-products 8 --review-import data/imports/review_samples.jsonl
python -m backend.ingest.specs        # specification extraction for the new snapshot
python -m backend.index_data
```

### [ 5 ] Run the system

Three terminals:

```bash
python -m uvicorn main:app --port 8000                     # API, docs at http://127.0.0.1:8000/docs
cd pi-worker && node --env-file=../.env dist/server.js     # Pi runtime on port 8090
cd frontend && npm run dev                                 # web app at http://localhost:5173
```

On Windows, `start-buildrig.bat` does all of this in one step: it starts Docker Desktop and the database
containers if `.env` selects them, restarts the three services in their own windows and opens the web app
(`start-buildrig.bat -Stop` stops them). In PowerShell, run the commands above one per line; `&&` needs
PowerShell 7.

Register any account on the login page (accounts are a local prototype stored in the browser), describe
what you need, then press **Generate recommendation**. Useful things to try:

- *"Gaming desktop for 1440p with 32GB RAM, budget S$3,000"* - then press the lock icon on a part and type
  *"Lower my budget to S$2,700"*; **Update recommendation** keeps unaffected parts and changes only what the
  new budget requires.
- *"Not sure whether to buy a desktop or laptop for video editing, budget S$2,800"* - side-by-side comparison.
- *"Quiet office desktop, budget S$1,200"* - the first draft exceeds the budget and the revision timeline shows
  the replanning step.
- Switch between **DAG workflow** and **Pi runtime** before generating.

`GET /api/v1/health` reports `ok` only when Neo4j and Milvus serve the published snapshot; otherwise it reports
`degraded` with the reason. Setting `BUILDRIG_RETRIEVAL_BACKEND=local` runs everything from the snapshot files
without the databases (development mode).

### [ 6 ] Tests and evaluation

```bash
python -m unittest discover -s tests -v       # unit and API tests (no model or database needed)
python -m evaluation.run_all                  # full evaluation, about 2 hours on an RTX 4060
python -m evaluation.report                   # rebuild figures and summary.md from saved results
```

---

## SECTION 6 : PROJECT REPORT / PAPER

*The group report is in `ProjectReport/`.* Technical references for the report:

- `SystemCode/docs/system-architecture.md` - architecture, agent workflows and memory design
- `SystemCode/docs/knowledge-base.md` - Neo4j schema, Milvus collection, retrieval and publishing protocol
- `SystemCode/evaluation/results/summary.md` and `figures/` - experiment results
- `Miscellaneous/screenshots/` - screenshots of the main user flows

### System overview

```text
SystemCode/
  crawler/            Shopify crawler (robots.txt, per-store rate limits, raw responses archived)
  backend/
    ingest/specs.py   specification extraction (regex / rules / evidence-checked LLM) + quality flags
    index_data.py     Neo4j graph and Milvus index for the pinned snapshot, publish manifest
    check_consistency.py  cross-store consistency report
    production_retrieval.py  Neo4j candidates + BM25 / dense / graph retrieval with RRF
    requirements_parser.py   LLM structured requirement extraction with rule fallback, clarification
    planning.py       compatibility-aware selection and targeted replanning
    validation.py     deterministic rule engine (passed / failed / unknown with reasons)
    agents.py, orchestrator.py  DAG specialist agents, Review -> Replan loop, Pi fallback
    revision.py       follow-up reuse of a previous run
    explanation_guard.py  filters explanation sentences that contradict the option
  pi-worker/          Pi Agent Core supervisor with six tools behind a server-side gate
  frontend/           Vue 3 web app
  evaluation/         test cases, labelled retrieval queries, experiments, results
  tests/              unit and API tests
  docker-compose.yml  Neo4j + Milvus + Ollama
```

---

## SECTION 7 : MISCELLANEOUS

- `Miscellaneous/proposal/` - project proposal drafts and supporting files
- `Miscellaneous/screenshots/` - UI screenshots produced by an automated browser walkthrough
