from __future__ import annotations

import json
import statistics
import time
from types import ModuleType
from typing import Any

from .. import legacy_app


ANOMALY_FIELDS = {
    "id": "anomaly_id",
    "user_id": "user_id",
    "type": "anomaly_type",
    "severity": "severity",
    "related_record_id": "related_record_id",
    "related_message_id": "related_message_id",
    "facts_json": "facts_json",
    "status": "status",
    "created_at": "created_at",
}


def detect_anomalies(
    message: dict[str, str],
    settings: dict[str, str] | None = None,
    legacy: ModuleType = legacy_app,
    now_millis: int | None = None,
) -> dict[str, Any]:
    settings = settings or legacy._settings()
    now_millis = now_millis or legacy._now_millis()
    if not _truthy(settings.get("anomaly_detection_enabled", "true")):
        return {"status": "disabled", "anomalies": [], "count": 0}

    anomalies: list[dict[str, Any]] = []
    today = legacy._load_today_intake_summary(message)
    standard = legacy._load_daily_standard(message)
    history = _load_history_items(message, settings, legacy)

    anomalies.extend(_daily_kcal_anomalies(today, standard, settings, now_millis))
    anomalies.extend(_protein_gap_anomalies(today, standard, settings, now_millis))
    anomalies.extend(_food_grams_anomalies(today, history, settings, now_millis))
    anomalies.extend(_meal_kcal_anomalies(today, history, settings, now_millis))
    anomalies.extend(_duplicate_anomalies(today, now_millis))

    write_result = write_anomalies(message, anomalies, settings, legacy) if anomalies else {"status": "skipped", "count": 0}
    return {"status": "ok", "anomalies": anomalies, "count": len(anomalies), "write_result": write_result}


def write_anomalies(
    message: dict[str, str],
    anomalies: list[dict[str, Any]],
    settings: dict[str, str],
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    if not settings.get("bitable_app_token") or not settings.get("bitable_anomaly_table_id"):
        return {"status": "skipped", "reason": "anomaly_table_not_configured", "count": len(anomalies)}
    records = []
    for item in anomalies:
        anomaly_id = str(item.get("anomaly_id") or _anomaly_id(message, item, legacy))
        item["anomaly_id"] = anomaly_id
        records.append(
            {
                "fields": {
                    field(settings, "id"): anomaly_id,
                    field(settings, "user_id"): str(message.get("user_id") or ""),
                    field(settings, "type"): str(item.get("anomaly_type") or ""),
                    field(settings, "severity"): str(item.get("severity") or "warning"),
                    field(settings, "related_record_id"): str(item.get("related_record_id") or ""),
                    field(settings, "related_message_id"): str(item.get("related_message_id") or ""),
                    field(settings, "facts_json"): json.dumps(item.get("facts") or {}, ensure_ascii=False, sort_keys=True),
                    field(settings, "status"): "active",
                    field(settings, "created_at"): legacy._now_millis(),
                }
            }
        )
    data = legacy._batch_create_bitable_records(
        settings["bitable_app_token"],
        settings["bitable_anomaly_table_id"],
        records,
        client_token=legacy._stable_uuid4_from_text(f"calorie-agent:anomaly:{message.get('user_id','')}:{legacy._now_millis()}"),
    )
    return {"status": "created", "count": len(records), "create_result": data}


def field_names(settings: dict[str, str], legacy: ModuleType) -> list[str]:
    return legacy._unique_non_empty([field(settings, key) for key in ANOMALY_FIELDS])


def field(settings: dict[str, str], key: str) -> str:
    return settings.get(f"bitable_field_anomaly_{key}") or ANOMALY_FIELDS[key]


def _daily_kcal_anomalies(
    today: dict[str, Any],
    standard: dict[str, Any],
    settings: dict[str, str],
    now_millis: int,
) -> list[dict[str, Any]]:
    total = _num(_nested(today, "totals", "kcal"))
    target = _num(standard.get("kcal"))
    ratio = _float(settings.get("anomaly_daily_kcal_max_ratio"), 1.25)
    if target <= 0 or total <= target * ratio:
        return []
    return [
        _anomaly(
            "daily_kcal_over",
            "warning",
            {"kcal": total, "target_kcal": target, "ratio": round(total / target, 2), "threshold_ratio": ratio, "now": now_millis},
        )
    ]


def _protein_gap_anomalies(
    today: dict[str, Any],
    standard: dict[str, Any],
    settings: dict[str, str],
    now_millis: int,
) -> list[dict[str, Any]]:
    current = _num(_nested(today, "totals", "protein"))
    target = _num(standard.get("protein"))
    gap = round(target - current, 1)
    threshold = _float(settings.get("target_gap_protein_min_grams"), 30)
    if target <= 0 or gap < threshold:
        return []
    return [
        _anomaly(
            "protein_gap",
            "warning",
            {"protein": current, "target_protein": target, "gap": gap, "threshold_gap": threshold, "now": now_millis},
        )
    ]


def _food_grams_anomalies(
    today: dict[str, Any],
    history: list[dict[str, Any]],
    settings: dict[str, str],
    now_millis: int,
) -> list[dict[str, Any]]:
    ratio = _float(settings.get("anomaly_food_grams_ratio"), 2.5)
    by_food: dict[str, list[float]] = {}
    for item in history:
        name = _food_key(item.get("food_name"))
        if name and _num(item.get("grams")) > 0:
            by_food.setdefault(name, []).append(_num(item.get("grams")))

    anomalies = []
    for item in today.get("items") if isinstance(today.get("items"), list) else []:
        if not isinstance(item, dict):
            continue
        grams = _num(item.get("grams"))
        name = _food_key(item.get("food_name"))
        samples = by_food.get(name, [])
        baseline = statistics.median(samples) if len(samples) >= 3 else 0
        if baseline > 0 and grams <= baseline * ratio:
            continue
        if baseline <= 0 and grams < 1000:
            continue
        anomalies.append(
            _anomaly(
                "food_grams_high",
                "critical",
                {
                    "food_name": item.get("food_name"),
                    "grams": grams,
                    "baseline_grams": round(baseline, 1) if baseline else None,
                    "threshold_ratio": ratio,
                    "now": now_millis,
                },
                item,
            )
        )
    return anomalies


def _meal_kcal_anomalies(
    today: dict[str, Any],
    history: list[dict[str, Any]],
    settings: dict[str, str],
    now_millis: int,
) -> list[dict[str, Any]]:
    samples = [_num(item.get("kcal")) for item in history if _num(item.get("kcal")) > 0]
    if len(samples) < 3:
        return []
    baseline = statistics.mean(samples)
    ratio = _float(settings.get("anomaly_meal_kcal_ratio"), 1.8)
    anomalies = []
    for item in today.get("items") if isinstance(today.get("items"), list) else []:
        if not isinstance(item, dict):
            continue
        kcal = _num(_nested(item, "nutrition", "kcal"))
        if kcal <= baseline * ratio:
            continue
        anomalies.append(
            _anomaly(
                "meal_kcal_high",
                "critical",
                {"food_name": item.get("food_name"), "kcal": kcal, "baseline_kcal": round(baseline, 1), "threshold_ratio": ratio, "now": now_millis},
                item,
            )
        )
    return anomalies


def _duplicate_anomalies(today: dict[str, Any], now_millis: int) -> list[dict[str, Any]]:
    seen: dict[tuple[str, float, str], dict[str, Any]] = {}
    anomalies = []
    for item in today.get("items") if isinstance(today.get("items"), list) else []:
        if not isinstance(item, dict):
            continue
        key = (_food_key(item.get("food_name")), round(_num(item.get("grams")), 1), str(item.get("meal_type") or ""))
        previous = seen.get(key)
        if previous:
            anomalies.append(
                _anomaly(
                    "possible_duplicate",
                    "critical",
                    {
                        "food_name": item.get("food_name"),
                        "grams": _num(item.get("grams")),
                        "previous_record_id": previous.get("record_id"),
                        "now": now_millis,
                    },
                    item,
                )
            )
        else:
            seen[key] = item
    return anomalies


def _load_history_items(message: dict[str, str], settings: dict[str, str], legacy: ModuleType) -> list[dict[str, Any]]:
    app_token = settings.get("bitable_app_token") or ""
    table_id = settings.get("bitable_intake_table_id") or ""
    if not app_token or not table_id:
        return []
    fields = legacy._unique_non_empty(
        [
            legacy._intake_field(settings, "date"),
            legacy._intake_field(settings, "user_id"),
            legacy._intake_field(settings, "status"),
            legacy._intake_field(settings, "food_name"),
            legacy._intake_field(settings, "grams"),
            legacy._intake_field(settings, "kcal"),
        ]
    )
    records = legacy._list_bitable_records(app_token, table_id, field_names=fields)
    today = legacy._today_string()
    user_id = str(message.get("user_id") or "").strip()
    result = []
    for record in records:
        raw = record.get("fields")
        if not isinstance(raw, dict):
            continue
        if user_id and legacy._extract_text(raw.get(legacy._intake_field(settings, "user_id"))) not in {"", user_id}:
            continue
        if not legacy._is_valid_intake_status(raw.get(legacy._intake_field(settings, "status"))):
            continue
        row_date = _date_text(raw.get(legacy._intake_field(settings, "date")), settings, legacy)
        if row_date == today:
            continue
        result.append(
            {
                "food_name": legacy._extract_text(raw.get(legacy._intake_field(settings, "food_name"))),
                "grams": legacy._extract_number(raw.get(legacy._intake_field(settings, "grams"))) or 0,
                "kcal": legacy._extract_number(raw.get(legacy._intake_field(settings, "kcal"))) or 0,
            }
        )
    return result


def _date_text(value: Any, settings: dict[str, str], legacy: ModuleType) -> str:
    if settings.get("bitable_field_intake_date_value_type") == "text":
        return legacy._extract_text(value)[:10]
    number = legacy._extract_number(value)
    if number is None:
        return ""
    return time.strftime("%Y-%m-%d", time.gmtime(number / 1000 + legacy._timezone_offset_seconds()))


def _anomaly(anomaly_type: str, severity: str, facts: dict[str, Any], related: dict[str, Any] | None = None) -> dict[str, Any]:
    related = related or {}
    return {
        "anomaly_type": anomaly_type,
        "severity": severity,
        "related_record_id": str(related.get("record_id") or ""),
        "related_message_id": str(related.get("message_id") or ""),
        "facts": facts,
    }


def _anomaly_id(message: dict[str, str], item: dict[str, Any], legacy: ModuleType) -> str:
    source = json.dumps(item, ensure_ascii=False, sort_keys=True)
    return legacy._stable_uuid4_from_text(f"calorie-agent:anomaly:{message.get('user_id','')}:{source}")


def _nested(payload: dict[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _food_key(value: Any) -> str:
    return str(value or "").strip().lower()


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "启用", "开启", "是"}
