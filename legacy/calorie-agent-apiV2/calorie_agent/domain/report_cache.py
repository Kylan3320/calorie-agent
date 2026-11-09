from __future__ import annotations

import json
from types import ModuleType
from typing import Any

from .. import legacy_app


REPORT_FIELDS = {
    "id": "report_id",
    "user_id": "user_id",
    "type": "report_type",
    "period_start": "period_start",
    "period_end": "period_end",
    "summary_text": "summary_text",
    "facts_json": "facts_json",
    "status": "status",
    "generated_at": "generated_at",
    "source": "source",
    "version": "version",
}


def cache_enabled(settings: dict[str, str]) -> bool:
    return _truthy(settings.get("report_cache_enabled", "true")) and bool(
        settings.get("bitable_app_token") and settings.get("bitable_report_table_id")
    )


def get_cached_report(
    message: dict[str, str],
    report_type: str,
    period: dict[str, Any],
    settings: dict[str, str],
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    if not cache_enabled(settings):
        return {"status": "disabled", "reason": "report_table_not_configured"}
    rows = _list_reports(message, settings, legacy)
    start = _date_text(period.get("start_date") or period.get("start"))
    end = _date_text(period.get("end_date") or period.get("end"))
    matches = [
        row
        for row in rows
        if row.get("report_type") == report_type
        and row.get("period_start") == start
        and row.get("period_end") == end
        and _active(row.get("status"))
    ]
    if not matches:
        return {"status": "miss"}
    best = sorted(matches, key=lambda row: float(row.get("generated_at") or 0), reverse=True)[0]
    return {"status": "hit", "report": best}


def get_report(
    message: dict[str, str],
    target: dict[str, Any],
    settings: dict[str, str],
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    if not cache_enabled(settings):
        return {"status": "disabled", "reason": "report_table_not_configured"}
    report_id = str(target.get("report_id") or "").strip()
    rows = _list_reports(message, settings, legacy)
    for row in rows:
        if report_id and row.get("report_id") == report_id and _active(row.get("status")):
            return {"status": "hit", "report": row}
    return {"status": "not_found"}


def save_report(
    message: dict[str, str],
    report: dict[str, Any],
    settings: dict[str, str],
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    if not cache_enabled(settings):
        return {"status": "skipped", "reason": "report_table_not_configured"}
    period = report.get("period") if isinstance(report.get("period"), dict) else {}
    report_type = str(report.get("report_type") or period.get("report_type") or "")
    start = _date_text(period.get("start_date"))
    end = _date_text(period.get("end_date"))
    if not report_type or not start or not end:
        return {"status": "invalid", "reason": "report_type_and_period_required"}

    user_id = str(message.get("user_id") or "")
    report_id = str(report.get("report_id") or _report_id(user_id, report_type, start, end, legacy))
    now = legacy._now_millis()
    fields = {
        _report_field(settings, "id"): report_id,
        _report_field(settings, "user_id"): user_id,
        _report_field(settings, "type"): report_type,
        _report_field(settings, "period_start"): start,
        _report_field(settings, "period_end"): end,
        _report_field(settings, "summary_text"): str(report.get("summary_text") or ""),
        _report_field(settings, "facts_json"): json.dumps(report.get("facts") or {}, ensure_ascii=False, sort_keys=True),
        _report_field(settings, "status"): "valid",
        _report_field(settings, "generated_at"): now,
        _report_field(settings, "source"): str(report.get("source") or "agent_runtime"),
        _report_field(settings, "version"): str(report.get("version") or ""),
    }
    existing = get_cached_report(message, report_type, {"start_date": start, "end_date": end}, settings, legacy)
    if existing.get("status") == "hit":
        record = existing.get("report") if isinstance(existing.get("report"), dict) else {}
        record_id = str(record.get("record_id") or "")
        update = dict(fields)
        update.pop(_report_field(settings, "id"), None)
        data = legacy._update_bitable_record(settings["bitable_app_token"], settings["bitable_report_table_id"], record_id, update)
        return {"status": "updated", "record_id": record_id, "report_id": report_id, "update_result": data}

    data = legacy._batch_create_bitable_records(
        settings["bitable_app_token"],
        settings["bitable_report_table_id"],
        [{"fields": fields}],
        client_token=legacy._stable_uuid4_from_text(f"calorie-agent:report:{report_id}"),
    )
    records = (data.get("data") or {}).get("records") if isinstance(data.get("data"), dict) else []
    record_id = str(records[0].get("record_id") or "") if records and isinstance(records[0], dict) else ""
    return {"status": "created", "record_id": record_id, "report_id": report_id, "create_result": data}


def _list_reports(message: dict[str, str], settings: dict[str, str], legacy: ModuleType) -> list[dict[str, Any]]:
    records = legacy._list_bitable_records(
        settings["bitable_app_token"],
        settings["bitable_report_table_id"],
        field_names=legacy._unique_non_empty([_report_field(settings, key) for key in REPORT_FIELDS]),
    )
    user_id = str(message.get("user_id") or "").strip()
    rows = [_report_from_record(record, settings, legacy) for record in records]
    result = []
    for row in rows:
        if not row:
            continue
        if user_id and row.get("user_id") and row.get("user_id") != user_id:
            continue
        result.append(row)
    return result


def _report_from_record(record: dict[str, Any], settings: dict[str, str], legacy: ModuleType) -> dict[str, Any] | None:
    fields = record.get("fields")
    if not isinstance(fields, dict):
        return None
    facts_json = legacy._extract_text(fields.get(_report_field(settings, "facts_json")))
    try:
        facts = json.loads(facts_json) if facts_json else {}
    except json.JSONDecodeError:
        facts = {}
    return {
        "record_id": str(record.get("record_id") or ""),
        "report_id": legacy._extract_text(fields.get(_report_field(settings, "id"))),
        "user_id": legacy._extract_text(fields.get(_report_field(settings, "user_id"))),
        "report_type": legacy._extract_text(fields.get(_report_field(settings, "type"))),
        "period_start": legacy._extract_text(fields.get(_report_field(settings, "period_start"))),
        "period_end": legacy._extract_text(fields.get(_report_field(settings, "period_end"))),
        "summary_text": legacy._extract_text(fields.get(_report_field(settings, "summary_text"))),
        "facts": facts if isinstance(facts, dict) else {},
        "status": legacy._extract_text(fields.get(_report_field(settings, "status"))).lower() or "valid",
        "generated_at": legacy._extract_number(fields.get(_report_field(settings, "generated_at"))) or 0,
        "source": legacy._extract_text(fields.get(_report_field(settings, "source"))),
        "version": legacy._extract_text(fields.get(_report_field(settings, "version"))),
    }


def _report_field(settings: dict[str, str], key: str) -> str:
    return settings.get(f"bitable_field_report_{key}") or REPORT_FIELDS[key]


def _report_id(user_id: str, report_type: str, start: str, end: str, legacy: ModuleType) -> str:
    return legacy._stable_uuid4_from_text(f"calorie-agent:report:{user_id}:{report_type}:{start}:{end}")


def _date_text(value: Any) -> str:
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value or "")[:10]


def _active(value: Any) -> bool:
    return str(value or "").lower() in {"", "valid", "active"}


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
