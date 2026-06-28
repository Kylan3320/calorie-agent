from __future__ import annotations

import json
from types import ModuleType
from typing import Any

from .. import legacy_app
from .food_match import normalize_food_name
from .memory import personal_memory_enabled


COMBO_FIELDS = {
    "id": "combo_id",
    "user_id": "user_id",
    "name": "combo_name",
    "meal_type": "meal_type",
    "items_json": "items_json",
    "use_count": "use_count",
    "confidence": "confidence",
    "status": "status",
    "created_at": "created_at",
    "updated_at": "updated_at",
}


def combo_table_enabled(settings: dict[str, str]) -> bool:
    return personal_memory_enabled(settings) and bool(settings.get("bitable_combo_table_id"))


def list_combos(message: dict[str, str], legacy: ModuleType = legacy_app, include_deleted: bool = False) -> list[dict[str, Any]]:
    settings = legacy._settings()
    if not combo_table_enabled(settings):
        return []
    records = legacy._list_bitable_records(
        settings["bitable_app_token"],
        settings["bitable_combo_table_id"],
        field_names=_combo_field_names(settings, legacy),
    )
    user_id = str(message.get("user_id") or "").strip()
    rows = [_combo_from_record(record, settings, legacy) for record in records]
    rows = [row for row in rows if row]
    filtered = []
    for row in rows:
        if user_id and row.get("user_id") != user_id:
            continue
        if not include_deleted and not _active(row.get("status")):
            continue
        filtered.append(row)
    return sorted(filtered, key=lambda row: (-float(row.get("use_count") or 0), str(row.get("combo_name") or "")))


def create_combo(
    message: dict[str, str],
    combo_name: str,
    meal_type: str,
    items: list[dict[str, Any]],
    confidence: float = 1.0,
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    settings = legacy._settings()
    if not combo_table_enabled(settings):
        return {"status": "disabled", "reason": "combo_table_not_configured"}
    user_id = str(message.get("user_id") or "").strip()
    combo_name = str(combo_name or "").strip()
    normalized_items = [_normalize_item(item, meal_type) for item in items if isinstance(item, dict)]
    normalized_items = [item for item in normalized_items if item.get("food_query") and item.get("grams") is not None]
    if not user_id or not combo_name or not normalized_items:
        return {"status": "invalid", "reason": "combo_name_and_items_required"}

    existing = find_combo(message, combo_name, legacy=legacy, include_deleted=True)
    now = legacy._now_millis()
    fields = {
        _combo_field(settings, "user_id"): user_id,
        _combo_field(settings, "name"): combo_name,
        _combo_field(settings, "meal_type"): meal_type or "unknown",
        _combo_field(settings, "items_json"): json.dumps(normalized_items, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        _combo_field(settings, "confidence"): float(confidence),
        _combo_field(settings, "status"): "active",
        _combo_field(settings, "updated_at"): now,
    }
    if existing:
        data = legacy._update_bitable_record(
            settings["bitable_app_token"],
            settings["bitable_combo_table_id"],
            str(existing["record_id"]),
            fields,
        )
        return {"status": "updated", "combo": {**existing, "items": normalized_items}, "update_result": data}

    combo_id = legacy._stable_uuid4_from_text(f"calorie-agent:combo:{user_id}:{normalize_food_name(combo_name)}")
    fields[_combo_field(settings, "id")] = combo_id
    fields[_combo_field(settings, "use_count")] = 0
    fields[_combo_field(settings, "created_at")] = now
    data = legacy._batch_create_bitable_records(
        settings["bitable_app_token"],
        settings["bitable_combo_table_id"],
        [{"fields": fields}],
        client_token=legacy._stable_uuid4_from_text(f"calorie-agent:combo-create:{combo_id}"),
    )
    return {"status": "created", "combo_id": combo_id, "combo_name": combo_name, "items": normalized_items, "create_result": data}


def use_combo(message: dict[str, str], combo_name: str, legacy: ModuleType = legacy_app) -> dict[str, Any]:
    combo = find_combo(message, combo_name, legacy=legacy)
    if not combo:
        return {"status": "not_found", "combo_name": combo_name}
    _increment_use_count(combo, legacy)
    return {"status": "found", "combo": combo, "items": combo.get("items", [])}


def delete_combo(message: dict[str, str], combo_name: str, legacy: ModuleType = legacy_app) -> dict[str, Any]:
    settings = legacy._settings()
    if not combo_table_enabled(settings):
        return {"status": "disabled", "deleted_count": 0}
    combo = find_combo(message, combo_name, legacy=legacy)
    if not combo:
        return {"status": "not_found", "deleted_count": 0}
    data = legacy._update_bitable_record(
        settings["bitable_app_token"],
        settings["bitable_combo_table_id"],
        str(combo["record_id"]),
        {
            _combo_field(settings, "status"): "deleted",
            _combo_field(settings, "updated_at"): legacy._now_millis(),
        },
    )
    return {"status": "deleted", "deleted_count": 1, "combo": combo, "update_result": data}


def find_combo(
    message: dict[str, str],
    combo_name: str,
    legacy: ModuleType = legacy_app,
    include_deleted: bool = False,
) -> dict[str, Any] | None:
    query = normalize_food_name(combo_name)
    for combo in list_combos(message, legacy=legacy, include_deleted=include_deleted):
        name = normalize_food_name(combo.get("combo_name"))
        if query and (query == name or query in name or name in query):
            return combo
    return None


def _increment_use_count(combo: dict[str, Any], legacy: ModuleType) -> None:
    settings = legacy._settings()
    if not combo.get("record_id") or not combo_table_enabled(settings):
        return
    legacy._update_bitable_record(
        settings["bitable_app_token"],
        settings["bitable_combo_table_id"],
        str(combo["record_id"]),
        {
            _combo_field(settings, "use_count"): int(float(combo.get("use_count") or 0)) + 1,
            _combo_field(settings, "updated_at"): legacy._now_millis(),
        },
    )


def _normalize_item(item: dict[str, Any], default_meal_type: str) -> dict[str, Any]:
    grams = item.get("grams")
    try:
        grams_value = round(float(grams), 1)
    except (TypeError, ValueError):
        grams_value = None
    return {
        "food_query": str(item.get("food_query") or item.get("food_name") or item.get("name") or "").strip(),
        "grams": grams_value,
        "meal_type": str(item.get("meal_type") or default_meal_type or "unknown"),
    }


def _combo_from_record(record: dict[str, Any], settings: dict[str, str], legacy: ModuleType) -> dict[str, Any] | None:
    fields = record.get("fields")
    if not isinstance(fields, dict):
        return None
    raw_items = legacy._extract_text(fields.get(_combo_field(settings, "items_json")))
    try:
        items = json.loads(raw_items) if raw_items else []
    except json.JSONDecodeError:
        items = []
    if not isinstance(items, list):
        items = []
    return {
        "record_id": str(record.get("record_id") or ""),
        "combo_id": legacy._extract_text(fields.get(_combo_field(settings, "id"))),
        "user_id": legacy._extract_text(fields.get(_combo_field(settings, "user_id"))),
        "combo_name": legacy._extract_text(fields.get(_combo_field(settings, "name"))),
        "meal_type": legacy._extract_text(fields.get(_combo_field(settings, "meal_type"))) or "unknown",
        "items": [item for item in items if isinstance(item, dict)],
        "use_count": legacy._extract_number(fields.get(_combo_field(settings, "use_count"))) or 0,
        "confidence": legacy._extract_number(fields.get(_combo_field(settings, "confidence"))) or 0,
        "status": legacy._extract_text(fields.get(_combo_field(settings, "status"))).lower() or "active",
        "created_at": legacy._extract_number(fields.get(_combo_field(settings, "created_at"))) or 0,
        "updated_at": legacy._extract_number(fields.get(_combo_field(settings, "updated_at"))) or 0,
    }


def _combo_field_names(settings: dict[str, str], legacy: ModuleType) -> list[str]:
    return legacy._unique_non_empty([_combo_field(settings, key) for key in COMBO_FIELDS])


def _combo_field(settings: dict[str, str], key: str) -> str:
    return settings.get(f"bitable_field_combo_{key}") or COMBO_FIELDS[key]


def _active(value: Any) -> bool:
    status = str(value or "").lower()
    return not status or status in {"active", "valid"}
