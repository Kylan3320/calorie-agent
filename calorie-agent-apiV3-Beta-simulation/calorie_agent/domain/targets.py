from __future__ import annotations

from datetime import date, datetime, timezone
from types import ModuleType
from typing import Any

from .. import legacy_app
from .trends import METRICS


def build_target_facts(
    message: dict[str, str],
    trend_facts: dict[str, Any],
    settings: dict[str, str],
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    daily_rows = trend_facts.get("days") if isinstance(trend_facts.get("days"), list) else []
    days = []
    for row in daily_rows:
        if not isinstance(row, dict):
            continue
        day = date.fromisoformat(str(row.get("date")))
        standard = load_standard_for_date(message, day, settings, legacy)
        days.append(_target_day(row, standard, settings))

    recorded = [row for row in days if row.get("has_records")]
    hit_counts = {
        metric: sum(1 for row in recorded if row.get("hit", {}).get(metric) is True)
        for metric in METRICS
    }
    hit_rates = {
        metric: _rate(hit_counts[metric], len(recorded)) if recorded else None
        for metric in METRICS
    }
    average_gaps = {
        metric: _round(sum(float(row.get("gap", {}).get(metric) or 0) for row in recorded) / len(recorded))
        if recorded
        else None
        for metric in METRICS
    }
    return {
        "thresholds": {
            "kcal_min_ratio": _float(settings.get("report_kcal_hit_min_ratio"), 0.9),
            "kcal_max_ratio": _float(settings.get("report_kcal_hit_max_ratio"), 1.1),
            "protein_min_ratio": _float(settings.get("report_protein_hit_min_ratio"), 0.9),
            "macro_min_ratio": _float(settings.get("report_macro_hit_min_ratio"), 0.8),
            "macro_max_ratio": _float(settings.get("report_macro_hit_max_ratio"), 1.2),
        },
        "recorded_day_count": len(recorded),
        "days": days,
        "hit_counts": hit_counts,
        "hit_rates": hit_rates,
        "average_gaps": average_gaps,
        "kcal_over_days": [row["date"] for row in recorded if row.get("status", {}).get("kcal") == "over"],
        "kcal_under_days": [row["date"] for row in recorded if row.get("status", {}).get("kcal") == "under"],
        "protein_under_days": [row["date"] for row in recorded if row.get("status", {}).get("protein") == "under"],
        "source": "daily_standard",
    }


def load_standard_for_date(
    message: dict[str, str],
    day: date,
    settings: dict[str, str],
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    app_token = settings.get("bitable_app_token", "")
    table_id = settings.get("bitable_standard_table_id", "")
    if not table_id:
        return legacy._default_daily_standard()
    if not app_token:
        raise RuntimeError("BITABLE_APP_TOKEN is not configured.")

    user_id_field = legacy._standard_field(settings, "user_id")
    type_field = legacy._standard_field(settings, "type")
    date_field = legacy._standard_field(settings, "date")
    fields = legacy._unique_non_empty(
        [
            user_id_field,
            type_field,
            date_field,
            legacy._standard_field(settings, "kcal"),
            legacy._standard_field(settings, "carbs"),
            legacy._standard_field(settings, "protein"),
            legacy._standard_field(settings, "fat"),
        ]
    )
    records = legacy._list_bitable_records(app_token, table_id, field_names=fields)
    user_id = str(message.get("user_id") or "").strip()
    best_score = -1
    best_fields: dict[str, Any] | None = None

    for record in records:
        raw = record.get("fields")
        if not isinstance(raw, dict):
            continue
        row_user = legacy._extract_text(raw.get(user_id_field))
        if user_id and row_user and row_user != user_id:
            continue

        date_score = _standard_date_score(raw.get(date_field), day, settings, legacy)
        if date_score < 0:
            continue

        standard_type = legacy._extract_text(raw.get(type_field)).lower()
        score = date_score
        if user_id and row_user == user_id:
            score += 100
        elif not row_user:
            score += 10
        if standard_type in {"daily", "default", "每天", "每日", "每日标准"}:
            score += 5
        if score > best_score:
            best_score = score
            best_fields = raw

    if best_fields is None:
        return legacy._default_daily_standard()
    return legacy._daily_standard_from_fields(best_fields, settings)


def _target_day(row: dict[str, Any], standard: dict[str, Any], settings: dict[str, str]) -> dict[str, Any]:
    has_records = bool(row.get("has_records"))
    totals = row.get("totals") if isinstance(row.get("totals"), dict) else {}
    gap = {metric: _round(float(totals.get(metric) or 0) - float(standard.get(metric) or 0)) for metric in METRICS}
    remaining = {metric: _round(max(0.0, -gap[metric])) for metric in METRICS}
    if not has_records:
        return {
            "date": str(row.get("date") or ""),
            "has_records": False,
            "standard": {metric: standard.get(metric) for metric in METRICS},
            "gap": {metric: None for metric in METRICS},
            "remaining": {metric: None for metric in METRICS},
            "hit": {metric: None for metric in METRICS},
            "status": {metric: "no_record" for metric in METRICS},
        }

    hit = {
        "kcal": _between(totals.get("kcal"), standard.get("kcal"), _float(settings.get("report_kcal_hit_min_ratio"), 0.9), _float(settings.get("report_kcal_hit_max_ratio"), 1.1)),
        "carbs": _between(totals.get("carbs"), standard.get("carbs"), _float(settings.get("report_macro_hit_min_ratio"), 0.8), _float(settings.get("report_macro_hit_max_ratio"), 1.2)),
        "protein": _at_least(totals.get("protein"), standard.get("protein"), _float(settings.get("report_protein_hit_min_ratio"), 0.9)),
        "fat": _between(totals.get("fat"), standard.get("fat"), _float(settings.get("report_macro_hit_min_ratio"), 0.8), _float(settings.get("report_macro_hit_max_ratio"), 1.2)),
    }
    status = {
        "kcal": _range_status(totals.get("kcal"), standard.get("kcal"), _float(settings.get("report_kcal_hit_min_ratio"), 0.9), _float(settings.get("report_kcal_hit_max_ratio"), 1.1)),
        "carbs": _range_status(totals.get("carbs"), standard.get("carbs"), _float(settings.get("report_macro_hit_min_ratio"), 0.8), _float(settings.get("report_macro_hit_max_ratio"), 1.2)),
        "protein": "hit" if hit["protein"] else "under",
        "fat": _range_status(totals.get("fat"), standard.get("fat"), _float(settings.get("report_macro_hit_min_ratio"), 0.8), _float(settings.get("report_macro_hit_max_ratio"), 1.2)),
    }
    return {
        "date": str(row.get("date") or ""),
        "has_records": True,
        "standard": {metric: _round(float(standard.get(metric) or 0)) for metric in METRICS},
        "gap": gap,
        "remaining": remaining,
        "hit": hit,
        "status": status,
    }


def _standard_date_score(value: Any, day: date, settings: dict[str, str], legacy: ModuleType) -> int:
    text = legacy._extract_text(value)
    if not text:
        return 0
    if text in {"today", "今天", "今日"}:
        return 20 if day.isoformat() == legacy._today_string() else -1
    if text[:10].count("-") == 2:
        return 30 if text[:10] == day.isoformat() else -1
    number = legacy._extract_number(value)
    if number is None:
        return -1
    offset = int(float(settings.get("report_timezone_offset_hours") or settings.get("timezone_offset_hours") or 8) * 3600)
    parsed = datetime.fromtimestamp((float(number) / 1000) + offset, tz=timezone.utc).date()
    return 30 if parsed == day else -1


def _between(value: Any, target: Any, min_ratio: float, max_ratio: float) -> bool:
    actual = float(value or 0)
    goal = float(target or 0)
    if goal <= 0:
        return False
    return goal * min_ratio <= actual <= goal * max_ratio


def _at_least(value: Any, target: Any, min_ratio: float) -> bool:
    actual = float(value or 0)
    goal = float(target or 0)
    if goal <= 0:
        return False
    return actual >= goal * min_ratio


def _range_status(value: Any, target: Any, min_ratio: float, max_ratio: float) -> str:
    actual = float(value or 0)
    goal = float(target or 0)
    if goal <= 0:
        return "unknown"
    if actual < goal * min_ratio:
        return "under"
    if actual > goal * max_ratio:
        return "over"
    return "hit"


def _rate(count: int, total: int) -> float:
    return _round(count * 100 / total) if total else 0.0


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _round(value: float) -> float:
    return round(float(value) + 1e-9, 1)
