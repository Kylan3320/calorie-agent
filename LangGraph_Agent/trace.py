from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from .state import LangGraphAgentState


DEFAULT_TRACE_PATH = Path(__file__).resolve().parents[3] / "reports" / "langgraph_agent_v0_traces.jsonl"


def write_trace(state: LangGraphAgentState, trace_path: str | Path | None = None) -> bool:
    path = Path(trace_path) if trace_path is not None else DEFAULT_TRACE_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = _trace_payload(state)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
    return True


def _trace_payload(state: LangGraphAgentState) -> dict[str, Any]:
    return {
        "schema_version": "langgraph_agent_trace.v0",
        "created_at": int(time.time()),
        "trace_id": state.get("trace_id", ""),
        "session_id": state.get("session_id", ""),
        "user_id": state.get("user_id", ""),
        "mode": state.get("mode", ""),
        "planner_backend": state.get("planner_backend", ""),
        "raw_text": state.get("raw_text", ""),
        "normalized_text": state.get("normalized_text", ""),
        "retrieved_context_summary": _retrieved_summary(state.get("retrieved_context", {})),
        "plan": state.get("plan", {}),
        "policy_decision": state.get("policy_decision", {}),
        "tool_results": state.get("tool_results", []),
        "readonly_candidate": state.get("readonly_candidate", {}),
        "action_summary": state.get("action_summary", ""),
        "final_reply": state.get("final_reply", ""),
        "guard_result": state.get("guard_result", {}),
        "errors": state.get("errors", []),
    }


def _retrieved_summary(retrieved: dict[str, Any]) -> dict[str, int]:
    if not isinstance(retrieved, dict):
        return {}
    return {
        key: len(value) if isinstance(value, list) else 0
        for key, value in retrieved.items()
    }
