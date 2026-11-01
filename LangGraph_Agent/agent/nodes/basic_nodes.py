"""Simple nodes: normalize input, load pending context, write trace."""

from __future__ import annotations

import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..state import AgentState

TRACE_SCHEMA_VERSION = "calorie_agent_trace.v1"


def input_normalizer(state: AgentState) -> dict[str, Any]:
    # NFKC 归一化：全角数字/字母（５０ｇ）转半角，避免规划器漏检
    text = unicodedata.normalize("NFKC", str(state.get("raw_text") or ""))
    text = re.sub(r"\s+", " ", text).strip()
    return {"normalized_text": text}


def context_loader(state: AgentState) -> dict[str, Any]:
    store = state.get("store")
    message = state.get("message") or {}
    errors = list(state.get("errors") or [])
    active_pending = None
    execution = state.get("execution")
    if execution:
        return {"context": {"active_pending": execution["payload"].get("active_pending")}}
    try:
        active_pending = store.load_active_pending(message.get("user_id", ""), message.get("chat_id", ""))
    except Exception as exc:
        raise RuntimeError("pending_context_unavailable") from exc
    print(f"[context_loader] active_pending={'yes:' + str(active_pending.get('intent')) if active_pending else 'none'} errors={errors}")
    return {"context": {"active_pending": active_pending}, "errors": errors}


def trace_writer(state: AgentState) -> dict[str, Any]:
    latency_ms = _latency(state)
    payload = _trace_payload({**state, "latency_ms": latency_ms})
    line = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    # trace 同步打到 stdout：云函数请求日志里立即可见（容器内文件仅作留档）
    print(f"[trace] {line}")
    if str(os.getenv("TRACE_ENABLED", "true")).lower() in {"false", "0", "no"}:
        return {"trace_written": False, "latency_ms": latency_ms}
    path = Path(os.getenv("TRACE_PATH") or "/tmp/traces.jsonl")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
    except OSError as exc:
        return {"trace_written": False, "latency_ms": latency_ms, "errors": [*list(state.get("errors") or []), f"trace_write_failed:{exc}"]}
    return {"trace_written": True, "latency_ms": latency_ms}


def _latency(state: AgentState) -> int:
    started = float(state.get("started_at") or time.monotonic())
    return int((time.monotonic() - started) * 1000)


def _trace_payload(state: AgentState) -> dict[str, Any]:
    message = state.get("message") or {}
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "trace_id": state.get("trace_id", ""),
        "message_id": message.get("message_id", ""),
        "user_id": message.get("user_id", ""),
        "chat_id": message.get("chat_id", ""),
        "preset": getattr(state.get("preset"), "name", ""),
        "planner": getattr(state.get("config"), "planner", ""),
        "raw_text": state.get("raw_text", ""),
        "normalized_text": state.get("normalized_text", ""),
        "pending_route": state.get("pending_route", {}),
        "plan": state.get("plan", {}),
        "planner_fallback": bool(state.get("planner_fallback")),
        "policy_decision": state.get("policy_decision", {}),
        "tool_results": state.get("tool_results", []),
        "observations": state.get("observations", []),
        "final_reply": state.get("final_reply", ""),
        "reply_source": state.get("reply_source", ""),
        "guard": state.get("guard_result", {}),
        "errors": state.get("errors", []),
        "latency_ms": state.get("latency_ms", 0),
    }


__all__ = ["context_loader", "input_normalizer", "trace_writer"]
