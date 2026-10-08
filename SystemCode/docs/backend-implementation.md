# BuildRig Backend Implementation

> **Superseded.** This page describes the prototype as of 2026-09-20. The current implementation is documented in `system-architecture.md` and `knowledge-base.md`.

## Current implementation

The FastAPI application now contains a runnable multi-agent recommendation path rather than only crawler endpoints.

| Area | Implementation |
|---|---|
| API boundary | Versioned `/api/v1` sessions, messages, runs, SSE, results, execution inspection and memory endpoints |
| Requirement agent | LangChain Runnable extracts device type, SGD budget, workloads and hard constraints; asks for missing essentials |
| Planner agent | Produces a bounded plan; optionally uses a configured LangChain chat model with Pydantic structured output |
| Specialist agents | Separate Desktop Planner, Laptop Selector, Evidence and Review agents |
| Tool harness | Allowlisted tools, per-run call budget and durable intent/effect/settlement records |
| Run harness | Complete restart checkpoint after each agent transition, event stream and usage ledger |
| Retrieval | Local contract implementation for immediate testing; production adapter for Milvus dense+BM25 and Neo4j graph retrieval |
| Validation | Decimal budget, configuration completeness and explicit unknown compatibility checks |
| Memory | Opt-in confirmed memories, optimistic version updates, owner isolation and run-time context injection |
| Pi Agent | FastAPI-to-Pi runtime adapter plus internal retrieval/validation APIs; the external Node worker URL is configured explicitly |

## Agent contract

Every agent receives an `AgentContext` containing a fixed requirements version, fixed data snapshot, confirmed memory bundle and Tool Harness. An agent cannot open files, issue arbitrary Cypher, access Milvus directly or write memory. It returns a JSON-serializable artifact. The coordinator writes that artifact into the durable operation checkpoint before starting the next dependent step.

The current DAG is:

1. Planner Agent validates and versions the task plan.
2. Exactly one planning branch activates: Desktop Planner or Laptop Selector.
3. Evidence Agent invokes three-route retrieval for each option.
4. Review Agent invokes deterministic validation and preserves unknown checks.
5. Coordinator publishes only non-failed options and attaches the plan, agent trace and tool trace.

## Harness invariants

- IDs are sortable UUIDv7 values.
- A run is pinned to one requirement version and one corpus snapshot.
- Tool execution persists intent before the effect and settles the result afterward.
- Read tools use replay policy `safe`; future external mutations must use `never`.
- Startup marks abandoned `effect_pending` calls as interrupted rather than successful.
- Agent and tool budgets bound autonomous execution.
- State transitions replace one complete restart state; consumers do not infer state from missing records.
- The usage ledger is append-only and separate from mutable operation state.
- User and internal-service boundaries are checked before data access.
- A model-proposed plan is accepted only when its roles and tasks are subsets of the server allowlist.

## Development and production modes

`BUILDRIG_RETRIEVAL_BACKEND=local` is intentionally a degraded development mode. It executes BM25, deterministic vector similarity and graph-contract ranking over the current JSONL snapshot so API and agent tests work without infrastructure. It does not claim to be the production Neo4j/Milvus system.

Production mode requires indexing with `python -m backend.index_data`, setting Neo4j and Milvus credentials, and starting with `BUILDRIG_RETRIEVAL_BACKEND=neo4j_milvus`. The health endpoint reports the active mode.

`orchestration_mode=pi` never silently falls back to DAG. It requires `BUILDRIG_PI_RUNTIME_URL`. Pi remains a separate Node.js Agent Harness worker because its official runtime is TypeScript; FastAPI and LangChain remain the business and retrieval backend.

## Known next production steps

- Replace the deterministic development embedding with a selected, evaluated embedding model and rebuild Milvus.
- Deploy a shared queue and worker lease before running multiple API replicas. SQLite is limited to the single-host prototype.
- Add the Pi Node worker deployment once a model provider and its credentials are selected.
- Extend structured specifications before changing compatibility checks from unknown to passed.
- Add labelled retrieval and recommendation evaluation sets; do not tune RRF parameters on anecdotal examples.
