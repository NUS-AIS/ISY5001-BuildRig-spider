"""E2 control condition: a single LLM agent with the same model, data and tools, but no role split.

Same as the multi-agent system: qwen3:8b via Ollama, temperature 0, the pinned snapshot, the same
candidate search (Neo4j filters), validation rules and hybrid retrieval, and the same tool-call budget.
Different: no specialist planner, no separate evidence/review agents, no replanner and no server-side
gate on submission - one agent decides everything in a single tool loop.
"""
from __future__ import annotations

import json
import time

from langchain_core.messages import HumanMessage, SystemMessage, ToolMessage
from langchain_core.tools import StructuredTool

from backend.planning import assemble_option
from backend.validation import validate_option

SYSTEM = ("/no_think You are a computer purchase assistant for Singapore. Use the tools to choose real in-stock offers; "
          "never invent products, prices or specifications. Respect the budget and every hard constraint. "
          "When you are done, call submit_build exactly once.")


def _compact(row: dict) -> dict:
    return {"offer_id": row["id"], "name": row["name"][:70], "price_sgd": float(row["price"]),
            "specs": {k: v for k, v in (row.get("specs") or {}).items() if v is not None}}


class SingleAgentBaseline:
    def __init__(self, corpus, settings, max_tool_calls: int = 24):
        from langchain_ollama import ChatOllama
        self.corpus = corpus
        self.max_tool_calls = max_tool_calls
        self.llm = ChatOllama(model=settings.model_name, base_url=settings.ollama_base_url, temperature=0,
                              reasoning=False, num_ctx=12288, num_predict=1200)

    def run(self, requirements: dict, device_type: str) -> dict:
        corpus, state = self.corpus, {"seen": set(), "submitted": None, "calls": [], "usage": {"calls": 0, "input_tokens": 0, "output_tokens": 0}}

        def find_parts(category: str, max_price_sgd: float | None = None, require: dict | None = None,
                       minimum: dict | None = None) -> str:
            """List in-stock offers of one category (cpu, motherboard, ram, gpu, psu, case, ssd, cooler, laptop),
            highest price first. require = exact spec matches, e.g. {"socket": "AM5"}; minimum = lower bounds, e.g. {"wattage_w": 750}."""
            rows = corpus.candidates(category, int(max_price_sgd * 100) if max_price_sgd else None, 5, None,
                                     require or None, minimum or None, None, "price_desc")
            state["seen"].update(r["id"] for r in rows)
            return json.dumps([_compact(r) for r in rows]) if rows else "No matching offers."

        def check_build(offer_ids: list[str]) -> str:
            """Validate a set of offer ids: budget, stock, socket, memory, PSU, clearance, form factor."""
            try:
                option = assemble_option(corpus, device_type, offer_ids, requirements)
            except KeyError as exc:
                return f"Unknown offer ids: {exc.args[0]}. Use ids returned by find_parts."
            v = validate_option(option, requirements)
            return json.dumps({"overall_status": v["overall_status"], "total_sgd": v["total_minor"] / 100,
                               "checks": [{"code": c["code"], "status": c["status"], "reason": c["reason"]} for c in v["checks"]]})

        def find_evidence(query: str, offer_ids: list[str]) -> str:
            """Search reviews and listings about the given offers."""
            pids = [str((corpus.offer(o) or {}).get("product_id") or o) for o in offer_ids]
            items = corpus.search(query, product_ids=pids, top_k=5)["items"]
            return json.dumps([{"kind": i["kind"], "excerpt": i["excerpt"][:200]} for i in items])

        def submit_build(offer_ids: list[str], title: str, reasons: list[str]) -> str:
            """Submit the final offer ids with a title and 2-4 reasons."""
            try:
                option = assemble_option(corpus, device_type, offer_ids, requirements)
            except KeyError as exc:
                return f"Unknown offer ids: {exc.args[0]}."
            state["submitted"] = {"option": option, "title": title, "reasons": reasons}
            return "Submitted."

        tools = {f.__name__: StructuredTool.from_function(f) for f in (find_parts, check_build, find_evidence, submit_build)}
        llm = self.llm.bind_tools(list(tools.values()))
        budget = requirements["budget"]["maximum_minor"] / 100
        brief = (f"Recommend ONE {'complete desktop (cpu, motherboard, ram, gpu, psu, case, ssd, cooler)' if device_type == 'desktop' else 'laptop'}. "
                 f"Budget at most S${budget}. Workloads: {requirements.get('workloads')}. Preferences: {requirements.get('preferences')}. "
                 f"Hard constraints: {requirements.get('hard_constraints')}. Locked products: {[i.get('name') for i in requirements.get('locked_items', [])]}. "
                 f"Already owned: {[o.get('mention') for o in requirements.get('owned_components', [])]}.")
        messages = [SystemMessage(SYSTEM), HumanMessage(brief)]
        started, nudged = time.time(), False
        while len(state["calls"]) < self.max_tool_calls and not state["submitted"]:
            try:
                response = llm.invoke(messages)
            except Exception as exc:
                state["error"] = type(exc).__name__
                break
            meta = getattr(response, "usage_metadata", None) or {}
            state["usage"]["calls"] += 1
            state["usage"]["input_tokens"] += meta.get("input_tokens", 0)
            state["usage"]["output_tokens"] += meta.get("output_tokens", 0)
            messages.append(response)
            if not response.tool_calls:
                if nudged:
                    break
                nudged = True
                messages.append(HumanMessage("/no_think Continue using the tools and finish with submit_build."))
                continue
            for call in response.tool_calls:
                state["calls"].append(call["name"])
                tool = tools.get(call["name"])
                try:
                    output = tool.invoke(call["args"]) if tool else f"Unknown tool {call['name']}"
                except Exception as exc:
                    output = f"Tool error: {exc}"
                messages.append(ToolMessage(content=str(output)[:6000], tool_call_id=call["id"]))
        result = {"orchestration_mode": "single_agent", "tool_calls": [{"tool": c} for c in state["calls"]],
                  "model_usage": state["usage"], "duration_seconds": round(time.time() - started, 1),
                  "error": state.get("error")}
        if state["submitted"]:
            option = state["submitted"]["option"]
            option.update(title=state["submitted"]["title"], reasons=state["submitted"]["reasons"], trade_offs=[],
                          validation=validate_option(option, requirements), evidence=[])
            result.update(outcome="recommendations_available", options=[option])
        else:
            result.update(outcome="no_submission", options=[])
        return result
