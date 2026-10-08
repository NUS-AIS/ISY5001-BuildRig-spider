# BuildRig system architecture

This document describes the system as implemented. The knowledge base (Neo4j schema, Milvus
collection, retrieval routes, publishing protocol) is detailed in `knowledge-base.md`.

## 1. Overview

```text
            offline                                         online
 shops --> crawler --> spec extraction --> Neo4j + Milvus      user --> Vue app --> FastAPI
                       (regex / rules /    (published           |
                        evidence-checked    snapshot)            v
                        LLM, quality flags)                 requirement understanding (LLM + rules)
                                                                 |
                                                   +-------------+--------------+
                                                   | DAG workflow (default)     |  Pi runtime (fallback / option)
                                                   | planner -> desktop/laptop  |  supervisor with 6 tools
                                                   | -> evidence || validation  |  behind a server-side gate
                                                   | -> review -> replan (<=3)  |
                                                   | -> explanation + guard     |
                                                   +-------------+--------------+
                                                                 v
                                               options with prices, sources, checks, evidence
```

| Layer | Implementation | Proposal requirement |
|---|---|---|
| Collection | `crawler/collect.py`: Shopify JSON, robots.txt, per-store rate limits, raw responses archived per snapshot | FR01 |
| Data quality | `backend/ingest/specs.py`: three extraction tiers, missing / conflict / duplicate / bundle flags | FR02 |
| Knowledge base | `backend/index_data.py`, `backend/production_retrieval.py` (see `knowledge-base.md`) | FR03, FR04 |
| Understanding | `backend/requirements_parser.py`: structured LLM extraction, rule fallback, clarification, routing | FR07 |
| Planning and replanning | `backend/planning.py`, `backend/agents.py`, `backend/orchestrator.py` | FR05, FR06 |
| Validation | `backend/validation.py`: deterministic rules | FR05 |
| Follow-up | `backend/revision.py`: requirement diff and reuse of the previous run | FR06, FR08 |
| Presentation | `frontend/`: checks with reasons, sources and dates, evidence, revision timeline, compare view, lock and update | FR08 |

## 2. Design principles

1. **Facts come from data and rules; language comes from the model.** Prices, totals and compatibility
   verdicts are computed deterministically. The model interprets requests, chooses among tool-provided
   options and writes explanations, and every model output is validated by the server.
2. **Unknown is not passed.** A rule that lacks a specification returns `unknown` with a reason.
3. **Hard constraints are never relaxed silently.** An unsolvable request ends as `no_feasible_option`
   with the failed checks, or with a clarification question.
4. **Everything is traceable.** Each item carries its offer id, merchant, source URL and collection time;
   each evidence item carries its route ranks and match level; each run records its plan, agent trace,
   tool calls, model usage and limitations.

## 3. Requirement understanding (FR07)

Rules (regex) always run and remain the authority for the budget. When a model is configured, a
structured extraction (`RequirementExtraction`) adds free-text workloads, preferences, parts to buy and
parts already owned; its numbers are accepted only when they occur in the message, and part names only
when they contain a model number and come from the message. Parts are resolved against the catalogue:
a unique match is locked, several matches produce a clarification question. "Keep the X" refers to the
previously recommended X when the session has one. Clarification is asked when the device type or
budget is missing, a budget is implausibly low, or the model's budget disagrees with the rules.
`device_type = compare` runs both branches.

## 4. DAG workflow (FR05, FR06)

| Step | Agent | What it decides | Model use |
|---|---|---|---|
| Plan | Planner | branches (desktop, laptop or both) and task graph | optional, allow-listed |
| Select | Desktop Planner | parts along the compatibility chain CPU -> board -> RAM -> GPU -> PSU -> case -> SSD -> cooler; budget shares per workload profile; integrated graphics for everyday use | chooses from tool shortlists; choices outside them are rejected |
| Select | Laptop Selector | ranks in-stock laptops meeting the hard constraints | ranking with reasons |
| Evidence | Evidence Agent | writes search queries, runs three-route retrieval for the option's products | query writing |
| Validate | rule engine (tool) | budget, stock, completeness, locked parts, bundles, socket, memory, PSU headroom, GPU clearance, form factor, display output, minimum memory and storage | none |
| Review | Review Agent | `pass`, `revise` (with categories) or `insufficient_information`; cannot pass a failed check; may request one preference-driven revision | preference review |
| Replan | `planning.revise` | re-selects only the categories named by failed checks; keeps locked and owned parts; excludes offers already tried; an overspend is fixed only with strictly cheaper parts | via the same chooser |
| Explain | Explanation Agent | title, reasons, trade-offs from the verified facts | writing, then filtered by `explanation_guard` |

Evidence retrieval and validation run in parallel. The loop stops after `BUILDRIG_MAX_REVISIONS` rounds
(default 3). If every option is still failing and a Pi runtime is configured, the run is handed to Pi.

## 5. Pi runtime

`pi-worker/` runs a Pi Agent Core supervisor per device type. The supervisor chooses its own next action
with six tools served by token-protected internal endpoints:

| Tool | Endpoint | Purpose |
|---|---|---|
| `draft_build` | `/internal/options/draft` | deterministic first draft to review and repair |
| `find_parts` | `/internal/candidates` | Neo4j candidates with spec filters |
| `select_part` | worker state | puts an offer returned by `find_parts` into the build; locked parts cannot be replaced |
| `check_build` | `/internal/options/assemble` | server-side assembly and validation |
| `find_evidence` | `/internal/retrieval/hybrid` | three-route retrieval |
| `submit_build` | `/internal/options/assemble` | rejected while any check fails |

The build is kept in worker state rather than in the model context, and each tool response states what is
still missing and which filters the next part needs. This made multi-step tool use reliable for an 8B model.

## 6. Follow-up adjustments (FR06, FR08)

A run may name a `base_run_id` from the same session. `revision.py` compares the two requirement versions:
a changed device type or a budget raised by more than 15 % triggers a fresh plan; otherwise the previous
options are carried over (newly locked or owned parts inserted), and validation plus the replanner change
only what the new requirements break. Results list the changes and which parts were kept.

## 7. Degradation and safety

- A failed retrieval route, a failed Neo4j candidate query or an unavailable model is reported in the
  run's `limitations`; deterministic fallbacks keep the run working.
- Retrieved text is evidence only. Explanations are filtered for sentences naming models outside the
  option or contradicting its graphics setup; prices and parts never come from model text.
- Internal endpoints require `BUILDRIG_INTERNAL_API_TOKEN`; user endpoints are scoped by `X-User-ID`
  (supplied by an authenticating gateway in production; the prototype login is local only).

## 8. State and memory

SQLite (`runtime/buildrig.db`) stores sessions, versioned requirements, runs, events (served as SSE),
checkpoints, a tool-call ledger with replay policies, model usage, and confirmed user memories. Memories
are opt-in per session, versioned and owner-scoped; confirmed memories are injected into the evidence
queries.

## 9. API

| Endpoint | Purpose |
|---|---|
| `POST /api/v1/sessions`, `GET /api/v1/sessions/{id}` | sessions |
| `POST /api/v1/sessions/{id}/messages` | parse a message into a new requirements version; returns questions |
| `POST /api/v1/sessions/{id}/runs` | start a recommendation (`orchestration_mode` dag or pi, optional `base_run_id`) |
| `GET /api/v1/runs/{id}`, `/events`, `/result`, `/execution`; `POST .../cancel` | run status, SSE events, result, trace |
| `GET/POST /api/v1/sessions/{id}/memories`, `PATCH/DELETE /api/v1/memories/{id}` | user memories |
| `POST /api/v1/internal/*` | service endpoints for the Pi runtime |
| `GET /api/v1/health` | backend, published snapshot, models, recent degradations |
