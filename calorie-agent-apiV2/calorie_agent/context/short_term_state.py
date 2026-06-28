from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Any


def load_short_term_state(
    message: str,
    recent_action_results: list[dict[str, Any]] | None = None,
    active_pending_summary: dict[str, Any] | None = None,
    last_message_summary: dict[str, Any] | None = None,
    current_date: str | None = None,
    timezone_name: str = "UTC+8",
) -> tuple[dict[str, Any], list[str]]:
    warnings: list[str] = []
    state: dict[str, Any] = {
        "current_date": current_date or _today_for_timezone(timezone_name),
        "timezone": timezone_name,
    }

    try:
        if active_pending_summary is not None:
            state["active_pending_summary"] = active_pending_summary
        elif _mentions_pending(message):
            state["active_pending_summary"] = {"source": "not_configured", "items": []}

        if recent_action_results is not None:
            state["recent_action_results"] = recent_action_results[:5]
        elif _mentions_recent_action(message):
            state["recent_action_results"] = {"source": "not_configured", "items": []}

        if last_message_summary is not None:
            state["last_message_summary"] = last_message_summary
        elif _mentions_recent_action(message):
            state["last_message_summary"] = {"source": "not_configured"}
    except Exception as exc:
        warnings.append(f"short_term_state_failed:{type(exc).__name__}")

    return state, warnings


def _mentions_pending(message: str) -> bool:
    text = (message or "").lower()
    keywords = ("pending", "确认", "继续", "补充", "刚才那个", "待确认")
    return any(keyword in text for keyword in keywords)


def _mentions_recent_action(message: str) -> bool:
    text = (message or "").lower()
    keywords = (
        "undo",
        "delete",
        "撤销",
        "删除",
        "上一条",
        "上上条",
        "之前",
        "刚才",
        "那条",
    )
    return any(keyword in text for keyword in keywords)


def _today_for_timezone(timezone_name: str) -> str:
    offset = _parse_utc_offset(timezone_name)
    return datetime.now(timezone(offset)).date().isoformat()


def _parse_utc_offset(timezone_name: str) -> timedelta:
    text = timezone_name.replace("UTC", "").strip()
    if not text:
        return timedelta(hours=8)
    sign = -1 if text.startswith("-") else 1
    text = text.lstrip("+-")
    try:
        hours_text, _, minutes_text = text.partition(":")
        hours = int(hours_text or "8")
        minutes = int(minutes_text or "0")
    except ValueError:
        return timedelta(hours=8)
    return sign * timedelta(hours=hours, minutes=minutes)
