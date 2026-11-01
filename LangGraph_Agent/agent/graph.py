"""Graph assembly: linear pipeline + two conditional edges (plan D5: LangGraph, restrained).

    START -> input_normalizer -> context_loader -> pending_router
        pending answered -> reply_composer (skip planner)
        otherwise        -> planner -> policy_gate
            allowed -> tool_executor -> reply_composer
            blocked -> reply_composer
        reply_composer -> reply_guard -> trace_writer -> END
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from langgraph.graph import END, START, StateGraph

from .config import Config, load_config
from .nodes import (
    context_loader,
    input_normalizer,
    pending_router,
    planner,
    policy_gate,
    reply_composer,
    reply_guard,
    route_after_policy,
    tool_executor,
    trace_writer,
)
from .state import AgentState, initial_state
from .storage import BitableStore, FakeStore
from .policy import resolve_preset


@lru_cache(maxsize=1)
def _compiled():
    graph = StateGraph(AgentState)
    graph.add_node("input_normalizer", input_normalizer)
    graph.add_node("context_loader", context_loader)
    graph.add_node("pending_router", pending_router)
    graph.add_node("planner", planner)
    graph.add_node("policy_gate", policy_gate)
    graph.add_node("tool_executor", tool_executor)
    graph.add_node("reply_composer", reply_composer)
    graph.add_node("reply_guard", reply_guard)
    graph.add_node("trace_writer", trace_writer)

    graph.add_edge(START, "input_normalizer")
    graph.add_edge("input_normalizer", "context_loader")
    graph.add_edge("context_loader", "pending_router")
    graph.add_conditional_edges(
        "pending_router",
        lambda state: "policy" if state.get("plan") else "planner",
        {"policy": "policy_gate", "planner": "planner"},
    )
    graph.add_edge("planner", "policy_gate")
    graph.add_conditional_edges("policy_gate", route_after_policy, {"executor": "tool_executor", "composer": "reply_composer"})
    graph.add_edge("tool_executor", "reply_composer")
    graph.add_edge("reply_composer", "reply_guard")
    graph.add_edge("reply_guard", "trace_writer")
    graph.add_edge("trace_writer", END)
    return graph.compile()


def run(
    message: dict[str, str],
    *,
    store: Any = None,
    config: Config | None = None,
    intake_date: str | None = None,
) -> dict[str, Any]:
    """Process one inbound message dict ({message_id,user_id,chat_id,text}) and
    return {status, reply, ...facts}. Reply sending stays in the caller (app.py)."""
    config = config or load_config()
    preset = resolve_preset(config.preset, config.allowed_tools)
    if store is None:
        store = FakeStore() if preset.store_kind == "fake" else BitableStore(offset_hours=config.timezone_offset_hours)

    execution = store.load_execution(message) if preset.allow_write else None
    if execution and execution["status"] == "completed":
        return execution["payload"]["result"]
    app = _compiled()
    state = initial_state(message)
    state.update({"config": config, "store": store, "preset": preset, "execution": execution, "intake_date": intake_date})
    final = app.invoke(state)

    errors = list(final.get("errors") or [])
    if not final["guard_result"]["passed"]:
        errors.append("reply_guard_failed")
    result = {
        "status": "error" if errors else "ok",
        "reply": str(final.get("final_reply") or ""),
        "reply_source": str(final.get("reply_source") or ""),
        "action_summary": str(final.get("action_summary") or ""),
        "trace_id": str(final.get("trace_id") or ""),
        "latency_ms": int(final.get("latency_ms") or 0),
        "plan": dict(final.get("plan") or {}),
        "policy_decision": dict(final.get("policy_decision") or {}),
        "tool_results": list(final.get("tool_results") or []),
        "guard_result": dict(final.get("guard_result") or {}),
        "errors": errors,
        "trace_written": bool(final.get("trace_written")),
    }
    execution = final.get("execution")
    if execution and not errors and final["guard_result"]["passed"]:
        store.save_execution(execution, {**execution["payload"], "result": result}, completed=True)
    return result


__all__ = ["run"]
