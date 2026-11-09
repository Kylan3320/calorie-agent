from __future__ import annotations

import json
from types import ModuleType
from typing import Any

from .. import legacy_app


REMINDER_TYPES = {"meal_missing", "target_gap", "anomaly", "pending", "daily_summary", "inactivity"}

RULE_FIELDS = {
    "id": "reminder_rule_id",
    "user_id": "user_id",
    "type": "reminder_type",
    "enabled": "enabled",
    "schedule_time": "schedule_time",
    "meal_type": "meal_type",
    "threshold_json": "threshold_json",
    "quiet_hours_start": "quiet_hours_start",
    "quiet_hours_end": "quiet_hours_end",
    "max_per_day": "max_per_day",
    "status": "status",
    "chat_id": "chat_id",
    "allow_group": "allow_group",
    "paused_until": "paused_until",
    "created_at": "created_at",
    "updated_at": "updated_at",
}


def table_enabled(settings: dict[str, str]) -> bool:
    return bool(settings.get("bitable_app_token") and settings.get("bitable_reminder_rule_table_id"))


def list_rules(
    message: dict[str, str] | None = None,
    settings: dict[str, str] | None = None,
    legacy: ModuleType = legacy_app,
    include_disabled: bool = True,
) -> list[dict[str, Any]]:
    settings = settings or legacy._settings()
    if not table_enabled(settings):
        return []
    records = legacy._list_bitable_records(
        settings["bitable_app_token"],
        settings["bitable_reminder_rule_table_id"],
        field_names=field_names(settings, legacy),
    )
    user_id = str((message or {}).get("user_id") or "").strip()
    rows = [_rule_from_record(record, settings, legacy) for record in records]
    result = []
    for row in rows:
        if not row:
            continue
        if user_id and row.get("user_id") and row.get("user_id") != user_id:
            continue
        if not include_disabled and not is_rule_active(row, settings, legacy._now_millis()):
            continue
        result.append(row)
    return sorted(result, key=lambda item: (str(item.get("reminder_type") or ""), str(item.get("meal_type") or "")))


def update_rule(
    message: dict[str, str],
    params: dict[str, Any],
    settings: dict[str, str] | None = None,
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    settings = settings or legacy._settings()
    if not table_enabled(settings):
        return {"status": "disabled", "reason": "reminder_rule_table_not_configured"}

    reminder_type = normalize_type(params.get("reminder_type") or params.get("type") or params.get("kind"))
    if not reminder_type:
        reminder_type = "daily_summary"
    user_id = str(params.get("user_id") or message.get("user_id") or "").strip()
    if not user_id:
        return {"status": "invalid", "reason": "user_id_required"}

    existing = _find_rule(message, reminder_type, str(params.get("meal_type") or ""), settings, legacy)
    now = legacy._now_millis()
    enabled = params.get("enabled")
    if enabled is None:
        enabled = _truthy(settings.get("reminder_default_enabled"))
    fields = {
        field(settings, "user_id"): user_id,
        field(settings, "type"): reminder_type,
        field(settings, "enabled"): bool(_truthy(enabled)),
        field(settings, "schedule_time"): _schedule_time(params, settings, reminder_type),
        field(settings, "meal_type"): str(params.get("meal_type") or ""),
        field(settings, "threshold_json"): _threshold_json(params),
        field(settings, "quiet_hours_start"): str(params.get("quiet_hours_start") or settings.get("reminder_quiet_hours_start") or ""),
        field(settings, "quiet_hours_end"): str(params.get("quiet_hours_end") or settings.get("reminder_quiet_hours_end") or ""),
        field(settings, "max_per_day"): _int(params.get("max_per_day"), _int(settings.get("reminder_max_per_day"), 2)),
        field(settings, "status"): str(params.get("status") or "active"),
        field(settings, "chat_id"): str(params.get("chat_id") or message.get("chat_id") or ""),
        field(settings, "allow_group"): bool(_truthy(params.get("allow_group"))),
        field(settings, "paused_until"): _float(params.get("paused_until"), 0),
        field(settings, "updated_at"): now,
    }
    if existing and existing.get("record_id"):
        data = legacy._update_bitable_record(
            settings["bitable_app_token"],
            settings["bitable_reminder_rule_table_id"],
            str(existing["record_id"]),
            fields,
        )
        return {"status": "updated", "rule": {**existing, **_fields_to_rule(fields, settings, legacy)}, "update_result": data}

    rule_id = str(params.get("reminder_rule_id") or _rule_id(user_id, reminder_type, str(params.get("meal_type") or ""), legacy))
    fields[field(settings, "id")] = rule_id
    fields[field(settings, "created_at")] = now
    data = legacy._batch_create_bitable_records(
        settings["bitable_app_token"],
        settings["bitable_reminder_rule_table_id"],
        [{"fields": fields}],
        client_token=legacy._stable_uuid4_from_text(f"calorie-agent:reminder-rule:{rule_id}"),
    )
    return {"status": "created", "rule_id": rule_id, "rule": _fields_to_rule(fields, settings, legacy), "create_result": data}


def pause_reminders(message: dict[str, str], params: dict[str, Any], legacy: ModuleType = legacy_app) -> dict[str, Any]:
    settings = legacy._settings()
    if not table_enabled(settings):
        return {"status": "disabled", "reason": "reminder_rule_table_not_configured"}
    hours = _float(params.get("hours"), 0)
    days = _float(params.get("days"), 0)
    if hours <= 0 and days <= 0:
        days = 7 if _truthy(params.get("this_week")) else 1
    paused_until = legacy._now_millis() + int((hours + days * 24) * 3600 * 1000)
    updates = _update_matching_rules(message, {"paused_until": paused_until}, legacy)
    return {"status": "paused" if updates else "not_found", "paused_until": paused_until, "updated_count": len(updates), "updates": updates}


def resume_reminders(message: dict[str, str], legacy: ModuleType = legacy_app) -> dict[str, Any]:
    updates = _update_matching_rules(message, {"paused_until": 0, "enabled": True}, legacy)
    return {"status": "resumed" if updates else "not_found", "updated_count": len(updates), "updates": updates}


def snooze_reminder(message: dict[str, str], params: dict[str, Any], legacy: ModuleType = legacy_app) -> dict[str, Any]:
    hours = _float(params.get("hours"), 2) or 2
    updates = _update_matching_rules(message, {"paused_until": legacy._now_millis() + int(hours * 3600 * 1000)}, legacy)
    return {"status": "snoozed" if updates else "not_found", "hours": hours, "updated_count": len(updates), "updates": updates}


def is_rule_active(rule: dict[str, Any], settings: dict[str, str], now_millis: int) -> bool:
    if not _truthy(settings.get("reminder_enabled", "true")):
        return False
    if not _truthy(rule.get("enabled")):
        return False
    if str(rule.get("status") or "active").lower() not in {"", "active", "valid"}:
        return False
    paused_until = _float(rule.get("paused_until"), 0)
    return paused_until <= 0 or paused_until <= now_millis


def normalize_type(value: Any) -> str:
    text = str(value or "").strip()
    aliases = {
        "summary": "daily_summary",
        "daily": "daily_summary",
        "protein": "target_gap",
        "target": "target_gap",
        "meal": "meal_missing",
    }
    text = aliases.get(text, text)
    return text if text in REMINDER_TYPES else ""


def field_names(settings: dict[str, str], legacy: ModuleType) -> list[str]:
    return legacy._unique_non_empty([field(settings, key) for key in RULE_FIELDS])


def field(settings: dict[str, str], key: str) -> str:
    return settings.get(f"bitable_field_reminder_rule_{key}") or RULE_FIELDS[key]


def _find_rule(
    message: dict[str, str],
    reminder_type: str,
    meal_type: str,
    settings: dict[str, str],
    legacy: ModuleType,
) -> dict[str, Any] | None:
    meal_type = str(meal_type or "")
    for rule in list_rules(message, settings=settings, legacy=legacy):
        if rule.get("reminder_type") != reminder_type:
            continue
        if meal_type and str(rule.get("meal_type") or "") != meal_type:
            continue
        if not meal_type or not rule.get("meal_type"):
            return rule
    return None


def _update_matching_rules(message: dict[str, str], params: dict[str, Any], legacy: ModuleType) -> list[dict[str, Any]]:
    settings = legacy._settings()
    rows = list_rules(message, settings=settings, legacy=legacy)
    updates = []
    for row in rows:
        fields: dict[str, Any] = {field(settings, "updated_at"): legacy._now_millis()}
        if "paused_until" in params:
            fields[field(settings, "paused_until")] = _float(params["paused_until"], 0)
        if "enabled" in params:
            fields[field(settings, "enabled")] = bool(_truthy(params["enabled"]))
        data = legacy._update_bitable_record(
            settings["bitable_app_token"],
            settings["bitable_reminder_rule_table_id"],
            str(row.get("record_id") or ""),
            fields,
        )
        updates.append({"rule": row, "update_result": data})
    return updates


def _rule_from_record(record: dict[str, Any], settings: dict[str, str], legacy: ModuleType) -> dict[str, Any] | None:
    fields = record.get("fields")
    if not isinstance(fields, dict):
        return None
    return {
        "record_id": str(record.get("record_id") or ""),
        "reminder_rule_id": legacy._extract_text(fields.get(field(settings, "id"))),
        "user_id": legacy._extract_text(fields.get(field(settings, "user_id"))),
        "reminder_type": normalize_type(legacy._extract_text(fields.get(field(settings, "type")))),
        "enabled": _truthy(fields.get(field(settings, "enabled"))),
        "schedule_time": legacy._extract_text(fields.get(field(settings, "schedule_time"))),
        "meal_type": legacy._extract_text(fields.get(field(settings, "meal_type"))),
        "thresholds": _parse_json(legacy._extract_text(fields.get(field(settings, "threshold_json")))),
        "quiet_hours_start": legacy._extract_text(fields.get(field(settings, "quiet_hours_start"))),
        "quiet_hours_end": legacy._extract_text(fields.get(field(settings, "quiet_hours_end"))),
        "max_per_day": legacy._extract_number(fields.get(field(settings, "max_per_day"))) or 0,
        "status": legacy._extract_text(fields.get(field(settings, "status"))).lower() or "active",
        "chat_id": legacy._extract_text(fields.get(field(settings, "chat_id"))),
        "allow_group": _truthy(fields.get(field(settings, "allow_group"))),
        "paused_until": legacy._extract_number(fields.get(field(settings, "paused_until"))) or 0,
        "created_at": legacy._extract_number(fields.get(field(settings, "created_at"))) or 0,
        "updated_at": legacy._extract_number(fields.get(field(settings, "updated_at"))) or 0,
    }


def _fields_to_rule(fields: dict[str, Any], settings: dict[str, str], legacy: ModuleType) -> dict[str, Any]:
    return _rule_from_record({"record_id": "", "fields": fields}, settings, legacy) or {}


def _rule_id(user_id: str, reminder_type: str, meal_type: str, legacy: ModuleType) -> str:
    return legacy._stable_uuid4_from_text(f"calorie-agent:reminder-rule:{user_id}:{reminder_type}:{meal_type}")


def _schedule_time(params: dict[str, Any], settings: dict[str, str], reminder_type: str) -> str:
    if params.get("schedule_time"):
        return str(params["schedule_time"])
    if reminder_type == "daily_summary":
        return str(settings.get("reminder_daily_summary_time") or "21:30")
    return str(params.get("time") or "")


def _threshold_json(params: dict[str, Any]) -> str:
    threshold = params.get("thresholds") if isinstance(params.get("thresholds"), dict) else {}
    if params.get("threshold_json"):
        return str(params["threshold_json"])
    for key in ("protein_min_grams", "kcal_min_remaining", "kcal_max_ratio"):
        if key in params:
            threshold[key] = params[key]
    return json.dumps(threshold, ensure_ascii=False, sort_keys=True) if threshold else ""


def _parse_json(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value) if value else {}
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "启用", "开启", "是"}


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
