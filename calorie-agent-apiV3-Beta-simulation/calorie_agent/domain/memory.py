from __future__ import annotations

import statistics
from types import ModuleType
from typing import Any

from .. import legacy_app
from .food_match import normalize_food_name


MEMORY_FIELDS = {
    "id": "memory_id",
    "user_id": "user_id",
    "food_id": "food_id",
    "food_name": "food_name",
    "alias": "alias",
    "default_grams": "default_grams",
    "meal_type": "meal_type",
    "sample_count": "sample_count",
    "confidence": "confidence",
    "source": "source",
    "source_message_id": "source_message_id",
    "status": "status",
    "created_at": "created_at",
    "updated_at": "updated_at",
}


def personal_memory_enabled(settings: dict[str, str]) -> bool:
    return _truthy(settings.get("personal_memory_enabled", "false")) and bool(settings.get("bitable_app_token"))


def memory_table_enabled(settings: dict[str, str]) -> bool:
    return personal_memory_enabled(settings) and bool(settings.get("bitable_memory_table_id"))


def min_sample_count(settings: dict[str, str]) -> int:
    return max(1, _int(settings.get("memory_min_sample_count"), 3))


def confidence_threshold(settings: dict[str, str]) -> float:
    return max(0.0, min(1.0, _float(settings.get("memory_confidence_threshold"), 0.85)))


def auto_default_grams_enabled(settings: dict[str, str]) -> bool:
    return personal_memory_enabled(settings) and _truthy(settings.get("auto_default_grams_enabled", "true"))


def auto_use_default_grams(settings: dict[str, str]) -> bool:
    return auto_default_grams_enabled(settings) and _truthy(settings.get("auto_use_default_grams", "false"))


def list_user_memories(
    message: dict[str, str],
    settings: dict[str, str] | None = None,
    source: str = "",
    include_deleted: bool = False,
    legacy: ModuleType = legacy_app,
) -> list[dict[str, Any]]:
    settings = settings or legacy._settings()
    if not memory_table_enabled(settings):
        return []

    records = legacy._list_bitable_records(
        settings["bitable_app_token"],
        settings["bitable_memory_table_id"],
        field_names=_memory_field_names(settings, legacy),
    )
    user_id = str(message.get("user_id") or "").strip()
    rows = [_memory_from_record(record, settings, legacy) for record in records]
    rows = [row for row in rows if row]
    filtered = []
    for row in rows:
        if user_id and row.get("user_id") != user_id:
            continue
        if source and row.get("source") != source:
            continue
        if not include_deleted and not _is_active(row.get("status")):
            continue
        filtered.append(row)
    return sorted(filtered, key=lambda row: (-float(row.get("confidence") or 0), -float(row.get("updated_at") or 0)))


def upsert_memory(
    message: dict[str, str],
    payload: dict[str, Any],
    settings: dict[str, str] | None = None,
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    settings = settings or legacy._settings()
    if not memory_table_enabled(settings):
        return {"status": "disabled", "reason": "memory_table_not_configured"}

    user_id = str(message.get("user_id") or "").strip()
    source = str(payload.get("source") or "").strip()
    if not user_id or not source:
        return {"status": "invalid", "reason": "user_id_and_source_required"}

    now = legacy._now_millis()
    memory_id = str(payload.get("memory_id") or _memory_id(message, payload, legacy))
    fields = {
        _memory_field(settings, "id"): memory_id,
        _memory_field(settings, "user_id"): user_id,
        _memory_field(settings, "food_id"): str(payload.get("food_id") or ""),
        _memory_field(settings, "food_name"): str(payload.get("food_name") or ""),
        _memory_field(settings, "alias"): str(payload.get("alias") or ""),
        _memory_field(settings, "meal_type"): str(payload.get("meal_type") or "unknown"),
        _memory_field(settings, "sample_count"): _int(payload.get("sample_count"), 0),
        _memory_field(settings, "confidence"): _float(payload.get("confidence"), 0.0),
        _memory_field(settings, "source"): source,
        _memory_field(settings, "source_message_id"): str(payload.get("source_message_id") or message.get("message_id") or ""),
        _memory_field(settings, "status"): str(payload.get("status") or "active"),
        _memory_field(settings, "updated_at"): now,
    }
    if payload.get("default_grams") is not None:
        fields[_memory_field(settings, "default_grams")] = _round(payload["default_grams"])

    existing = _find_duplicate_memory(message, payload, settings, legacy)
    if existing and existing.get("record_id"):
        update = dict(fields)
        update.pop(_memory_field(settings, "id"), None)
        data = legacy._update_bitable_record(
            settings["bitable_app_token"],
            settings["bitable_memory_table_id"],
            str(existing["record_id"]),
            update,
        )
        return {"status": "updated", "record_id": existing["record_id"], "memory": {**existing, **payload}, "update_result": data}

    fields[_memory_field(settings, "created_at")] = now
    data = legacy._batch_create_bitable_records(
        settings["bitable_app_token"],
        settings["bitable_memory_table_id"],
        [{"fields": fields}],
        client_token=legacy._stable_uuid4_from_text(f"calorie-agent:memory:{memory_id}"),
    )
    records = (data.get("data") or {}).get("records") if isinstance(data.get("data"), dict) else []
    record_id = str(records[0].get("record_id") or "") if records and isinstance(records[0], dict) else ""
    return {"status": "created", "record_id": record_id, "memory_id": memory_id, "create_result": data}


def soft_delete_memories(
    message: dict[str, str],
    target: dict[str, Any],
    settings: dict[str, str] | None = None,
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    settings = settings or legacy._settings()
    if not memory_table_enabled(settings):
        return {"status": "disabled", "deleted_count": 0, "reason": "memory_table_not_configured"}

    source = str(target.get("source") or target.get("memory_type") or "").strip()
    alias = normalize_food_name(target.get("alias") or target.get("name") or "")
    food_name = normalize_food_name(target.get("food_name") or target.get("food_query") or target.get("name") or "")
    rows = list_user_memories(message, settings=settings, source=source, legacy=legacy) if source else list_user_memories(message, settings=settings, legacy=legacy)
    matches = []
    for row in rows:
        if alias and normalize_food_name(row.get("alias")) == alias:
            matches.append(row)
            continue
        if food_name and normalize_food_name(row.get("food_name")) == food_name:
            matches.append(row)
            continue
        if target.get("all") is True:
            matches.append(row)

    status_field = _memory_field(settings, "status")
    updated_field = _memory_field(settings, "updated_at")
    updates = [
        legacy._update_bitable_record(
            settings["bitable_app_token"],
            settings["bitable_memory_table_id"],
            str(row["record_id"]),
            {status_field: "deleted", updated_field: legacy._now_millis()},
        )
        for row in matches
        if row.get("record_id")
    ]
    return {"status": "deleted" if updates else "not_found", "deleted_count": len(updates), "items": matches, "update_results": updates}


def suggest_default_grams(
    message: dict[str, str],
    food: dict[str, Any],
    settings: dict[str, str] | None = None,
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    settings = settings or legacy._settings()
    if not auto_default_grams_enabled(settings):
        return {"status": "disabled"}

    samples = _history_grams(message, food, settings, legacy)
    needed = min_sample_count(settings)
    if len(samples) < needed:
        return {"status": "insufficient_samples", "sample_count": len(samples), "min_sample_count": needed}

    grams = _round(statistics.median(samples))
    payload = {
        "source": "default_grams",
        "food_id": food.get("food_id", ""),
        "food_name": food.get("name", ""),
        "default_grams": grams,
        "sample_count": len(samples),
        "confidence": min(0.99, 0.5 + len(samples) / 20),
        "status": "active",
    }
    write = upsert_memory(message, payload, settings=settings, legacy=legacy)
    return {
        "status": "suggested",
        "food_name": food.get("name", ""),
        "food_id": food.get("food_id", ""),
        "default_grams": grams,
        "sample_count": len(samples),
        "min_sample_count": needed,
        "memory_write_result": write,
        "auto_use": auto_use_default_grams(settings),
    }


def _history_grams(
    message: dict[str, str],
    food: dict[str, Any],
    settings: dict[str, str],
    legacy: ModuleType,
) -> list[float]:
    app_token = settings.get("bitable_app_token") or ""
    table_id = settings.get("bitable_intake_table_id") or ""
    if not app_token or not table_id:
        return []

    field_names = legacy._unique_non_empty(
        [
            legacy._intake_field(settings, "user_id"),
            legacy._intake_field(settings, "food_id"),
            legacy._intake_field(settings, "food_name"),
            legacy._intake_field(settings, "grams"),
            legacy._intake_field(settings, "status"),
        ]
    )
    records = legacy._list_bitable_records(app_token, table_id, field_names=field_names)
    user_id = str(message.get("user_id") or "").strip()
    food_id = str(food.get("food_id") or "")
    food_name = normalize_food_name(food.get("name"))
    grams_values: list[float] = []
    for record in records:
        fields = record.get("fields")
        if not isinstance(fields, dict):
            continue
        row_user = legacy._extract_text(fields.get(legacy._intake_field(settings, "user_id")))
        if user_id and row_user and row_user != user_id:
            continue
        if not legacy._is_valid_intake_status(fields.get(legacy._intake_field(settings, "status"))):
            continue
        row_food_id = legacy._extract_text(fields.get(legacy._intake_field(settings, "food_id")))
        row_food_name = normalize_food_name(legacy._extract_text(fields.get(legacy._intake_field(settings, "food_name"))))
        if food_id:
            if row_food_id and row_food_id != food_id:
                continue
            if not row_food_id and row_food_name != food_name:
                continue
        elif row_food_name != food_name:
            continue
        grams = legacy._extract_number(fields.get(legacy._intake_field(settings, "grams")))
        if grams is not None and grams > 0:
            grams_values.append(float(grams))
    return grams_values


def _find_duplicate_memory(
    message: dict[str, str],
    payload: dict[str, Any],
    settings: dict[str, str],
    legacy: ModuleType,
) -> dict[str, Any] | None:
    source = str(payload.get("source") or "")
    rows = list_user_memories(message, settings=settings, source=source, include_deleted=True, legacy=legacy)
    alias = normalize_food_name(payload.get("alias"))
    food_id = str(payload.get("food_id") or "")
    food_name = normalize_food_name(payload.get("food_name"))
    meal_type = str(payload.get("meal_type") or "unknown")
    for row in rows:
        if alias and normalize_food_name(row.get("alias")) == alias and _same_food(row, food_id, food_name):
            return row
        if source == "default_grams" and _same_food(row, food_id, food_name) and str(row.get("meal_type") or "unknown") == meal_type:
            return row
    return None


def _same_food(row: dict[str, Any], food_id: str, food_name: str) -> bool:
    if food_id and str(row.get("food_id") or "") == food_id:
        return True
    return bool(food_name and normalize_food_name(row.get("food_name")) == food_name)


def _memory_from_record(record: dict[str, Any], settings: dict[str, str], legacy: ModuleType) -> dict[str, Any] | None:
    fields = record.get("fields")
    if not isinstance(fields, dict):
        return None
    return {
        "record_id": str(record.get("record_id") or ""),
        "memory_id": legacy._extract_text(fields.get(_memory_field(settings, "id"))),
        "user_id": legacy._extract_text(fields.get(_memory_field(settings, "user_id"))),
        "food_id": legacy._extract_text(fields.get(_memory_field(settings, "food_id"))),
        "food_name": legacy._extract_text(fields.get(_memory_field(settings, "food_name"))),
        "alias": legacy._extract_text(fields.get(_memory_field(settings, "alias"))),
        "default_grams": legacy._extract_number(fields.get(_memory_field(settings, "default_grams"))),
        "meal_type": legacy._extract_text(fields.get(_memory_field(settings, "meal_type"))) or "unknown",
        "sample_count": legacy._extract_number(fields.get(_memory_field(settings, "sample_count"))) or 0,
        "confidence": legacy._extract_number(fields.get(_memory_field(settings, "confidence"))) or 0,
        "source": legacy._extract_text(fields.get(_memory_field(settings, "source"))),
        "source_message_id": legacy._extract_text(fields.get(_memory_field(settings, "source_message_id"))),
        "status": legacy._extract_text(fields.get(_memory_field(settings, "status"))).lower() or "active",
        "created_at": legacy._extract_number(fields.get(_memory_field(settings, "created_at"))) or 0,
        "updated_at": legacy._extract_number(fields.get(_memory_field(settings, "updated_at"))) or 0,
    }


def _memory_field_names(settings: dict[str, str], legacy: ModuleType) -> list[str]:
    return legacy._unique_non_empty([_memory_field(settings, key) for key in MEMORY_FIELDS])


def _memory_field(settings: dict[str, str], key: str) -> str:
    return settings.get(f"bitable_field_memory_{key}") or MEMORY_FIELDS[key]


def _memory_id(message: dict[str, str], payload: dict[str, Any], legacy: ModuleType) -> str:
    source = str(payload.get("source") or "")
    key = "|".join(
        [
            str(message.get("user_id") or ""),
            source,
            normalize_food_name(payload.get("alias")),
            str(payload.get("food_id") or ""),
            normalize_food_name(payload.get("food_name")),
            str(payload.get("meal_type") or ""),
        ]
    )
    return legacy._stable_uuid4_from_text(f"calorie-agent:memory:{key}")


def _is_active(value: Any) -> bool:
    status = str(value or "").lower()
    return not status or status in {"active", "valid", "candidate", "confirmed"}


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int(value: Any, default: int) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _round(value: Any) -> float:
    return round(float(value) + 1e-9, 1)
