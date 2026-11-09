from __future__ import annotations

from types import ModuleType
from typing import Any

from .. import legacy_app
from .memory import personal_memory_enabled


PREFERENCE_FIELDS = {
    "id": "preference_id",
    "user_id": "user_id",
    "key": "key",
    "value": "value",
    "confidence": "confidence",
    "status": "status",
    "created_at": "created_at",
    "updated_at": "updated_at",
}


def preference_table_enabled(settings: dict[str, str]) -> bool:
    return personal_memory_enabled(settings) and bool(settings.get("bitable_preference_table_id"))


def list_preferences(message: dict[str, str], legacy: ModuleType = legacy_app, include_deleted: bool = False) -> list[dict[str, Any]]:
    settings = legacy._settings()
    if not preference_table_enabled(settings):
        return []
    records = legacy._list_bitable_records(
        settings["bitable_app_token"],
        settings["bitable_preference_table_id"],
        field_names=_preference_field_names(settings, legacy),
    )
    user_id = str(message.get("user_id") or "").strip()
    rows = [_preference_from_record(record, settings, legacy) for record in records]
    rows = [row for row in rows if row]
    filtered = []
    for row in rows:
        if user_id and row.get("user_id") != user_id:
            continue
        if not include_deleted and not _active(row.get("status")):
            continue
        filtered.append(row)
    return sorted(filtered, key=lambda row: str(row.get("key") or ""))


def get_preference_map(message: dict[str, str], legacy: ModuleType = legacy_app) -> dict[str, str]:
    return {str(row.get("key") or ""): str(row.get("value") or "") for row in list_preferences(message, legacy=legacy) if row.get("key")}


def update_preference(
    message: dict[str, str],
    key: str,
    value: Any,
    confidence: float = 1.0,
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    settings = legacy._settings()
    if not preference_table_enabled(settings):
        return {"status": "disabled", "reason": "preference_table_not_configured"}
    user_id = str(message.get("user_id") or "").strip()
    key = str(key or "").strip()
    if not user_id or not key:
        return {"status": "invalid", "reason": "user_id_and_key_required"}

    now = legacy._now_millis()
    existing = _find_preference(message, key, legacy)
    fields = {
        _preference_field(settings, "user_id"): user_id,
        _preference_field(settings, "key"): key,
        _preference_field(settings, "value"): str(value),
        _preference_field(settings, "confidence"): float(confidence),
        _preference_field(settings, "status"): "active",
        _preference_field(settings, "updated_at"): now,
    }
    if existing:
        data = legacy._update_bitable_record(
            settings["bitable_app_token"],
            settings["bitable_preference_table_id"],
            str(existing["record_id"]),
            fields,
        )
        return {"status": "updated", "preference": {**existing, "value": str(value)}, "update_result": data}

    preference_id = legacy._stable_uuid4_from_text(f"calorie-agent:preference:{user_id}:{key}")
    fields[_preference_field(settings, "id")] = preference_id
    fields[_preference_field(settings, "created_at")] = now
    data = legacy._batch_create_bitable_records(
        settings["bitable_app_token"],
        settings["bitable_preference_table_id"],
        [{"fields": fields}],
        client_token=legacy._stable_uuid4_from_text(f"calorie-agent:preference-create:{preference_id}"),
    )
    return {"status": "created", "preference_id": preference_id, "create_result": data}


def delete_preferences(message: dict[str, str], target: dict[str, Any], legacy: ModuleType = legacy_app) -> dict[str, Any]:
    settings = legacy._settings()
    if not preference_table_enabled(settings):
        return {"status": "disabled", "deleted_count": 0, "reason": "preference_table_not_configured"}
    key = str(target.get("key") or "").strip()
    rows = list_preferences(message, legacy=legacy)
    matches = [row for row in rows if target.get("all") is True or (key and row.get("key") == key)]
    updates = [
        legacy._update_bitable_record(
            settings["bitable_app_token"],
            settings["bitable_preference_table_id"],
            str(row["record_id"]),
            {
                _preference_field(settings, "status"): "deleted",
                _preference_field(settings, "updated_at"): legacy._now_millis(),
            },
        )
        for row in matches
        if row.get("record_id")
    ]
    return {"status": "deleted" if updates else "not_found", "deleted_count": len(updates), "items": matches}


def _find_preference(message: dict[str, str], key: str, legacy: ModuleType) -> dict[str, Any] | None:
    for row in list_preferences(message, legacy=legacy, include_deleted=True):
        if row.get("key") == key:
            return row
    return None


def _preference_from_record(record: dict[str, Any], settings: dict[str, str], legacy: ModuleType) -> dict[str, Any] | None:
    fields = record.get("fields")
    if not isinstance(fields, dict):
        return None
    return {
        "record_id": str(record.get("record_id") or ""),
        "preference_id": legacy._extract_text(fields.get(_preference_field(settings, "id"))),
        "user_id": legacy._extract_text(fields.get(_preference_field(settings, "user_id"))),
        "key": legacy._extract_text(fields.get(_preference_field(settings, "key"))),
        "value": legacy._extract_text(fields.get(_preference_field(settings, "value"))),
        "confidence": legacy._extract_number(fields.get(_preference_field(settings, "confidence"))) or 0,
        "status": legacy._extract_text(fields.get(_preference_field(settings, "status"))).lower() or "active",
        "created_at": legacy._extract_number(fields.get(_preference_field(settings, "created_at"))) or 0,
        "updated_at": legacy._extract_number(fields.get(_preference_field(settings, "updated_at"))) or 0,
    }


def _preference_field_names(settings: dict[str, str], legacy: ModuleType) -> list[str]:
    return legacy._unique_non_empty([_preference_field(settings, key) for key in PREFERENCE_FIELDS])


def _preference_field(settings: dict[str, str], key: str) -> str:
    return settings.get(f"bitable_field_preference_{key}") or PREFERENCE_FIELDS[key]


def _active(value: Any) -> bool:
    status = str(value or "").lower()
    return not status or status in {"active", "valid"}
