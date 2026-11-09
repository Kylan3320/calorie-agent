from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import ModuleType
from typing import Any

from .. import legacy_app


METRICS = ("kcal", "carbs", "protein", "fat")


def resolve_period(
    params: dict[str, Any],
    settings: dict[str, str],
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    today = date.fromisoformat(legacy._today_string())
    period = str(params.get("period") or params.get("range") or "").strip().lower()
    if params.get("date"):
        day = _parse_date(params["date"], today)
        return _period("daily", day, day, "custom_date")
    if params.get("period_start") or params.get("start_date"):
        start = _parse_date(params.get("period_start") or params.get("start_date"), today)
        end = _parse_date(params.get("period_end") or params.get("end_date") or start.isoformat(), today)
        return _period("custom", start, end, "custom")
    if params.get("month"):
        start = _parse_month(params["month"], today)
        end = _month_end(start)
        if start.year == today.year and start.month == today.month:
            end = today
        return _period("monthly", start, end, "custom_month")

    if period in {"yesterday", "last_day"}:
        day = today - timedelta(days=1)
        return _period("daily", day, day, "yesterday")
    if period in {"this_week", "week", ""} and str(params.get("report_type") or "") == "weekly":
        start = today - timedelta(days=today.weekday())
        return _period("weekly", start, today, "this_week")
    if period == "last_week":
        this_monday = today - timedelta(days=today.weekday())
        start = this_monday - timedelta(days=7)
        return _period("weekly", start, start + timedelta(days=6), "last_week")
    if period in {"last_7_days", "recent_7_days", "7d"}:
        return _period("weekly", today - timedelta(days=6), today, "last_7_days")
    if period in {"this_month", "month", ""} and str(params.get("report_type") or "") == "monthly":
        return _period("monthly", today.replace(day=1), today, "this_month")
    if period == "last_month":
        first = today.replace(day=1)
        start = (first - timedelta(days=1)).replace(day=1)
        return _period("monthly", start, first - timedelta(days=1), "last_month")
    if period == "today" or not period:
        return _period("daily", today, today, "today")
    if period == "this_week":
        start = today - timedelta(days=today.weekday())
        return _period("weekly", start, today, "this_week")
    if period == "this_month":
        return _period("monthly", today.replace(day=1), today, "this_month")
    return _period("daily", today, today, "today")


def build_trend_facts(
    message: dict[str, str],
    period: dict[str, Any],
    settings: dict[str, str],
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    items = load_intake_items(message, period["start_date"], period["end_date"], settings, legacy)
    days = _date_list(period["start_date"], period["end_date"])
    by_date = {day.isoformat(): _empty_day(day) for day in days}
    for item in items:
        day_key = item["date"]
        row = by_date.setdefault(day_key, _empty_day(date.fromisoformat(day_key)))
        row["has_records"] = True
        row["items"].append(item)
        for metric in METRICS:
            row["totals"][metric] = _round(row["totals"][metric] + float(item.get(metric) or 0))

    daily = [by_date[day.isoformat()] for day in days]
    recorded_days = [row for row in daily if row["has_records"]]
    totals = {metric: _round(sum(float(row["totals"][metric]) for row in recorded_days)) for metric in METRICS}
    averages_recorded = {
        metric: _round(totals[metric] / len(recorded_days)) if recorded_days else None
        for metric in METRICS
    }
    averages_natural = {
        metric: _round(totals[metric] / len(daily)) if daily else None
        for metric in METRICS
    }
    return {
        "period": _period_payload(period),
        "days": daily,
        "recorded_day_count": len(recorded_days),
        "natural_day_count": len(daily),
        "totals": totals,
        "averages_recorded_days": averages_recorded,
        "averages_natural_days": averages_natural,
        "max_days": _extreme_days(recorded_days, reverse=True),
        "min_days": _extreme_days(recorded_days, reverse=False),
        "volatility": _volatility(recorded_days),
        "top_foods": _top_foods(items),
    }


def load_intake_items(
    message: dict[str, str],
    start_date: date,
    end_date: date,
    settings: dict[str, str],
    legacy: ModuleType = legacy_app,
) -> list[dict[str, Any]]:
    app_token = settings["bitable_app_token"]
    table_id = settings["bitable_intake_table_id"]
    if not app_token or not table_id:
        raise RuntimeError("BITABLE_APP_TOKEN and BITABLE_INTAKE_TABLE_ID are not configured.")

    fields = [
        legacy._intake_field(settings, "date"),
        legacy._intake_field(settings, "user_id"),
        legacy._intake_field(settings, "status"),
        legacy._intake_field(settings, "meal_type"),
        legacy._intake_field(settings, "food_name"),
        legacy._intake_field(settings, "grams"),
        legacy._intake_field(settings, "carbs"),
        legacy._intake_field(settings, "protein"),
        legacy._intake_field(settings, "fat"),
        legacy._intake_field(settings, "kcal"),
        legacy._intake_field(settings, "message_id"),
        legacy._intake_field(settings, "created_at"),
    ]
    records = legacy._list_bitable_records(app_token, table_id, field_names=legacy._unique_non_empty(fields))
    user_id = str(message.get("user_id") or "").strip()
    items: list[dict[str, Any]] = []
    for record in records:
        raw = record.get("fields")
        if not isinstance(raw, dict):
            continue
        row_user = legacy._extract_text(raw.get(legacy._intake_field(settings, "user_id")))
        if user_id and row_user and row_user != user_id:
            continue
        if not legacy._is_valid_intake_status(raw.get(legacy._intake_field(settings, "status"))):
            continue
        row_date = _date_from_value(raw.get(legacy._intake_field(settings, "date")), settings, legacy)
        if row_date is None or row_date < start_date or row_date > end_date:
            continue
        items.append(
            {
                "record_id": str(record.get("record_id") or ""),
                "date": row_date.isoformat(),
                "message_id": legacy._extract_text(raw.get(legacy._intake_field(settings, "message_id"))),
                "meal_type": legacy._extract_text(raw.get(legacy._intake_field(settings, "meal_type"))),
                "food_name": legacy._extract_text(raw.get(legacy._intake_field(settings, "food_name"))),
                "grams": legacy._extract_number(raw.get(legacy._intake_field(settings, "grams"))) or 0.0,
                "kcal": legacy._extract_number(raw.get(legacy._intake_field(settings, "kcal"))) or 0.0,
                "carbs": legacy._extract_number(raw.get(legacy._intake_field(settings, "carbs"))) or 0.0,
                "protein": legacy._extract_number(raw.get(legacy._intake_field(settings, "protein"))) or 0.0,
                "fat": legacy._extract_number(raw.get(legacy._intake_field(settings, "fat"))) or 0.0,
                "created_at": legacy._extract_number(raw.get(legacy._intake_field(settings, "created_at"))) or 0.0,
            }
        )
    return sorted(items, key=lambda item: (item["date"], item["created_at"], item["record_id"]))


def compare_trend_facts(current: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    current_avg = current.get("averages_recorded_days") if isinstance(current.get("averages_recorded_days"), dict) else {}
    previous_avg = previous.get("averages_recorded_days") if isinstance(previous.get("averages_recorded_days"), dict) else {}
    deltas = {}
    for metric in METRICS:
        left = current_avg.get(metric)
        right = previous_avg.get(metric)
        deltas[metric] = None if left is None or right is None else _round(float(left) - float(right))
    return {"current": current, "previous": previous, "average_deltas": deltas}


def _period(report_type: str, start: date, end: date, key: str) -> dict[str, Any]:
    if end < start:
        start, end = end, start
    return {"report_type": report_type, "period_key": key, "start_date": start, "end_date": end}


def _period_payload(period: dict[str, Any]) -> dict[str, Any]:
    return {
        "report_type": period["report_type"],
        "period_key": period["period_key"],
        "start_date": period["start_date"].isoformat(),
        "end_date": period["end_date"].isoformat(),
        "days": (period["end_date"] - period["start_date"]).days + 1,
    }


def _parse_date(value: Any, today: date) -> date:
    text = str(value or "").strip().lower()
    if text in {"today", ""}:
        return today
    if text == "yesterday":
        return today - timedelta(days=1)
    return date.fromisoformat(text[:10])


def _parse_month(value: Any, today: date) -> date:
    text = str(value or "").strip()
    if not text:
        return today.replace(day=1)
    if len(text) == 7:
        return date.fromisoformat(f"{text}-01")
    return date.fromisoformat(text[:10]).replace(day=1)


def _month_end(start: date) -> date:
    if start.month == 12:
        return date(start.year, 12, 31)
    return date(start.year, start.month + 1, 1) - timedelta(days=1)


def _date_from_value(value: Any, settings: dict[str, str], legacy: ModuleType) -> date | None:
    text = legacy._extract_text(value)
    if text[:10].count("-") == 2:
        try:
            return date.fromisoformat(text[:10])
        except ValueError:
            return None
    number = legacy._extract_number(value)
    if number is None:
        return None
    offset = int(float(settings.get("report_timezone_offset_hours") or settings.get("timezone_offset_hours") or 8) * 3600)
    return datetime.fromtimestamp((float(number) / 1000) + offset, tz=timezone.utc).date()


def _date_list(start: date, end: date) -> list[date]:
    return [start + timedelta(days=index) for index in range((end - start).days + 1)]


def _empty_day(day: date) -> dict[str, Any]:
    return {"date": day.isoformat(), "has_records": False, "items": [], "totals": {metric: 0.0 for metric in METRICS}}


def _extreme_days(days: list[dict[str, Any]], reverse: bool) -> dict[str, Any]:
    result = {}
    for metric in METRICS:
        ordered = sorted(days, key=lambda row: float(row["totals"][metric]), reverse=reverse)
        result[metric] = {"date": ordered[0]["date"], "value": ordered[0]["totals"][metric]} if ordered else None
    return result


def _volatility(days: list[dict[str, Any]]) -> dict[str, Any]:
    values = {}
    for metric in METRICS:
        nums = [float(row["totals"][metric]) for row in days]
        values[metric] = _round(max(nums) - min(nums)) if nums else None
    return values


def _top_foods(items: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    totals: dict[str, dict[str, Any]] = {}
    for item in items:
        name = str(item.get("food_name") or "")
        if not name:
            continue
        row = totals.setdefault(name, {"food_name": name, "grams": 0.0, "kcal": 0.0, "count": 0})
        row["grams"] = _round(row["grams"] + float(item.get("grams") or 0))
        row["kcal"] = _round(row["kcal"] + float(item.get("kcal") or 0))
        row["count"] += 1
    return sorted(totals.values(), key=lambda row: (-float(row["kcal"]), row["food_name"]))[:limit]


def _round(value: float) -> float:
    return round(float(value) + 1e-9, 1)
