from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Any
import uuid


class EventType(str, Enum):
    MESSAGE_RECEIVED = "message_received"
    RECORD_FOOD_SUCCESS = "record_food_success"
    RECORD_FOOD_FAILED = "record_food_failed"
    QUERY_TODAY_SUCCESS = "query_today_success"
    UNDO_SUCCESS = "undo_success"
    PENDING_CREATED = "pending_created"
    PENDING_COMPLETED = "pending_completed"
    FEEDBACK_SUBMITTED = "feedback_submitted"
    PLANNER_FAILED = "planner_failed"
    TOOL_FAILED = "tool_failed"
    REPLY_SENT = "reply_sent"


P0_EVENT_TYPES = {item.value for item in EventType}
LEGACY_EVENT_TYPES = {"record_food", "query_today", "undo", "feedback", "error"}
VALID_EVENT_TYPES = P0_EVENT_TYPES | LEGACY_EVENT_TYPES

_FAILURE_STATUSES = {"error", "failed", "failure", "permission_denied", "blocked"}
_SENSITIVE_KEY_PARTS = (
    "api_key",
    "authorization",
    "chat_history",
    "cookie",
    "conversation",
    "deepseek",
    "final_reply",
    "history",
    "password",
    "prompt",
    "raw_text",
    "secret",
    "token",
    "transcript",
)


def normalize_usage_event(event_payload: Mapping[str, Any]) -> dict[str, Any]:
    metadata = event_payload.get("metadata_json")
    if metadata is None:
        metadata = event_payload.get("metadata")

    return {
        "event_id": str(event_payload.get("event_id") or uuid.uuid4()),
        "tenant_id": _clean_text(event_payload.get("tenant_id")),
        "user_id": _clean_text(event_payload.get("user_id")),
        "message_id": _clean_text(event_payload.get("message_id")),
        "event_type": _event_type_value(event_payload.get("event_type")),
        "success": _bool(event_payload.get("success", True)),
        "latency_ms": _int(event_payload.get("latency_ms")),
        "error_type": _clean_text(event_payload.get("error_type")),
        "metadata_json": _sanitize_metadata(metadata),
        "created_at": _created_at(event_payload.get("created_at")),
    }


def build_usage_event(
    *,
    tenant_id: Any = "",
    user_id: Any = "",
    message_id: Any = "",
    event_type: EventType | str,
    success: Any = True,
    latency_ms: Any = 0,
    error_type: Any = "",
    metadata_json: Mapping[str, Any] | None = None,
    created_at: Any = None,
    event_id: Any = None,
) -> dict[str, Any]:
    return normalize_usage_event(
        {
            "event_id": event_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "message_id": message_id,
            "event_type": event_type,
            "success": success,
            "latency_ms": latency_ms,
            "error_type": error_type,
            "metadata_json": metadata_json or {},
            "created_at": created_at,
        }
    )


def build_usage_events_from_runtime_result(
    message: Mapping[str, Any],
    result: Mapping[str, Any],
    *,
    created_at: Any = None,
) -> list[dict[str, Any]]:
    metrics = result.get("agent_metrics") if isinstance(result.get("agent_metrics"), Mapping) else {}
    trace_like = {
        "trace_id": result.get("trace_id") or "",
        "message_id": message.get("message_id") or result.get("message_id") or "",
        "tenant_id": message.get("tenant_id") or result.get("tenant_id") or "",
        "user_id": message.get("user_id") or result.get("user_id") or "",
        "planner_prompt_version": result.get("planner_prompt_version") or "",
        "plan_json": result.get("parse_result") if isinstance(result.get("parse_result"), Mapping) else {},
        "action_count": _int((result.get("parse_result") or {}).get("action_count") if isinstance(result.get("parse_result"), Mapping) else 0),
        "tool_results": result.get("tool_results") if isinstance(result.get("tool_results"), list) else [],
        "final_reply": result.get("reply") or "",
        "fallback_used": bool(metrics.get("planner_fallback_used")),
        "error_type": metrics.get("reply_error_type") or result.get("error_type") or "",
        "latency_ms": metrics.get("agent_runtime_latency_ms") or result.get("latency_ms") or 0,
        "reply_guard": result.get("reply_guard") if isinstance(result.get("reply_guard"), Mapping) else {},
    }
    return build_usage_events_from_trace(trace_like, created_at=created_at)


def build_usage_events_from_trace(trace: Any, *, created_at: Any = None) -> list[dict[str, Any]]:
    payload = _as_mapping(trace)
    plan_json = payload.get("plan_json") if isinstance(payload.get("plan_json"), Mapping) else {}
    tool_results = payload.get("tool_results") if isinstance(payload.get("tool_results"), list) else []
    action_count = _int(payload.get("action_count") or plan_json.get("action_count"))
    base = {
        "tenant_id": payload.get("tenant_id"),
        "user_id": payload.get("user_id"),
        "message_id": payload.get("message_id"),
        "latency_ms": payload.get("latency_ms"),
        "created_at": created_at,
    }
    trace_meta = _trace_metadata(payload, action_count)

    events = [
        build_usage_event(
            **base,
            event_type=EventType.MESSAGE_RECEIVED,
            success=True,
            metadata_json={**trace_meta, "stage": "inbound"},
        )
    ]

    error_type = _clean_text(payload.get("error_type"))
    if error_type and not tool_results and action_count == 0:
        events.append(
            build_usage_event(
                **base,
                event_type=EventType.PLANNER_FAILED,
                success=False,
                error_type=error_type,
                metadata_json={**trace_meta, "stage": "planner"},
            )
        )

    for item in tool_results:
        if isinstance(item, Mapping):
            events.extend(_events_from_tool_result(item, base, trace_meta))

    if _clean_text(payload.get("final_reply")):
        reply_guard = payload.get("reply_guard") if isinstance(payload.get("reply_guard"), Mapping) else {}
        events.append(
            build_usage_event(
                **base,
                event_type=EventType.REPLY_SENT,
                success=True,
                metadata_json={
                    **trace_meta,
                    "stage": "reply",
                    "reply_guard_passed": bool(reply_guard.get("passed", True)),
                },
            )
        )

    return events


def record_usage_event(storage: Any, event_payload: Mapping[str, Any]) -> dict[str, Any]:
    writer = getattr(storage, "record_usage_event", None)
    if not callable(writer):
        raise TypeError("storage must provide record_usage_event(event_payload)")
    return writer(normalize_usage_event(event_payload))


def safe_record_usage_event(storage: Any, event_payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        result = record_usage_event(storage, event_payload)
        if isinstance(result, dict):
            return result
        return {"created": True, "result": result}
    except Exception as exc:
        return {
            "created": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def _events_from_tool_result(
    result: Mapping[str, Any],
    base: Mapping[str, Any],
    trace_meta: Mapping[str, Any],
) -> list[dict[str, Any]]:
    tool = _clean_text(result.get("tool"))
    status = _clean_text(result.get("status")).lower()
    error_type = _clean_text(result.get("error") or status)
    metadata = {
        **trace_meta,
        "stage": "tool",
        "tool": tool,
        "tool_status": status,
        "action_id": _clean_text(result.get("action_id")),
    }
    events: list[dict[str, Any]] = []

    if tool == "record_food":
        if _is_pending_created(result):
            events.append(build_usage_event(**base, event_type=EventType.PENDING_CREATED, metadata_json=metadata))
        elif _is_failure_status(status):
            events.append(
                build_usage_event(
                    **base,
                    event_type=EventType.RECORD_FOOD_FAILED,
                    success=False,
                    error_type=error_type,
                    metadata_json=metadata,
                )
            )
        else:
            events.append(build_usage_event(**base, event_type=EventType.RECORD_FOOD_SUCCESS, metadata_json=metadata))
    elif tool == "query_today" and not _is_failure_status(status):
        events.append(build_usage_event(**base, event_type=EventType.QUERY_TODAY_SUCCESS, metadata_json=metadata))
    elif tool == "undo_intake" and not _is_failure_status(status):
        events.append(build_usage_event(**base, event_type=EventType.UNDO_SUCCESS, metadata_json=metadata))
    elif tool == "create_pending" and not _is_failure_status(status):
        events.append(build_usage_event(**base, event_type=EventType.PENDING_CREATED, metadata_json=metadata))

    if _is_pending_completed(result):
        events.append(build_usage_event(**base, event_type=EventType.PENDING_COMPLETED, metadata_json=metadata))

    if _is_failure_status(status):
        events.append(
            build_usage_event(
                **base,
                event_type=EventType.TOOL_FAILED,
                success=False,
                error_type=error_type,
                metadata_json=metadata,
            )
        )

    return events


def _is_pending_created(result: Mapping[str, Any]) -> bool:
    status = _clean_text(result.get("status")).lower()
    if status in {"pending", "pending_created"}:
        return True
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    pending = data.get("pending_result") if isinstance(data.get("pending_result"), Mapping) else {}
    return bool(pending and _clean_text(pending.get("status")).lower() in {"created", "pending"})


def _is_pending_completed(result: Mapping[str, Any]) -> bool:
    status = _clean_text(result.get("status")).lower()
    if status == "completed":
        return True
    data = result.get("data") if isinstance(result.get("data"), Mapping) else {}
    pending = data.get("pending_result") if isinstance(data.get("pending_result"), Mapping) else {}
    return _clean_text(pending.get("status")).lower() == "completed"


def _is_failure_status(status: str) -> bool:
    if not status:
        return False
    return status in _FAILURE_STATUSES or status.endswith("_failed")


def _trace_metadata(payload: Mapping[str, Any], action_count: int) -> dict[str, Any]:
    return {
        "schema_version": "product_metrics.usage_event.v1",
        "source": "agent_trace",
        "trace_id": _clean_text(payload.get("trace_id")),
        "planner_prompt_version": _clean_text(payload.get("planner_prompt_version")),
        "action_count": action_count,
    }


def _event_type_value(value: Any) -> str:
    if isinstance(value, EventType):
        text = value.value
    else:
        text = _clean_text(value)
    if text not in VALID_EVENT_TYPES:
        raise ValueError(f"unsupported usage event type: {text}")
    return text


def _sanitize_metadata(value: Any, depth: int = 0) -> dict[str, Any] | list[Any] | str | int | float | bool | None:
    if depth > 3:
        return None
    if isinstance(value, Mapping):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            key_text = _clean_text(key)
            if not key_text or _is_sensitive_key(key_text):
                continue
            clean[key_text] = _sanitize_metadata(item, depth + 1)
        return clean
    if isinstance(value, list):
        return [_sanitize_metadata(item, depth + 1) for item in value[:10]]
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    text = _clean_text(value)
    if len(text) > 200:
        return f"{text[:200]}..."
    return text


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def _as_mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict()
        if isinstance(converted, Mapping):
            return converted
    return {}


def _created_at(value: Any) -> str:
    if isinstance(value, datetime):
        dt = value.astimezone(timezone.utc)
        return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    text = _clean_text(value)
    return text or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}
