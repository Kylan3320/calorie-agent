from __future__ import annotations

import re
from types import ModuleType
from typing import Any

from .. import legacy_app


DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def clear_daily_intake(legacy: ModuleType = legacy_app) -> dict[str, Any]:
    settings = legacy._settings()
    app_token = settings["bitable_app_token"]
    table_id = settings["bitable_intake_table_id"]
    if not app_token or not table_id:
        raise RuntimeError("BITABLE_APP_TOKEN and BITABLE_INTAKE_TABLE_ID are not configured.")

    date_field = legacy._intake_field(settings, "date")
    field_names = legacy._unique_non_empty([date_field])
    records = list(legacy._list_bitable_records(app_token, table_id, field_names=field_names))

    candidates = [
        record
        for record in records
        if _should_delete_record(record, settings, date_field, legacy)
    ]
    results = [
        legacy._delete_bitable_record(app_token, table_id, str(record.get("record_id") or ""))
        for record in candidates
        if record.get("record_id")
    ]
    return {
        "status": "completed",
        "scanned_count": len(records),
        "candidate_count": len(candidates),
        "deleted_count": len(results),
        "today": legacy._today_string(),
        "today_start_millis": legacy._today_start_millis(),
        "delete_results": results,
    }


def _should_delete_record(
    record: dict[str, Any],
    settings: dict[str, str],
    date_field: str,
    legacy: ModuleType,
) -> bool:
    fields = record.get("fields")
    if not isinstance(fields, dict):
        return False
    value = fields.get(date_field)
    if settings.get("bitable_field_intake_date_value_type") == "text":
        date_text = legacy._extract_text(value)
        return bool(DATE_RE.fullmatch(date_text)) and date_text < legacy._today_string()

    number = legacy._extract_number(value)
    return number is not None and int(number) < legacy._today_start_millis()
