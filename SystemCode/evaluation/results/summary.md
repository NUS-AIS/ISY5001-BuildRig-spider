# BuildRig evaluation summary

Snapshot `20260906T124528641415Z`, chat model `qwen3:8b`, embedding `bge-m3`, commit `9e36926`, max revisions 3.

## E1 End-to-end quality

| Metric | Rate | n |
|---|---|---|
| Task completed | 100% | 40 |
| Constraints met | 100% | 32 |
| Compatibility correct | 100% | 29 |
| Source accurate | 100% | 29 |
| Unknowns honest | 100% | 29 |
| Follow-up success | 100% | 5 |
| Explanation quality (LLM judge, 1-5) | 5.0 | |
| Explanation guard: sentences filtered / cases affected | 1 / 1 of 29 | |
| Mean latency (s) / tool calls / LLM calls / tokens | 40.8 / 20.5 / 7.4 / 8220.2 | |

Task completion by scenario: desktop 100%, laptop 100%, compare 100%, ambiguous 100%, low_budget 100%, locked 100%, follow_up 100%, data_gaps 100%

## E2 Multi-agent vs single agent

| Metric | Multi-agent (DAG) | Single agent |
|---|---|---|
| Task completed | 100% | 47% |
| Constraints met | 100% | 75% |
| Compatibility correct | 100% | 75% |
| Source accurate | 100% | 100% |
| Unknowns honest | 100% | 100% |
| Latency s | 35.1 | 53.9 |
| LLM calls | 6.3 | 12.9 |
| Tool calls | 19.1 | 15.4 |
| Tokens | 6795.2 | 73341.7 |

Token counts for the multi-agent system cover the recommendation run; requirement parsing is shared by both conditions and excluded from both.

## E3 Retrieval ablation

| Variant | Recall@5 | Recall@10 | nDCG@5 | nDCG@10 | Latency s |
|---|---|---|---|---|---|
| dense | 0.84 | 0.81 | 0.84 | 0.81 | 0.181 |
| bm25 | 0.72 | 0.70 | 0.74 | 0.72 | 0.003 |
| bm25+dense | 0.84 | 0.84 | 0.84 | 0.84 | 0.051 |
| bm25+dense+graph | 0.85 | 0.86 | 0.85 | 0.86 | 0.073 |
| hybrid+neo4j_constraint | 0.95 | 0.94 | 0.95 | 0.94 | 0.063 |

nDCG@10 by query type: dense: model 0.90, spec 0.74, semantic 0.78; bm25: model 0.84, spec 0.74, semantic 0.57; bm25+dense: model 0.93, spec 0.85, semantic 0.73; bm25+dense+graph: model 0.96, spec 0.88, semantic 0.73; hybrid+neo4j_constraint: model 0.98, spec 0.94, semantic 0.89

## E4 Orchestration and recovery

| Fault | Recovery with replan | Mean rounds | Parts kept | First round targeted | Recovery without replan |
|---|---|---|---|---|---|
| memory mismatch | 100% (5) | 1.4 | 82% | 80% | 0% |
| over budget | 100% (5) | 1.4 | 70% | 100% | 0% |
| psu too small | 100% (4) | 1.0 | 88% | 100% | 0% |
| socket mismatch | 100% (5) | 1.4 | 80% | 100% | 0% |

| | DAG | Pi runtime |
|---|---|---|
| task completed | 100% | 88% |
| constraint satisfaction | 100% | 88% |
| mean seconds | 36.51 | 54.56 |
| mean tool calls | 21.88 | 6.38 |
| mean llm calls | 6.62 | 7.0 |
| mean tokens | 7151.5 | 11358.88 |

## E5 Data quality

- LLM tier vs rules on 60 values the rules already knew: 40 answers passed the evidence guard, agreement when accepted 98%; 20 abstained or were rejected.
- Compatibility verdicts on 40 configurations: accuracy when decided 100%, unknown rate 0%, false passes 0.

## Cross-store consistency

Consistent: **True**. Neo4j evidence items 2251, Milvus chunks 4149 for 2251 evidence items; orphan chunks 0, graph evidence without chunks 0, chunks from other snapshots 0.

Figures are in `figures/`. Per-case records: `e1_records.jsonl`, `e2_baseline_records.jsonl`, `e4_pi_records.jsonl`; LLM-judge items for spot checks: `judge_items.jsonl`.
