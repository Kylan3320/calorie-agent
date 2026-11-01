"""Graph state definition and initial state factory."""

from __future__ import annotations

import time
from typing import Any, TypedDict
from uuid import uuid4


class AgentState(TypedDict, total=False):
    # runtime bindings (injected per run, not serializable)
    config: Any
    store: Any
    preset: Any
    # request
    trace_id: str
    message: dict[str, str]
    raw_text: str
    normalized_text: str
    # facts
    context: dict[str, Any]
    execution: Any
    intake_date: str | None
    pending_route: dict[str, Any]
    plan: dict[str, Any]
    planner_fallback: bool
    policy_decision: dict[str, Any]
    risk: dict[str, Any]
    tool_results: list[dict[str, Any]]
    observations: list[dict[str, Any]]
    # output
    final_reply: str
    reply_source: str
    action_summary: str
    guard_result: dict[str, Any]
    errors: list[str]
    trace_written: bool
    started_at: float
    latency_ms: int


def initial_state(
    message: dict[str, str],
    *,
    raw_text: str | None = None,
) -> AgentState:
    return {
        "trace_id": f"v4_{uuid4().hex[:12]}",
        "message": dict(message),
        "raw_text": str(raw_text if raw_text is not None else message.get("text") or ""),
        "normalized_text": "",
        "context": {},
        "pending_route": {},
        "plan": {},
        "planner_fallback": False,
        "policy_decision": {},
        "risk": {},
        "tool_results": [],
        "observations": [],
        "final_reply": "",
        "reply_source": "",
        "action_summary": "",
        "guard_result": {},
        "errors": [],
        "trace_written": False,
        "started_at": time.monotonic(),
        "latency_ms": 0,
    }
