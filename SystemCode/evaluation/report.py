"""Turn evaluation/results/*.json into figures and summary.md.   python -m evaluation.report"""
from __future__ import annotations

import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

BLUE, ORANGE, GREEN, GREY, RED, PURPLE = "#2F6DB5", "#D9822B", "#2E8B57", "#9AA5B1", "#C0392B", "#7B4FB8"
plt.rcParams.update({"figure.dpi": 150, "axes.spines.top": False, "axes.spines.right": False, "font.size": 9})


def _load(results: Path, name: str):
    path = results / name
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _bar_labels(ax, bars, fmt="{:.0%}"):
    for b in bars:
        ax.annotate(fmt.format(b.get_height()), (b.get_x() + b.get_width() / 2, b.get_height()),
                    ha="center", va="bottom", fontsize=7, xytext=(0, 2), textcoords="offset points")


METRICS = [("task_completed", "Task completed"), ("constraint_satisfaction", "Constraints met"),
           ("compatibility_correct", "Compatibility correct"), ("source_accuracy", "Source accurate"),
           ("unknown_handling", "Unknowns honest"), ("adjustment_success", "Follow-up success")]


def _rate(summary, key):
    return (summary.get(key) or {}).get("rate")


def build(results: Path) -> None:
    figs = results / "figures"
    figs.mkdir(exist_ok=True)
    lines = ["# BuildRig evaluation summary", ""]
    meta = _load(results, "run_metadata.json") or {}
    lines += [f"Snapshot `{meta.get('snapshot_id')}`, chat model `{meta.get('chat_model')}`, embedding `{meta.get('embedding_model')}`, "
              f"commit `{meta.get('git_commit')}`, max revisions {meta.get('max_revisions')}.", ""]

    e1 = _load(results, "e1_end_to_end.json")
    if e1:
        s = e1["summary"]
        keys = [(k, l) for k, l in METRICS if s.get(k)]
        fig, ax = plt.subplots(figsize=(7, 3))
        bars = ax.bar([l for _, l in keys], [_rate(s, k) for k, _ in keys], color=BLUE)
        _bar_labels(ax, bars)
        ax.set_ylim(0, 1.1)
        ax.set_title(f"E1 end-to-end quality ({len(e1['cases'])} cases)")
        plt.xticks(rotation=20, ha="right")
        fig.tight_layout(); fig.savefig(figs / "e1_scorecard.png"); plt.close(fig)
        sc = s["task_completed_by_scenario"]
        fig, ax = plt.subplots(figsize=(7, 3))
        bars = ax.bar(list(sc), list(sc.values()), color=GREEN)
        _bar_labels(ax, bars)
        ax.set_ylim(0, 1.1); ax.set_title("E1 task completion by scenario")
        plt.xticks(rotation=20, ha="right")
        fig.tight_layout(); fig.savefig(figs / "e1_by_scenario.png"); plt.close(fig)
        lines += ["## E1 End-to-end quality", "", "| Metric | Rate | n |", "|---|---|---|"]
        lines += [f"| {l} | {_rate(s, k):.0%} | {s[k]['n']} |" for k, l in METRICS if s.get(k)]
        g = s.get("explanation_guard") or {}
        lines += [f"| Explanation quality (LLM judge, 1-5) | {s.get('explanation_quality_mean')} | |",
                  f"| Explanation guard: sentences filtered / cases affected | {g.get('sentences_filtered')} / "
                  f"{g.get('cases_with_filtered_sentences')} of {g.get('cases_with_options')} | |",
                  f"| Mean latency (s) / tool calls / LLM calls / tokens | {s.get('mean_seconds')} / {s.get('mean_tool_calls')} / "
                  f"{s.get('mean_llm_calls')} / {s.get('mean_tokens')} | |", "",
                  "Task completion by scenario: " + ", ".join(f"{k} {v:.0%}" for k, v in sc.items()), ""]
        failures = [c for c in e1["cases"] if not c.get("task_completed")]
        if failures:
            lines += ["Cases not completed: " + "; ".join(f"{c['case_id']} ({c.get('detail')})" for c in failures), ""]

    e2 = _load(results, "e2_multi_vs_single.json")
    if e2:
        m, sgl = e2["summary"]["multi_agent_dag"], e2["summary"]["single_agent"]
        keys = [(k, l) for k, l in METRICS[:5]]
        fig, (a1, a2) = plt.subplots(1, 2, figsize=(9, 3.2), gridspec_kw={"width_ratios": [3, 2]})
        xs = range(len(keys))
        b1 = a1.bar([x - 0.2 for x in xs], [_rate(m, k) or 0 for k, _ in keys], 0.4, color=BLUE, label="Multi-agent (DAG)")
        b2 = a1.bar([x + 0.2 for x in xs], [_rate(sgl, k) or 0 for k, _ in keys], 0.4, color=ORANGE, label="Single agent")
        _bar_labels(a1, b1); _bar_labels(a1, b2)
        a1.set_xticks(list(xs)); a1.set_xticklabels([l for _, l in keys], rotation=20, ha="right"); a1.set_ylim(0, 1.15)
        a1.legend(fontsize=7, loc="upper center", bbox_to_anchor=(0.5, -0.32), ncol=2, frameon=False); a1.set_title("Quality")
        costs = [("mean_seconds", "Latency s"), ("mean_llm_calls", "LLM calls"), ("mean_tool_calls", "Tool calls")]
        xs = range(len(costs))
        a2.bar([x - 0.2 for x in xs], [m.get(k) or 0 for k, _ in costs], 0.4, color=BLUE)
        a2.bar([x + 0.2 for x in xs], [sgl.get(k) or 0 for k, _ in costs], 0.4, color=ORANGE)
        a2.set_xticks(list(xs)); a2.set_xticklabels([l for _, l in costs]); a2.set_title("Cost per case")
        fig.suptitle(f"E2 multi-agent vs single agent ({len(e2['cases'])} cases, same model and tools)")
        fig.tight_layout(); fig.savefig(figs / "e2_multi_vs_single.png"); plt.close(fig)
        lines += ["## E2 Multi-agent vs single agent", "", "| Metric | Multi-agent (DAG) | Single agent |", "|---|---|---|"]
        for k, l in keys:
            lines.append(f"| {l} | {_fmt(_rate(m, k))} | {_fmt(_rate(sgl, k))} |")
        for k, l in costs + [("mean_tokens", "Tokens")]:
            lines.append(f"| {l} | {m.get(k)} | {sgl.get(k)} |")
        lines += ["", "Token counts for the multi-agent system cover the recommendation run; requirement parsing is shared by "
                  "both conditions and excluded from both.", ""]

    e3 = _load(results, "e3_retrieval_ablation.json")
    if e3:
        s = e3["summary"]
        variants = list(s)
        types = ["model", "spec", "semantic", "all"]
        fig, ax = plt.subplots(figsize=(8, 3.2))
        width = 0.8 / len(variants)
        colors = [GREY, PURPLE, ORANGE, BLUE, GREEN]
        for i, v in enumerate(variants):
            bars = ax.bar([t + i * width for t in range(len(types))], [s[v][t]["ndcg@10"] for t in types], width,
                          label=v, color=colors[i % len(colors)])
            _bar_labels(ax, bars, "{:.2f}")
        ax.set_xticks([t + 0.4 - width / 2 for t in range(len(types))]); ax.set_xticklabels(types)
        ax.set_ylim(0, 1.15); ax.set_ylabel("nDCG@10")
        ax.legend(fontsize=7, ncol=5, loc="upper center", bbox_to_anchor=(0.5, -0.1), frameon=False)
        ax.set_title(f"E3 retrieval ablation ({e3['queries']} labelled queries)")
        fig.tight_layout(); fig.savefig(figs / "e3_retrieval_ablation.png"); plt.close(fig)
        lines += ["## E3 Retrieval ablation", "", "| Variant | Recall@5 | Recall@10 | nDCG@5 | nDCG@10 | Latency s |",
                  "|---|---|---|---|---|---|"]
        lines += [f"| {v} | {s[v]['all']['recall@5']:.2f} | {s[v]['all']['recall@10']:.2f} | {s[v]['all']['ndcg@5']:.2f} | "
                  f"{s[v]['all']['ndcg@10']:.2f} | {s[v]['latency_s']} |" for v in variants]
        lines += ["", "nDCG@10 by query type: " + "; ".join(
            f"{v}: " + ", ".join(f"{t} {s[v][t]['ndcg@10']:.2f}" for t in ('model', 'spec', 'semantic')) for v in variants), ""]

    e4 = _load(results, "e4_orchestration.json")
    if e4:
        rec = e4["summary"]["fault_recovery"]
        faults = sorted({f for mode in rec.values() for f in mode})
        fig, (a1, a2) = plt.subplots(1, 2, figsize=(9, 3.2))
        xs = range(len(faults))
        for off, mode, color in ((-0.2, "with_replan", GREEN), (0.2, "without_replan", GREY)):
            bars = a1.bar([x + off for x in xs], [rec.get(mode, {}).get(f, {}).get("recovery_rate", 0) for f in faults], 0.4,
                          color=color, label=mode.replace("_", " "))
            _bar_labels(a1, bars)
        a1.set_xticks(list(xs)); a1.set_xticklabels([f.replace("_", " ") for f in faults], rotation=15, ha="right")
        a1.set_ylim(0, 1.15); a1.legend(fontsize=7); a1.set_title("Recovery after injected faults")
        dvp = e4["summary"]["dag_vs_pi"]
        keys = [("task_completed", "Completed"), ("constraint_satisfaction", "Constraints met")]
        xs = range(len(keys))
        b1 = a2.bar([x - 0.2 for x in xs], [dvp["dag"][k] for k, _ in keys], 0.4, color=BLUE, label="DAG")
        b2 = a2.bar([x + 0.2 for x in xs], [dvp["pi"][k] for k, _ in keys], 0.4, color=PURPLE, label="Pi runtime")
        _bar_labels(a2, b1); _bar_labels(a2, b2)
        a2.set_xticks(list(xs)); a2.set_xticklabels([l for _, l in keys]); a2.set_ylim(0, 1.15); a2.legend(fontsize=7)
        a2.set_title(f"DAG vs Pi ({dvp['dag']['cases']} cases)")
        fig.tight_layout(); fig.savefig(figs / "e4_recovery.png"); plt.close(fig)
        lines += ["## E4 Orchestration and recovery", "", "| Fault | Recovery with replan | Mean rounds | Parts kept | First round targeted | Recovery without replan |",
                  "|---|---|---|---|---|---|"]
        for f in faults:
            w, wo = rec.get("with_replan", {}).get(f, {}), rec.get("without_replan", {}).get(f, {})
            lines.append(f"| {f.replace('_', ' ')} | {_fmt(w.get('recovery_rate'))} ({w.get('trials')}) | {w.get('mean_rounds')} | "
                         f"{_fmt(w.get('parts_kept_share'))} | {_fmt(w.get('first_round_targeted_rate'))} | {_fmt(wo.get('recovery_rate'))} |")
        lines += ["", "| | DAG | Pi runtime |", "|---|---|---|"]
        for k in ("task_completed", "constraint_satisfaction", "mean_seconds", "mean_tool_calls", "mean_llm_calls", "mean_tokens"):
            lines.append(f"| {k.replace('_', ' ')} | {_fmt(dvp['dag'].get(k))} | {_fmt(dvp['pi'].get(k))} |")
        lines.append("")

    latency = []
    for name, path, color in (("DAG", "e1_end_to_end.json", BLUE), ("Single agent", "e2_multi_vs_single.json", ORANGE)):
        data = _load(results, path)
        if data:
            values = [c["seconds"] for c in data.get("cases" if name == "DAG" else "single_agent_cases", []) if c.get("seconds")]
            latency.append((name, values, color))
    if e4:
        latency.append(("Pi runtime", [c["seconds"] for c in e4["pi_cases"] if c.get("seconds")], PURPLE))
    if latency:
        fig, ax = plt.subplots(figsize=(6, 3))
        parts = ax.boxplot([v for _, v, _ in latency], patch_artist=True)
        ax.set_xticks(range(1, len(latency) + 1)); ax.set_xticklabels([n for n, _, _ in latency])
        for patch, (_, _, c) in zip(parts["boxes"], latency):
            patch.set_facecolor(c); patch.set_alpha(0.6)
        ax.set_ylabel("seconds per case"); ax.set_title("Recommendation latency")
        fig.tight_layout(); fig.savefig(figs / "e4_latency.png"); plt.close(fig)

    e5 = _load(results, "e5_data_quality.json")
    if e5:
        cov = {c: v for c, v in e5["coverage"].items() if not c.startswith("_")}
        labels, regex, rule, llm = [], [], [], []
        for cat, info in cov.items():
            for field, f in info["fields"].items():
                n = info["offers"]
                labels.append(f"{cat}.{field}")
                regex.append(f["by_method"].get("regex", 0) / n)
                rule.append(f["by_method"].get("rule_inferred", 0) / n)
                llm.append(f["by_method"].get("llm", 0) / n)
        fig, ax = plt.subplots(figsize=(9, 3.4))
        ax.bar(labels, regex, color=BLUE, label="regex")
        ax.bar(labels, rule, bottom=regex, color=GREEN, label="rule inferred")
        ax.bar(labels, llm, bottom=[a + b for a, b in zip(regex, rule)], color=ORANGE, label="LLM (evidence-checked)")
        ax.set_ylim(0, 1.05); ax.set_ylabel("coverage"); ax.legend(fontsize=7)
        ax.set_title("E5 specification coverage by extraction tier")
        plt.xticks(rotation=60, ha="right", fontsize=7)
        fig.tight_layout(); fig.savefig(figs / "e5_spec_coverage.png"); plt.close(fig)
        comp = e5["compatibility"]
        by_rule = {}
        for r in comp["rows"]:
            outcome = "unknown" if r["verdict"] == "unknown" else ("correct" if (r["verdict"] == "passed") == (r["truth"] == "compatible") else "wrong")
            by_rule.setdefault(r["rule"], {"correct": 0, "wrong": 0, "unknown": 0})[outcome] += 1
        fig, ax = plt.subplots(figsize=(6, 3))
        names = list(by_rule)
        bottom = [0] * len(names)
        for outcome, color in (("correct", GREEN), ("wrong", RED), ("unknown", GREY)):
            values = [by_rule[n][outcome] for n in names]
            ax.bar([n.replace("_", " ") for n in names], values, bottom=bottom, color=color, label=outcome)
            bottom = [a + b for a, b in zip(bottom, values)]
        ax.legend(fontsize=7); ax.set_title("E5 compatibility verdicts vs name-derived ground truth")
        fig.tight_layout(); fig.savefig(figs / "e5_rule_accuracy.png"); plt.close(fig)
        agree = e5["llm_vs_rules"]
        lines += ["## E5 Data quality", "",
                  f"- LLM tier vs rules on {agree['pairs']} values the rules already knew: {agree['accepted_by_guard']} answers passed the "
                  f"evidence guard, agreement when accepted {_fmt(agree['agreement_when_accepted'])}; {agree['abstained_or_rejected']} abstained or were rejected.",
                  f"- Compatibility verdicts on {comp['pairs']} configurations: accuracy when decided {_fmt(comp['accuracy_when_decided'])}, "
                  f"unknown rate {_fmt(comp['unknown_rate'])}, false passes {comp['false_passes']}.", ""]

    cons = _load(results, "consistency_report.json")
    if cons:
        p = cons["problems"]
        lines += ["## Cross-store consistency", "",
                  f"Consistent: **{cons['consistent']}**. Neo4j evidence items {cons['neo4j']['evidence_items']}, Milvus chunks "
                  f"{cons['milvus']['chunks']} for {cons['milvus']['evidence_items']} evidence items; orphan chunks "
                  f"{p['orphan_chunks_evidence_count']}, graph evidence without chunks {p['graph_evidence_without_chunks_count']}, "
                  f"chunks from other snapshots {sum(p['chunks_from_other_snapshots'].values()) if p['chunks_from_other_snapshots'] else 0}.", ""]

    lines += ["Figures are in `figures/`. Per-case records: `e1_records.jsonl`, `e2_baseline_records.jsonl`, `e4_pi_records.jsonl`; "
              "LLM-judge items for spot checks: `judge_items.jsonl`."]
    (results / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fmt(value):
    if value is None:
        return "-"
    return f"{value:.0%}" if isinstance(value, float) and value <= 1 else str(value)


if __name__ == "__main__":
    build(Path(__file__).parent / "results")
