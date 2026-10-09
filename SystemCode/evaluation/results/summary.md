# BuildRig evaluation summary

Snapshot `20260906T124528641415Z`, chat model `qwen3:8b`, embedding `bge-m3`, commit `992b1cd`, max revisions 3.

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
| Explanation guard: sentences filtered / cases affected | 0 / 0 of 29 | |
| Mean latency (s) / tool calls / LLM calls / tokens | 64.1 / 18.8 / 7.5 / 8362.2 | |

Task completion by scenario: desktop 100%, laptop 100%, compare 100%, ambiguous 100%, low_budget 100%, locked 100%, follow_up 100%, data_gaps 100%

## E2 Multi-agent vs single agent

| Metric | Multi-agent (DAG) | Single agent |
|---|---|---|
| Task completed | 100% | 47% |
| Constraints met | 100% | 75% |
| Compatibility correct | 100% | 75% |
| Source accurate | 100% | 100% |
| Unknowns honest | 100% | 100% |
| Latency s | 42.4 | 65.2 |
| LLM calls | 6.2 | 13.6 |
| Tool calls | 18.4 | 15.2 |
| Tokens | 6737.6 | 80252.5 |

Token counts for the multi-agent system cover the recommendation run; requirement parsing is shared by both conditions and excluded from both.

## E3 Retrieval ablation

| Variant | Recall@5 | Recall@10 | nDCG@5 | nDCG@10 | Latency s |
|---|---|---|---|---|---|
| dense | 0.83 | 0.81 | 0.83 | 0.81 | 0.137 |
| bm25 | 0.72 | 0.70 | 0.74 | 0.72 | 0.007 |
| bm25+dense | 0.84 | 0.84 | 0.85 | 0.85 | 0.029 |
| bm25+dense+graph | 0.85 | 0.86 | 0.86 | 0.86 | 0.098 |
| hybrid+neo4j_constraint | 0.95 | 0.93 | 0.95 | 0.93 | 0.055 |

nDCG@10 by query type: dense: model 0.90, spec 0.74, semantic 0.79; bm25: model 0.84, spec 0.74, semantic 0.57; bm25+dense: model 0.93, spec 0.87, semantic 0.74; bm25+dense+graph: model 0.96, spec 0.88, semantic 0.74; hybrid+neo4j_constraint: model 0.98, spec 0.93, semantic 0.89

## E4 Orchestration and recovery

| Fault | Recovery with replan | Mean rounds | Parts kept | First round targeted | Recovery without replan |
|---|---|---|---|---|---|
| memory mismatch | 100% (5) | 1.4 | 82% | 80% | 0% |
| over budget | 100% (5) | 1.4 | 70% | 100% | 0% |
| psu too small | 100% (4) | 1.0 | 88% | 100% | 0% |
| socket mismatch | 100% (5) | 1.6 | 78% | 100% | 0% |

| | DAG | Pi runtime |
|---|---|---|
| task completed | 100% | 75% |
| constraint satisfaction | 100% | 75% |
| mean seconds | 42.23 | 234.91 |
| mean tool calls | 20.5 | 7.12 |
| mean llm calls | 6.38 | 7.62 |
| mean tokens | 7021.88 | 8569.38 |

## E5 Data quality

- LLM tier vs rules on 60 values the rules already knew: 40 answers passed the evidence guard, agreement when accepted 98%; 20 abstained or were rejected.
- Compatibility verdicts on 40 configurations: accuracy when decided 100%, unknown rate 0%, false passes 0.

## Cross-store consistency

Consistent: **True**. Neo4j evidence items 2251, Milvus chunks 4149 for 2251 evidence items; orphan chunks 0, graph evidence without chunks 0, chunks from other snapshots 0.

Figures are in `figures/`. Per-case records: `e1_records.jsonl`, `e2_baseline_records.jsonl`, `e4_pi_records.jsonl`; LLM-judge items for spot checks: `judge_items.jsonl`.
