from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any

from .user_context import UserContext


class IdentityResolutionError(RuntimeError):
    def __init__(self, code: str, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "error",
            "error": self.code,
            "message": str(self),
            "details": self.details,
        }


def resolve_or_create_user_context(event_payload: Mapping[str, Any], storage: Any) -> UserContext:
    if storage is None or not callable(getattr(storage, "resolve_or_create_user", None)):
        raise IdentityResolutionError(
            "storage_unavailable",
            "A storage repository with resolve_or_create_user is required.",
        )

    fields = extract_feishu_event_fields(event_payload)
    missing = [key for key in ("feishu_app_id", "feishu_open_id") if not fields[key]]
    if missing:
        raise IdentityResolutionError(
            "missing_identity_field",
            "Missing required Feishu identity fields.",
            {"missing": missing},
        )

    try:
        resolved = storage.resolve_or_create_user(
            fields["feishu_app_id"],
            fields["feishu_open_id"],
            fields["chat_id"],
        )
    except Exception as exc:
        raise IdentityResolutionError(
            "storage_resolution_failed",
            "Failed to resolve or create V3 user context.",
            {"type": type(exc).__name__, "message": str(exc)},
        ) from exc

    if not isinstance(resolved, Mapping):
        raise IdentityResolutionError(
            "invalid_storage_result",
            "Storage resolve_or_create_user must return a mapping.",
            {"type": type(resolved).__name__},
        )

    tenant_id = _clean(resolved.get("tenant_id"))
    user_id = _clean(resolved.get("user_id"))
    if not tenant_id or not user_id:
        raise IdentityResolutionError(
            "invalid_storage_result",
            "Storage result must include tenant_id and user_id.",
            {"keys": sorted(str(key) for key in resolved.keys())},
        )

    return UserContext(
        tenant_id=tenant_id,
        user_id=user_id,
        feishu_app_id=fields["feishu_app_id"],
        feishu_open_id=fields["feishu_open_id"],
        chat_id=fields["chat_id"],
        message_id=fields["message_id"],
        raw_text=fields["raw_text"],
        event_time=fields["event_time"],
    )


def extract_feishu_event_fields(event_payload: Mapping[str, Any]) -> dict[str, str]:
    header = _mapping(event_payload.get("header"))
    event = _mapping(event_payload.get("event"))
    message = _mapping(event.get("message")) or _mapping(event_payload.get("message"))
    sender = _mapping(event.get("sender")) or _mapping(event_payload.get("sender"))
    sender_id = _mapping(sender.get("sender_id"))

    return {
        "feishu_app_id": _first_text(
            event_payload.get("feishu_app_id"),
            header.get("app_id"),
            event.get("app_id"),
            event_payload.get("app_id"),
        ),
        "feishu_open_id": _first_text(
            event_payload.get("feishu_open_id"),
            sender_id.get("open_id"),
            event_payload.get("open_id"),
        ),
        "chat_id": _first_text(event_payload.get("chat_id"), message.get("chat_id")),
        "message_id": _first_text(event_payload.get("message_id"), message.get("message_id")),
        "raw_text": _first_text(
            event_payload.get("raw_text"),
            event_payload.get("text"),
            _message_text(message),
        ),
        "event_time": _coerce_event_time(
            _first_text(
                event_payload.get("event_time"),
                message.get("create_time"),
                header.get("create_time"),
                event.get("create_time"),
            )
        ),
    }


def _message_text(message: Mapping[str, Any]) -> str:
    raw_content = message.get("content")
    if isinstance(raw_content, str):
        try:
            content = json.loads(raw_content)
        except json.JSONDecodeError:
            return raw_content.strip()
    elif isinstance(raw_content, Mapping):
        content = raw_content
    else:
        return ""

    return _clean(content.get("text"))


def _coerce_event_time(value: Any) -> str:
    text = _clean(value)
    if not text:
        return ""
    if not text.isdigit():
        return text

    timestamp = int(text)
    if timestamp > 10_000_000_000:
        timestamp = timestamp / 1000
    try:
        return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    except (OSError, OverflowError, ValueError):
        return text


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _first_text(*values: Any) -> str:
    for value in values:
        text = _clean(value)
        if text:
            return text
    return ""


def _clean(value: Any) -> str:
    return str(value or "").strip()
