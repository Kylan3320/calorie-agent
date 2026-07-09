"""Trace writer node wrapper."""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..state import LangGraphAgentState
from ..trace import write_trace

DEFAULT_CORE_TRACE_PATH = Path(__file__).resolve().parents[4] / "reports" / "langgraph_agent_v42_core_candidate.jsonl"


def trace_writer(state: LangGraphAgentState) -> dict[str, Any]:
    if state.get("trace_enabled") is False:
        return {"trace_written": False}
    if state.get("mode") == "core_candidate":
        trace_path = str(state.get("trace_path") or "")
        return {"trace_written": _write_core_trace(state, trace_path or DEFAULT_CORE_TRACE_PATH)}
    trace_path = str(state.get("trace_path") or "")
    return {"trace_written": write_trace(state, trace_path) if trace_path else write_trace(state)}


def _write_core_trace(state: LangGraphAgentState, trace_path: str | Path) -> bool:
    path = Path(trace_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "langgraph_core_candidate_trace.v4.2",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": state.get("trace_id", ""),
        "session_id": state.get("session_id", ""),
        "message_id": (state.get("message") or {}).get("message_id", "") if isinstance(state.get("message"), dict) else "",
        "raw_text": state.get("raw_text", ""),
        "normalized_text": state.get("normalized_text", ""),
        "core_path": state.get("core_path", ""),
        "plan": state.get("plan", {}),
        "tool_results": state.get("tool_results", []),
        "core_candidate": state.get("core_candidate", {}),
        "action_summary": state.get("action_summary", ""),
        "final_reply": state.get("final_reply", ""),
        "guard_result": state.get("guard_result", {}),
        "errors": state.get("errors", []),
    }
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return True


__all__ = ["trace_writer"]
