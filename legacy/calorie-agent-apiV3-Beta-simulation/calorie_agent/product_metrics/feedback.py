from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any


_ACCURACY_MARKERS = ("不准", "不准确", "错了", "算错", "热量不对", "营养不对")
_USABILITY_MARKERS = ("不好用", "太麻烦", "看不懂", "用不了", "难用")


def detect_feedback(text: Any, *, related_message_id: str = "") -> dict[str, Any] | None:
    value = str(text or "").strip()
    if not value:
        return None

    feedback_type = ""
    content = value

    if value.startswith("反馈:"):
        feedback_type = "general"
        content = value.split(":", 1)[1].strip()
    elif value.startswith("反馈："):
        feedback_type = "general"
        content = value.split("：", 1)[1].strip()
    elif value.startswith("建议"):
        feedback_type = "suggestion"
        content = value[2:].lstrip(" ：:，,")
    elif _contains_any(value, _ACCURACY_MARKERS):
        feedback_type = "accuracy"
    elif _contains_any(value, _USABILITY_MARKERS):
        feedback_type = "usability"
    else:
        return None

    if _contains_any(content, _ACCURACY_MARKERS):
        feedback_type = "accuracy"
    elif _contains_any(content, _USABILITY_MARKERS) and feedback_type == "general":
        feedback_type = "usability"

    return {
        "feedback_type": feedback_type or "general",
        "content": content or value,
        "related_message_id": related_message_id,
        "sentiment": _sentiment(content or value),
        "status": "open",
    }


def build_feedback_sample(
    *,
    tenant_id: Any = "",
    user_id: Any = "",
    message_id: Any = "",
    text: Any = "",
    related_message_id: str = "",
    created_at: Any = None,
) -> dict[str, Any] | None:
    detected = detect_feedback(text, related_message_id=related_message_id)
    if detected is None:
        return None
    return {
        "tenant_id": str(tenant_id or "").strip(),
        "user_id": str(user_id or "").strip(),
        "message_id": str(message_id or "").strip(),
        "feedback_type": detected["feedback_type"],
        "content": detected["content"],
        "related_message_id": detected["related_message_id"],
        "sentiment": detected["sentiment"],
        "status": detected["status"],
        "metadata_json": {
            "source": "natural_language_feedback",
            "related_message_id": related_message_id,
        },
        "created_at": _created_at(created_at),
    }


def record_feedback_sample(storage: Any, feedback_payload: Mapping[str, Any]) -> dict[str, Any]:
    writer = getattr(storage, "record_feedback_sample", None)
    if not callable(writer):
        raise TypeError("storage must provide record_feedback_sample(feedback_payload)")
    return writer(dict(feedback_payload))


def safe_record_feedback_sample(storage: Any, feedback_payload: Mapping[str, Any]) -> dict[str, Any]:
    try:
        result = record_feedback_sample(storage, feedback_payload)
        if isinstance(result, dict):
            return result
        return {"created": True, "result": result}
    except Exception as exc:
        return {
            "created": False,
            "error_type": type(exc).__name__,
            "error": str(exc),
        }


def _sentiment(text: str) -> str:
    if _contains_any(text, _ACCURACY_MARKERS + _USABILITY_MARKERS):
        return "negative"
    return "neutral"


def _contains_any(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers)


def _created_at(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    text = str(value or "").strip()
    return text or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
