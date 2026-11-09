from __future__ import annotations

import json
from types import ModuleType
from typing import Any

from .. import legacy_app
from ..domain import combos, memory, preferences


def empty_profile_sources() -> dict[str, Any]:
    return {
        "preferences": [],
        "memory_aliases": [],
        "default_grams": [],
        "combos": [],
        "trace_stats": {},
        "settings": {},
    }


def load_profile_sources(
    user_id: str,
    *,
    include_trace_stats: bool = True,
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    sources = empty_profile_sources()
    user_id = str(user_id or "").strip()
    if not user_id:
        return sources

    message = {"user_id": user_id}
    try:
        settings = legacy._settings()
    except Exception:
        settings = {}
    sources["settings"] = {
        "auto_use_default_grams": settings.get("auto_use_default_grams", "false"),
    }

    try:
        sources["preferences"] = preferences.list_preferences(message, legacy=legacy)
    except Exception:
        sources["preferences"] = []

    try:
        rows = memory.list_user_memories(message, legacy=legacy)
    except Exception:
        rows = []
    sources["memory_aliases"] = [
        row
        for row in rows
        if str(row.get("alias") or "").strip() and str(row.get("food_name") or "").strip()
    ]
    sources["default_grams"] = [row for row in rows if str(row.get("source") or "") == "default_grams"]

    try:
        sources["combos"] = combos.list_combos(message, legacy=legacy)
    except Exception:
        sources["combos"] = []

    if include_trace_stats and not _learning_disabled(sources["preferences"]):
        sources["trace_stats"] = _load_trace_stats(user_id, settings, legacy)

    return sources


def _load_trace_stats(user_id: str, settings: dict[str, str], legacy: ModuleType) -> dict[str, Any]:
    app_token = settings.get("bitable_app_token") or ""
    table_id = settings.get("bitable_agent_trace_table_id") or ""
    if not app_token or not table_id:
        return {}

    fields = [
        _field(settings, "user_id", "user_id"),
        _field(settings, "plan_json", "plan_json"),
    ]
    try:
        records = legacy._list_bitable_records(app_token, table_id, field_names=legacy._unique_non_empty(fields))
    except Exception:
        return {}

    query_counts: dict[str, int] = {}
    meal_patterns: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        raw = record.get("fields")
        if not isinstance(raw, dict):
            continue
        row_user = legacy._extract_text(raw.get(_field(settings, "user_id", "user_id")))
        if row_user and row_user != user_id:
            continue
        plan_json = _json_object(legacy._extract_text(raw.get(_field(settings, "plan_json", "plan_json"))))
        goal = str(plan_json.get("goal") or plan_json.get("intent") or "").strip()
        if goal and "query" in goal:
            query_counts[goal] = query_counts.get(goal, 0) + 1

        actions = plan_json.get("actions") if isinstance(plan_json.get("actions"), list) else []
        for action in actions:
            if not isinstance(action, dict):
                continue
            params = action.get("params") if isinstance(action.get("params"), dict) else action.get("parameters")
            params = params if isinstance(params, dict) else {}
            meal_type = str(params.get("meal_type") or "").strip()
            phrase = str(params.get("meal_phrase") or "").strip()
            if not meal_type or not phrase:
                continue
            key = (phrase, meal_type)
            row = meal_patterns.setdefault(key, {"phrase": phrase, "meal_type": meal_type, "count": 0})
            row["count"] += 1

    common_query_patterns = [
        key for key, _count in sorted(query_counts.items(), key=lambda item: (-item[1], item[0]))[:10]
    ]
    common_meal_patterns = [
        {"phrase": row["phrase"], "meal_type": row["meal_type"], "confidence": min(0.99, 0.5 + row["count"] / 10)}
        for row in sorted(meal_patterns.values(), key=lambda item: (-item["count"], item["phrase"]))[:10]
    ]
    return {
        "common_query_patterns": common_query_patterns,
        "common_meal_patterns": common_meal_patterns,
    }


def _field(settings: dict[str, str], name: str, default: str) -> str:
    return settings.get(f"bitable_field_trace_{name}") or default


def _json_object(text: str) -> dict[str, Any]:
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _learning_disabled(preferences_value: Any) -> bool:
    values: dict[str, Any] = {}
    if isinstance(preferences_value, dict):
        values = preferences_value
    elif isinstance(preferences_value, list):
        for row in preferences_value:
            if not isinstance(row, dict):
                continue
            key = str(row.get("key") or "").strip()
            if key:
                values[key] = row.get("value")

    for key in ("learning_enabled", "auto_learning", "personalization_enabled"):
        if key in values:
            return not _bool_value(values[key], True)
    return False


def _bool_value(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled", "enable"}:
        return True
    if text in {"0", "false", "no", "off", "disabled", "disable"}:
        return False
    return default
