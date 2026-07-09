from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph

from .nodes import (
    action_summary_composer,
    context_retriever,
    deterministic_planner,
    fake_tool_executor,
    input_normalizer,
    llm_planner,
    observation_repair,
    policy_gate,
    reply_composer,
    reply_guard,
    trace_writer,
)
from .state import LangGraphAgentState, initial_state


def build_graph(planner_backend: str = "deterministic"):
    graph = StateGraph(LangGraphAgentState)
    graph.add_node("input_normalizer", input_normalizer)
    graph.add_node("context_retriever", context_retriever)
    graph.add_node("planner", llm_planner if planner_backend == "llm" else deterministic_planner)
    graph.add_node("policy_gate", policy_gate)
    graph.add_node("tool_executor", fake_tool_executor)
    graph.add_node("observation_repair", observation_repair)
    graph.add_node("action_summary_composer", action_summary_composer)
    graph.add_node("reply_composer", reply_composer)
    graph.add_node("reply_guard", reply_guard)
    graph.add_node("trace_writer", trace_writer)

    graph.add_edge(START, "input_normalizer")
    graph.add_edge("input_normalizer", "context_retriever")
    graph.add_edge("context_retriever", "planner")
    graph.add_edge("planner", "policy_gate")
    graph.add_edge("policy_gate", "tool_executor")
    graph.add_edge("tool_executor", "observation_repair")
    graph.add_edge("observation_repair", "action_summary_composer")
    graph.add_edge("action_summary_composer", "reply_composer")
    graph.add_edge("reply_composer", "reply_guard")
    graph.add_edge("reply_guard", "trace_writer")
    graph.add_edge("trace_writer", END)
    return graph.compile()


def run_langgraph_agent(
    message: dict[str, Any],
    *,
    context: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
) -> dict[str, Any]:
    options = _entrypoint_options(message, context=context, settings=settings)
    state = _run_graph_state(str(message.get("text") or ""), **options)
    return _final_result(state)


def run_agent(text: str, **kwargs: Any) -> LangGraphAgentState:
    return _run_graph_state(text, **kwargs)


def _run_graph_state(text: str, **kwargs: Any) -> LangGraphAgentState:
    planner_backend = str(kwargs.get("planner_backend") or "deterministic")
    app = build_graph(planner_backend=planner_backend)
    state_kwargs = {
        key: kwargs[key]
        for key in ("user_id", "session_id", "mode", "planner_backend", "current_date", "timezone")
        if key in kwargs
    }
    state = initial_state(text, **state_kwargs)
    if "message" in kwargs and isinstance(kwargs["message"], dict):
        state["message"] = dict(kwargs["message"])
    for key in (
        "readonly_allowed_tools",
        "readonly_legacy",
        "write_allowed_tools",
        "write_legacy",
        "fixture_mode",
        "plan_override",
        "trace_path",
        "trace_enabled",
    ):
        if key in kwargs:
            state[key] = kwargs[key]
    result = app.invoke(state)
    return dict(result)


def _entrypoint_options(
    message: dict[str, Any],
    *,
    context: dict[str, Any] | None,
    settings: dict[str, Any] | None,
) -> dict[str, Any]:
    context = dict(context or {})
    settings = dict(settings or {})
    options: dict[str, Any] = {
        "message": {
            "message_id": str(message.get("message_id") or ""),
            "user_id": str(message.get("user_id") or ""),
            "chat_id": str(message.get("chat_id") or message.get("session_id") or ""),
            "text": str(message.get("text") or ""),
        }
    }
    for key in ("user_id", "session_id", "mode", "planner_backend", "timezone"):
        if key in message:
            options[key] = message[key]
    if "planner_backend" in settings:
        options["planner_backend"] = settings["planner_backend"]
    if "timezone" in context:
        options["timezone"] = context["timezone"]
    if "today" in context:
        options["current_date"] = context["today"]
    for key in (
        "readonly_allowed_tools",
        "readonly_legacy",
        "write_allowed_tools",
        "write_legacy",
        "fixture_mode",
        "plan_override",
        "trace_path",
        "trace_enabled",
    ):
        if key in settings:
            options[key] = settings[key]
        elif key in context:
            options[key] = context[key]
    return options


def _final_result(state: LangGraphAgentState) -> dict[str, Any]:
    errors = list(state.get("errors") or [])
    return {
        "reply": str(state.get("final_reply") or ""),
        "status": "error" if errors else "ok",
        "action_summary": str(state.get("action_summary") or ""),
        "execution_facts": {
            "plan": dict(state.get("plan") or {}),
            "policy_decision": dict(state.get("policy_decision") or {}),
            "tool_results": list(state.get("tool_results") or []),
            "observations": list(state.get("observations") or []),
        },
        "metrics": {
            "tool_result_count": len(list(state.get("tool_results") or [])),
            "error_count": len(errors),
            "trace_written": bool(state.get("trace_written")),
        },
        "trace": dict(state),
        "errors": errors,
    }
