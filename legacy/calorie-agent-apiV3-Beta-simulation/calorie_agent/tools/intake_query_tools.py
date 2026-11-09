from __future__ import annotations

import re
from datetime import date as Date
from datetime import datetime, timedelta, timezone
from types import ModuleType
from typing import Any

from .. import legacy_app
from ..domain.food_match import normalize_food_name


METRICS = ("grams", "kcal", "carbs", "protein", "fat")
VALID_INTAKE_STATUSES = {"valid", "active", "normal", "启用", "正常", "有效", "已记录"}
GENERIC_FRUIT_QUERIES = {"水果", "fruit", "fruits"}
COMMON_FRUITS = {
    "苹果",
    "香蕉",
    "梨",
    "橙",
    "橘",
    "葡萄",
    "西瓜",
    "草莓",
    "蓝莓",
    "桃",
    "猕猴桃",
    "哈密瓜",
    "菠萝",
    "芒果",
}


def query_intake_food_nutrition(
    records: list[dict[str, Any]],
    *,
    user_id: str,
    food_query: str,
    date_scope: str = "today",
    date: str | None = None,
    meal_type: str | None = None,
    record_group_id: str | None = None,
    limit: int = 20,
    current_date: str | Date | None = None,
    field_map: dict[str, str] | None = None,
    timezone_offset_hours: float = 8,
) -> dict[str, Any]:
    """Read stored nutrition facts for a recorded intake food.

    The function is intentionally pure and read-only: callers provide intake
    records, and all nutrition numbers in the result come from those records.
    """

    resolved = _resolve_query_date(date_scope, date, current_date)
    query = {
        "date_scope": date_scope or "today",
        "date": resolved.isoformat() if isinstance(resolved, Date) else date,
        "food_query": str(food_query or "").strip(),
        "meal_type": meal_type or None,
    }
    if record_group_id:
        query["record_group_id"] = record_group_id

    if not user_id:
        return _result("error", query, [], None, "no_match", [], "user_id_required")
    if not query["food_query"]:
        return _result("error", query, [], None, "no_match", [], "food_query_required")
    if not isinstance(resolved, Date):
        return _result("error", query, [], None, "no_match", [], "invalid_date")

    normalized_rows = [
        row
        for row in (
            _normalize_intake_record(record, field_map, timezone_offset_hours)
            for record in records
        )
        if row is not None
    ]
    filtered = [
        row
        for row in normalized_rows
        if row["user_id"] == user_id
        and row["date"] == resolved.isoformat()
        and row["status"] == "valid"
        and (not meal_type or row["meal_type"] == meal_type)
        and (not record_group_id or row["record_group_id"] == record_group_id)
    ]
    matches = _match_records(filtered, query["food_query"])
    if not matches:
        return _result("no_match", query, [], None, "no_match", [], "no matching intake record")

    distinct_foods = _distinct_food_names(matches)
    if len(distinct_foods) > 1:
        return _result(
            "multiple_candidates",
            query,
            [],
            None,
            "multiple_foods",
            _candidate_summaries(matches, limit),
            "multiple matching foods; choose one food name",
        )

    selected = matches[: _safe_limit(limit)]
    matched_records = [_output_record(row) for row in selected]
    ambiguity = "multiple_records" if len(selected) > 1 else "none"
    return _result(
        "success",
        query,
        matched_records,
        _totals(selected),
        ambiguity,
        [],
        _success_message(query["food_query"], matched_records, ambiguity),
    )


def query_intake_food_nutrition_from_legacy(
    message: dict[str, str],
    params: dict[str, Any],
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    """Read Bitable intake rows through the legacy read API without executing V1."""

    actor_user_id = str(message.get("user_id") or "").strip()
    requested_user_id = str(params.get("user_id") or actor_user_id).strip()
    if requested_user_id and actor_user_id and requested_user_id != actor_user_id:
        query = {
            "date_scope": str(params.get("date_scope") or "today"),
            "date": str(params.get("date") or ""),
            "food_query": str(params.get("food_query") or ""),
            "meal_type": params.get("meal_type") or None,
        }
        return _result("error", query, [], None, "no_match", [], "cross_user_query_denied")

    settings = legacy._settings()
    app_token = settings["bitable_app_token"]
    table_id = settings["bitable_intake_table_id"]
    if not app_token or not table_id:
        query = {
            "date_scope": str(params.get("date_scope") or "today"),
            "date": str(params.get("date") or ""),
            "food_query": str(params.get("food_query") or ""),
            "meal_type": params.get("meal_type") or None,
        }
        return _result("error", query, [], None, "no_match", [], "intake_table_not_configured")

    field_map = _legacy_intake_field_map(settings, legacy)
    records = legacy._list_bitable_records(
        app_token,
        table_id,
        field_names=legacy._unique_non_empty(list(field_map.values())),
    )
    return query_intake_food_nutrition(
        records,
        user_id=actor_user_id,
        date_scope=str(params.get("date_scope") or "today"),
        date=str(params.get("date") or "") or None,
        food_query=str(params.get("food_query") or params.get("food_name") or ""),
        meal_type=str(params.get("meal_type") or "") or None,
        record_group_id=str(params.get("record_group_id") or params.get("record_group") or "") or None,
        limit=_safe_limit(params.get("limit") or 20),
        current_date=legacy._today_string(),
        field_map=field_map,
        timezone_offset_hours=float(settings.get("timezone_offset_hours") or 8),
    )


def format_intake_food_nutrition_reply(result: dict[str, Any]) -> str:
    status = str(result.get("status") or "")
    query = result.get("query") if isinstance(result.get("query"), dict) else {}
    food_query = str(query.get("food_query") or "该食物")
    if status == "no_match":
        return f"没有找到{food_query}的有效摄入记录。"
    if status == "multiple_candidates":
        candidates = result.get("candidates") if isinstance(result.get("candidates"), list) else []
        names = [str(item.get("food_name") or "") for item in candidates if isinstance(item, dict)]
        suffix = "、".join(name for name in names if name)
        if suffix:
            return f"找到多个可能的食物：{suffix}。请指定要查询哪一个。"
        return "找到多个可能的食物，请指定要查询哪一个。"
    if status != "success":
        return "暂时无法查询这条摄入记录。"

    records = result.get("matched_records") if isinstance(result.get("matched_records"), list) else []
    total = result.get("total") if isinstance(result.get("total"), dict) else {}
    food_name = str(records[0].get("food_name") or food_query) if records and isinstance(records[0], dict) else food_query
    prefix = f"{food_name}"
    if len(records) > 1:
        prefix = f"{food_name}共{len(records)}条记录"
    grams = _format_number(total.get("grams"))
    kcal = _format_number(total.get("kcal"))
    carbs = _format_number(total.get("carbs"))
    protein = _format_number(total.get("protein"))
    fat = _format_number(total.get("fat"))
    return f"{prefix}：{grams}g，{kcal} kcal，碳水 {carbs}g，蛋白质 {protein}g，脂肪 {fat}g。"


def _normalize_intake_record(
    record: dict[str, Any],
    field_map: dict[str, str] | None,
    timezone_offset_hours: float,
) -> dict[str, Any] | None:
    fields = record.get("fields") if isinstance(record.get("fields"), dict) else record
    if not isinstance(fields, dict):
        return None
    row_date = _date_from_value(_value(fields, field_map, "date", ["date"]), timezone_offset_hours)
    if row_date is None:
        return None
    user_id = _text(_value(fields, field_map, "user_id", ["user_id"]))
    food_name = _text(_value(fields, field_map, "food_name", ["food_name", "food_name_snapshot", "matched_name", "name"]))
    status = _text(_value(fields, field_map, "status", ["status"])).lower()
    if status not in VALID_INTAKE_STATUSES:
        return None
    return {
        "record_id": str(record.get("record_id") or _text(fields.get("record_id"))),
        "message_id": _text(_value(fields, field_map, "message_id", ["message_id"])),
        "record_group_id": _text(_value(fields, field_map, "record_group_id", ["record_group_id", "record_group", "group_id"])),
        "date": row_date.isoformat(),
        "user_id": user_id,
        "meal_type": _text(_value(fields, field_map, "meal_type", ["meal_type"])) or "unknown",
        "food_name": food_name,
        "aliases": _aliases(fields),
        "grams": _number(_value(fields, field_map, "grams", ["grams"])) or 0.0,
        "kcal": _number(_value(fields, field_map, "kcal", ["kcal"])) or 0.0,
        "carbs": _number(_value(fields, field_map, "carbs", ["carbs"])) or 0.0,
        "protein": _number(_value(fields, field_map, "protein", ["protein"])) or 0.0,
        "fat": _number(_value(fields, field_map, "fat", ["fat"])) or 0.0,
        "status": "valid",
    }


def _match_records(records: list[dict[str, Any]], food_query: str) -> list[dict[str, Any]]:
    query = normalize_food_name(food_query)
    if not query:
        return []
    if query in GENERIC_FRUIT_QUERIES:
        return [record for record in records if _looks_like_fruit(record["food_name"])]

    exact = [record for record in records if query in _record_keys(record)]
    if exact:
        return exact
    if len(query) < 2:
        return []
    return [
        record
        for record in records
        if any(query in key or key in query for key in _record_keys(record) if key)
    ]


def _record_keys(record: dict[str, Any]) -> set[str]:
    keys = {normalize_food_name(record.get("food_name"))}
    keys.update(normalize_food_name(alias) for alias in record.get("aliases", []))
    return {key for key in keys if key}


def _looks_like_fruit(food_name: str) -> bool:
    normalized = normalize_food_name(food_name)
    if normalized in {normalize_food_name(name) for name in COMMON_FRUITS}:
        return True
    return any(token in normalized for token in ("果", "瓜", "莓", "橙", "橘", "梨", "桃"))


def _distinct_food_names(records: list[dict[str, Any]]) -> list[str]:
    names: dict[str, str] = {}
    for record in records:
        normalized = normalize_food_name(record.get("food_name"))
        if normalized and normalized not in names:
            names[normalized] = str(record.get("food_name") or "")
    return list(names.values())


def _candidate_summaries(records: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        name = str(record.get("food_name") or "")
        grouped.setdefault(name, []).append(record)
    candidates = [
        {
            "food_name": name,
            "record_count": len(rows),
            "record_ids": [str(row.get("record_id") or "") for row in rows if row.get("record_id")],
        }
        for name, rows in grouped.items()
    ]
    return sorted(candidates, key=lambda item: item["food_name"])[: _safe_limit(limit)]


def _totals(records: list[dict[str, Any]]) -> dict[str, float]:
    return {metric: _round(sum(float(record.get(metric) or 0) for record in records)) for metric in METRICS}


def _output_record(record: dict[str, Any]) -> dict[str, Any]:
    return {
        "record_id": record["record_id"],
        "message_id": record["message_id"],
        "record_group_id": record["record_group_id"],
        "date": record["date"],
        "meal_type": record["meal_type"],
        "food_name": record["food_name"],
        "grams": record["grams"],
        "kcal": record["kcal"],
        "carbs": record["carbs"],
        "protein": record["protein"],
        "fat": record["fat"],
        "status": "valid",
    }


def _result(
    status: str,
    query: dict[str, Any],
    matched_records: list[dict[str, Any]],
    total: dict[str, float] | None,
    ambiguity: str,
    candidates: list[dict[str, Any]],
    message: str,
) -> dict[str, Any]:
    return {
        "status": status,
        "query": query,
        "matched_records": matched_records,
        "total": total,
        "ambiguity": ambiguity,
        "candidates": candidates,
        "message": message,
    }


def _success_message(food_query: str, records: list[dict[str, Any]], ambiguity: str) -> str:
    if ambiguity == "multiple_records":
        return f"matched {len(records)} stored intake records for {food_query}"
    return f"matched stored intake record for {food_query}"


def _resolve_query_date(date_scope: str, explicit_date: str | None, current_date: str | Date | None) -> Date | None:
    today = _parse_date(current_date) if current_date is not None else Date.today()
    scope = str(date_scope or "today").strip().lower()
    if scope == "today":
        return today
    if scope == "yesterday":
        return today - timedelta(days=1)
    if scope == "date":
        return _parse_date(explicit_date)
    return _parse_date(explicit_date) if explicit_date else today


def _parse_date(value: Any) -> Date | None:
    if isinstance(value, Date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return Date.fromisoformat(text[:10])
    except ValueError:
        return None


def _date_from_value(value: Any, timezone_offset_hours: float) -> Date | None:
    parsed = _parse_date(value)
    if parsed is not None:
        return parsed
    number = _number(value)
    if number is None:
        return None
    return datetime.fromtimestamp((float(number) / 1000) + timezone_offset_hours * 3600, tz=timezone.utc).date()


def _value(fields: dict[str, Any], field_map: dict[str, str] | None, key: str, fallbacks: list[str]) -> Any:
    mapped = field_map.get(key) if field_map else None
    for name in [mapped, *fallbacks]:
        if name and name in fields:
            return fields[name]
    return None


def _legacy_intake_field_map(settings: dict[str, str], legacy: ModuleType) -> dict[str, str]:
    keys = [
        "date",
        "user_id",
        "message_id",
        "meal_type",
        "food_name",
        "grams",
        "carbs",
        "protein",
        "fat",
        "kcal",
        "status",
        "created_at",
    ]
    return {key: legacy._intake_field(settings, key) for key in keys if legacy._intake_field(settings, key)}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        return " ".join(part for part in (_text(item) for item in value) if part).strip()
    if isinstance(value, dict):
        for key in ("text", "name", "value", "link"):
            if key in value:
                text = _text(value[key])
                if text:
                    return text
    return str(value).strip()


def _number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return _round(float(value))
    if isinstance(value, str):
        try:
            return _round(float(value.strip()))
        except ValueError:
            return None
    if isinstance(value, list) and value:
        return _number(value[0])
    if isinstance(value, dict):
        for key in ("value", "text", "number"):
            if key in value:
                number = _number(value[key])
                if number is not None:
                    return number
    return None


def _aliases(fields: dict[str, Any]) -> list[str]:
    raw = fields.get("aliases") if "aliases" in fields else fields.get("alias")
    if isinstance(raw, list):
        return [_text(item) for item in raw if _text(item)]
    return [item.strip() for item in re.split(r"[,，、;；/|\n]+", _text(raw)) if item.strip()]


def _safe_limit(value: int) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError):
        limit = 20
    return min(max(limit, 1), 100)


def _round(value: float) -> float:
    return round(float(value) + 1e-9, 1)


def _format_number(value: Any) -> str:
    number = float(value or 0)
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"
