from __future__ import annotations

from typing import Any, TypedDict
from uuid import uuid4


class LangGraphAgentState(TypedDict, total=False):
    trace_id: str
    session_id: str
    user_id: str
    mode: str
    planner_backend: str
    raw_text: str
    normalized_text: str
    context: dict[str, Any]
    message: dict[str, str]
    readonly_allowed_tools: list[str]
    readonly_candidate: dict[str, Any]
    readonly_legacy: Any
    write_allowed_tools: list[str]
    write_candidate: dict[str, Any]
    write_legacy: Any
    core_candidate: dict[str, Any]
    core_action_summary: str
    core_source_result: dict[str, Any]
    core_path: str
    fixture_mode: str
    action_summary: str
    trace_path: str
    trace_enabled: bool
    retrieved_context: dict[str, list[dict[str, Any]]]
    plan: dict[str, Any]
    plan_override: dict[str, Any]
    risk: dict[str, Any]
    policy_decision: dict[str, Any]
    tool_results: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    final_reply: str
    guard_result: dict[str, Any]
    errors: list[str]
    trace_written: bool


def initial_state(
    text: str,
    *,
    user_id: str = "local_user",
    session_id: str = "local_session",
    mode: str = "shadow",
    planner_backend: str = "deterministic",
    current_date: str = "2026-07-03",
    timezone: str = "America/Los_Angeles",
) -> LangGraphAgentState:
    return {
        "trace_id": f"lgv0_{uuid4().hex[:12]}",
        "session_id": session_id,
        "user_id": user_id,
        "mode": mode,
        "planner_backend": planner_backend,
        "raw_text": str(text or ""),
        "normalized_text": "",
        "context": {"current_date": current_date, "timezone": timezone},
        "retrieved_context": {},
        "plan": {},
        "risk": {},
        "policy_decision": {},
        "tool_results": [],
        "observations": [],
        "final_reply": "",
        "guard_result": {},
        "errors": [],
        "trace_written": False,
    }
