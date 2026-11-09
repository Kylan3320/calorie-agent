from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

from .. import legacy_app


TRACE_SCHEMA_VERSION = "agent_trace.v1"


@dataclass(frozen=True)
class AgentTrace:
    trace_id: str
    message_id: str
    user_id: str
    raw_text: str
    planner_prompt_version: str
    plan_json: dict[str, Any] = field(default_factory=dict)
    action_count: int = 0
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    execution_facts: dict[str, Any] = field(default_factory=dict)
    final_reply: str = ""
    fallback_used: bool = False
    error_type: str = ""
    latency_ms: int = 0
    reply_guard: dict[str, Any] = field(default_factory=dict)
    tenant_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": TRACE_SCHEMA_VERSION,
            "trace_id": self.trace_id,
            "message_id": self.message_id,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
            "raw_text": self.raw_text,
            "planner_prompt_version": self.planner_prompt_version,
            "plan_json": self.plan_json,
            "action_count": self.action_count,
            "tool_results": self.tool_results,
            "execution_facts": self.execution_facts,
            "final_reply": self.final_reply,
            "fallback_used": self.fallback_used,
            "error_type": self.error_type,
            "latency_ms": self.latency_ms,
            "reply_guard": self.reply_guard,
        }


def make_trace_id(message: dict[str, str]) -> str:
    key = str(message.get("message_id") or f"{message.get('chat_id', '')}:{message.get('text', '')}")
    token_bytes = bytearray(hashlib.sha256(f"calorie-agent:{key}:trace".encode("utf-8")).digest()[:16])
    token_bytes[6] = (token_bytes[6] & 0x0F) | 0x40
    token_bytes[8] = (token_bytes[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(token_bytes)))


def emit_agent_trace(
    trace: AgentTrace,
    settings: dict[str, str],
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "agent_trace", "trace": trace.to_dict()}
    try:
        write = _write_trace_if_configured(trace, settings, legacy=legacy)
        if write:
            result["trace_write_result"] = write
    except Exception as exc:
        result["trace_write_error"] = str(exc)
    product_metrics = _write_product_metrics_if_configured(trace, settings)
    if product_metrics:
        result["product_metrics_write_result"] = product_metrics
    print(json.dumps(result, ensure_ascii=False), flush=True)
    return result


def build_error_trace(
    message: dict[str, str],
    planner_prompt_version: str,
    started_at: float,
    error_type: str,
    error: Exception,
) -> AgentTrace:
    return AgentTrace(
        trace_id=make_trace_id(message),
        message_id=str(message.get("message_id") or ""),
        tenant_id=str(message.get("tenant_id") or ""),
        user_id=str(message.get("user_id") or ""),
        raw_text=str(message.get("text") or ""),
        planner_prompt_version=planner_prompt_version,
        fallback_used=True,
        error_type=f"{error_type}:{type(error).__name__}",
        latency_ms=int((time.monotonic() - started_at) * 1000),
    )


def _write_trace_if_configured(
    trace: AgentTrace,
    settings: dict[str, str],
    legacy: ModuleType,
) -> dict[str, Any] | None:
    app_token = settings.get("bitable_app_token") or ""
    table_id = settings.get("bitable_agent_trace_table_id") or ""
    if not app_token or not table_id:
        return None

    fields = _trace_fields(trace, settings)
    client_token = legacy._stable_uuid4_from_text(f"calorie-agent:{trace.trace_id}:agent-trace")
    return legacy._batch_create_bitable_records(
        app_token,
        table_id,
        [{"fields": fields}],
        client_token=client_token,
    )


def _write_product_metrics_if_configured(
    trace: AgentTrace,
    settings: dict[str, Any],
) -> dict[str, Any] | None:
    if not _truthy(settings.get("product_metrics_enabled") or settings.get("v3_product_metrics_enabled")):
        return None
    storage = settings.get("_product_metrics_storage")
    if storage is None:
        return {"status": "skipped", "reason": "missing_product_metrics_storage"}

    try:
        from ..product_metrics.events import build_usage_events_from_trace, safe_record_usage_event

        events = build_usage_events_from_trace(trace.to_dict())
        results = [safe_record_usage_event(storage, event) for event in events]
        return {
            "status": "recorded",
            "event_count": len(events),
            "results": results,
        }
    except Exception as exc:
        return {"status": "error", "error_type": type(exc).__name__, "error": str(exc)}


def _trace_fields(trace: AgentTrace, settings: dict[str, str]) -> dict[str, Any]:
    payload = trace.to_dict()
    return {
        _field(settings, "trace_id", "trace_id"): trace.trace_id,
        _field(settings, "message_id", "message_id"): trace.message_id,
        _field(settings, "user_id", "user_id"): trace.user_id,
        _field(settings, "raw_text", "raw_text"): trace.raw_text,
        _field(settings, "planner_prompt_version", "planner_prompt_version"): trace.planner_prompt_version,
        _field(settings, "plan_json", "plan_json"): _json_text(payload["plan_json"]),
        _field(settings, "action_count", "action_count"): trace.action_count,
        _field(settings, "tool_results", "tool_results"): _json_text(payload["tool_results"]),
        _field(settings, "execution_facts", "execution_facts"): _json_text(payload["execution_facts"]),
        _field(settings, "final_reply", "final_reply"): trace.final_reply,
        _field(settings, "fallback_used", "fallback_used"): trace.fallback_used,
        _field(settings, "error_type", "error_type"): trace.error_type,
        _field(settings, "latency_ms", "latency_ms"): trace.latency_ms,
        _field(settings, "created_at", "created_at"): int(time.time() * 1000),
    }


def _field(settings: dict[str, str], name: str, default: str) -> str:
    return settings.get(f"bitable_field_trace_{name}") or default


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _truthy(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}
