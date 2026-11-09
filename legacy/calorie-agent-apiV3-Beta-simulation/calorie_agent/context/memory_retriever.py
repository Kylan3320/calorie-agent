from __future__ import annotations

from copy import deepcopy
from typing import Any


MEMORY_KEYS = (
    "aliases",
    "default_grams",
    "combos",
    "preferences",
    "planner_profile",
    "member_relations_summary",
    "permissions_summary",
)

SENSITIVE_KEY_FRAGMENTS = (
    "api_key",
    "app_secret",
    "access_token",
    "tenant_access_token",
    "authorization",
    "password",
    "secret",
)


def empty_memory_summary() -> dict[str, Any]:
    return {
        "aliases": [],
        "default_grams": [],
        "combos": [],
        "preferences": {},
        "planner_profile": {},
        "member_relations_summary": {"source": "not_configured", "members": []},
        "permissions_summary": {"source": "not_configured", "rules": []},
    }


def load_user_memory_summary(
    user_id: str,
    message: str,
    provided_memory: dict[str, Any] | None = None,
    max_items: int = 5,
) -> dict[str, Any]:
    if not provided_memory:
        return empty_memory_summary()

    summary = empty_memory_summary()
    sanitized = _sanitize_value(deepcopy(provided_memory))
    if not isinstance(sanitized, dict):
        return summary

    for key in MEMORY_KEYS:
        if key not in sanitized:
            continue
        value = sanitized[key]
        if isinstance(value, list):
            summary[key] = _filter_user_items(value, user_id, max_items)
        elif isinstance(value, dict):
            summary[key] = _filter_user_dict(value, user_id, max_items)
        else:
            summary[key] = value

    return summary


def _filter_user_items(items: list[Any], user_id: str, max_items: int) -> list[Any]:
    filtered: list[Any] = []
    for item in items:
        if isinstance(item, dict):
            item_user_id = str(item.get("user_id") or item.get("owner_user_id") or "")
            if item_user_id and user_id and item_user_id != user_id:
                continue
        filtered.append(item)
        if len(filtered) >= max_items:
            break
    return filtered


def _filter_user_dict(value: dict[str, Any], user_id: str, max_items: int) -> dict[str, Any]:
    item_user_id = str(value.get("user_id") or value.get("owner_user_id") or "")
    if item_user_id and user_id and item_user_id != user_id:
        return {}

    result: dict[str, Any] = {}
    for index, (key, item) in enumerate(value.items()):
        if index >= max_items:
            break
        result[key] = item
    return result


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if _is_sensitive_key(str(key)):
                continue
            result[key] = _sanitize_value(item)
        return result
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower()
    return any(fragment in normalized for fragment in SENSITIVE_KEY_FRAGMENTS)
