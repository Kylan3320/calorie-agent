from __future__ import annotations

from pathlib import Path
from typing import Any

from .graph import run_langgraph_agent
from .tools.fake_write_adapter import FakeWriteLegacy, WRITE_ALLOWED_TOOLS


DEFAULT_WRITE_TRACE_PATH = Path(__file__).resolve().parents[3] / "reports" / "langgraph_agent_v31_write_candidate.jsonl"


def run_write_candidate(
    text: str,
    *,
    user_id: str = "local_user",
    session_id: str = "local_session",
    message: dict[str, str] | None = None,
    planner_backend: str = "deterministic",
    allowed_tools: set[str] | None = None,
    legacy: FakeWriteLegacy | None = None,
    fixture_mode: str = "",
    plan_override: dict[str, Any] | None = None,
    trace_path: str | Path | None = DEFAULT_WRITE_TRACE_PATH,
    write_trace_enabled: bool = True,
) -> dict[str, Any]:
    candidate_message = {
        "message_id": f"lgv31_{session_id}",
        "user_id": user_id,
        "chat_id": session_id,
        "session_id": session_id,
        "text": str(text or ""),
        "mode": "write_candidate",
        "planner_backend": planner_backend,
        **(message or {}),
    }
    result = run_langgraph_agent(
        candidate_message,
        settings={
            "planner_backend": planner_backend,
            "write_allowed_tools": sorted(allowed_tools or WRITE_ALLOWED_TOOLS),
            "write_legacy": legacy or FakeWriteLegacy(),
            "fixture_mode": fixture_mode,
            "plan_override": plan_override or {},
            "trace_path": str(trace_path) if trace_path is not None else "",
            "trace_enabled": bool(write_trace_enabled),
        },
    )
    trace = result.get("trace") if isinstance(result.get("trace"), dict) else {}
    return dict(trace)
