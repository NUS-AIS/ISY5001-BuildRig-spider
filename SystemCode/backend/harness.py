from dataclasses import dataclass
from typing import Any, Callable, Literal
from backend.store import StateStore


ReplayPolicy = Literal["safe", "never"]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    handler: Callable[..., Any]
    replay: ReplayPolicy = "safe"
    description: str = ""


class ToolHarness:
    """Durable effect boundary shared by DAG agents and the Pi adapter."""
    def __init__(self, store: StateStore, tools: list[ToolSpec], max_calls: int = 80):
        self.store = store
        self.tools = {tool.name: tool for tool in tools}
        self.max_calls = max_calls

    def call(self, run_id: str, agent_role: str, name: str, **arguments):
        if self.store.is_cancelled(run_id):
            raise RuntimeError("Run was cancelled")
        if name not in self.tools:
            raise ValueError(f"Tool is not registered: {name}")
        if len(self.store.tool_calls(run_id)) >= self.max_calls:
            raise RuntimeError("Tool call budget exhausted")
        spec = self.tools[name]
        call_id = self.store.tool_intent(run_id, agent_role, name, spec.replay, arguments)
        self.store.event(run_id, "tool_start", {"tool_call_id": call_id, "agent_role": agent_role, "tool": name})
        self.store.tool_pending(call_id)
        try:
            result = spec.handler(**arguments)
        except Exception as exc:
            error = {"code": "TOOL_FAILED", "message": str(exc)}
            self.store.tool_settle(call_id, error=error)
            self.store.event(run_id, "tool_end", {"tool_call_id": call_id, "status": "failed"})
            raise
        self.store.tool_settle(call_id, result=result)
        self.store.event(run_id, "tool_end", {"tool_call_id": call_id, "status": "completed"})
        return result


class RunHarness:
    """Persists complete restart state after each agent transition."""
    def __init__(self, store: StateStore, run_id: str, max_agent_steps: int = 24):
        self.store, self.run_id, self.max_agent_steps = store, run_id, max_agent_steps
        current = store.operation_state(run_id)
        self.state = current["state"] if current else {"phase": "starting", "plan_version": 0, "completed_tasks": [], "artifacts": {}}

    def transition(self, phase: str, agent_role: str, task_id: str, artifact: Any = None):
        if len(self.state["completed_tasks"]) >= self.max_agent_steps:
            raise RuntimeError("Agent step budget exhausted")
        if artifact is not None:
            self.state["artifacts"][task_id] = artifact
        self.state["completed_tasks"].append({"task_id": task_id, "agent_role": agent_role})
        self.state["phase"] = phase
        version = self.store.checkpoint(self.run_id, self.state)
        self.store.event(self.run_id, "agent_transition", {"task_id": task_id, "agent_role": agent_role, "phase": phase, "checkpoint_version": version})
