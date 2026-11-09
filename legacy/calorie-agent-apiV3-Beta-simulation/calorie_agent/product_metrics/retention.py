from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any


_ACTIVATION_EVENTS = {"record_food_success", "record_food"}
_EFFECTIVE_EVENTS = {
    "record_food_success",
    "record_food",
    "query_today_success",
    "query_today",
    "undo_success",
    "undo",
    "pending_created",
    "pending_completed",
    "feedback_submitted",
    "feedback",
}


def calculate_activation(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    activations = _activation_by_user(events)
    return {
        "activation_count": len(activations),
        "activated_users": sorted(activations),
        "activation_by_user": {user_id: timestamp.isoformat().replace("+00:00", "Z") for user_id, timestamp in sorted(activations.items())},
    }


def calculate_d1_retention(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    return _calculate_retention(events, label="D1", target_day=1, window_before=0, window_after=0)


def calculate_d7_retention(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    return _calculate_retention(events, label="D7", target_day=7, window_before=1, window_after=1)


def calculate_w4_retention(events: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    return _calculate_retention(events, label="W4", target_day=28, window_before=0, window_after=6)


def _calculate_retention(
    events: Iterable[Mapping[str, Any]],
    *,
    label: str,
    target_day: int,
    window_before: int,
    window_after: int,
) -> dict[str, Any]:
    rows = [event for event in events if isinstance(event, Mapping)]
    activations = _activation_by_user(rows)
    events_by_user: dict[str, list[tuple[datetime, Mapping[str, Any]]]] = defaultdict(list)
    observed_until: date | None = None
    for event in rows:
        timestamp = _event_datetime(event)
        if timestamp is None:
            continue
        observed_until = max(observed_until, timestamp.date()) if observed_until else timestamp.date()
        user_id = _user_id(event)
        if user_id:
            events_by_user[user_id].append((timestamp, event))

    eligible_users: list[str] = []
    retained_users: list[str] = []
    for user_id, activation_at in activations.items():
        window_start = activation_at.date() + timedelta(days=target_day - window_before)
        window_end = activation_at.date() + timedelta(days=target_day + window_after)
        if observed_until is not None and observed_until < window_start:
            continue
        eligible_users.append(user_id)
        if any(
            window_start <= timestamp.date() <= window_end
            and timestamp.date() > activation_at.date()
            and _is_effective_event(event)
            for timestamp, event in events_by_user.get(user_id, [])
        ):
            retained_users.append(user_id)

    eligible_count = len(eligible_users)
    retained_count = len(retained_users)
    return {
        "window": label,
        "target_day": target_day,
        "eligible_users": sorted(eligible_users),
        "eligible_count": eligible_count,
        "retained_users": sorted(retained_users),
        "retained_count": retained_count,
        "retention_rate": retained_count / eligible_count if eligible_count else 0.0,
    }


def _activation_by_user(events: Iterable[Mapping[str, Any]]) -> dict[str, datetime]:
    activations: dict[str, datetime] = {}
    for event in events:
        if not isinstance(event, Mapping) or not _is_success(event):
            continue
        if str(event.get("event_type") or "").strip() not in _ACTIVATION_EVENTS:
            continue
        user_id = _user_id(event)
        timestamp = _event_datetime(event)
        if not user_id or timestamp is None:
            continue
        if user_id not in activations or timestamp < activations[user_id]:
            activations[user_id] = timestamp
    return activations


def _is_effective_event(event: Mapping[str, Any]) -> bool:
    return _is_success(event) and str(event.get("event_type") or "").strip() in _EFFECTIVE_EVENTS


def _is_success(event: Mapping[str, Any]) -> bool:
    value = event.get("success", True)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", ""}


def _user_id(event: Mapping[str, Any]) -> str:
    return str(event.get("user_id") or "").strip()


def _event_datetime(event: Mapping[str, Any]) -> datetime | None:
    value = event.get("created_at")
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    text = str(value or "").strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        if " " in text and "T" not in text:
            text = text.replace(" ", "T")
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
