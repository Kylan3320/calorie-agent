from __future__ import annotations

import json
import time
from types import ModuleType
from typing import Any

from .. import legacy_app
from . import anomaly, notification_rules


EVENT_FIELDS = {
    "id": "reminder_event_id",
    "user_id": "user_id",
    "rule_id": "rule_id",
    "type": "reminder_type",
    "trigger_reason": "trigger_reason",
    "facts_json": "facts_json",
    "message_text": "message_text",
    "status": "status",
    "sent_at": "sent_at",
    "acknowledged_at": "acknowledged_at",
    "source": "source",
    "chat_id": "chat_id",
    "error": "error",
}


def evaluate_reminders(
    legacy: ModuleType = legacy_app,
    now_millis: int | None = None,
    send: bool = True,
) -> dict[str, Any]:
    settings = legacy._settings()
    now_millis = now_millis or legacy._now_millis()
    if not _truthy(settings.get("reminder_enabled", "true")):
        return _result("disabled", [], [], "reminder_disabled")
    if not notification_rules.table_enabled(settings):
        return _result("disabled", [], [], "reminder_rule_table_not_configured")

    rules = notification_rules.list_rules(None, settings=settings, legacy=legacy, include_disabled=True)
    active_rules = [rule for rule in rules if notification_rules.is_rule_active(rule, settings, now_millis)]
    candidates: list[dict[str, Any]] = []
    for rule in active_rules:
        candidates.extend(generate_candidates_for_rule(rule, settings, legacy, now_millis))

    decisions = []
    sent = []
    for candidate in candidates:
        skip_reason = skip_reason_for_candidate(candidate, settings, legacy, now_millis)
        if skip_reason:
            event = record_event(candidate, "skipped", settings, legacy, now_millis, error=skip_reason)
            decisions.append({"status": "skipped", "reason": skip_reason, "candidate": candidate, "event": event})
            continue
        if not send:
            event = record_event(candidate, "planned", settings, legacy, now_millis)
            decisions.append({"status": "planned", "candidate": candidate, "event": event})
            continue
        delivered = send_candidate(candidate, settings, legacy, now_millis)
        decisions.append(delivered)
        if delivered.get("status") == "sent":
            sent.append(delivered)

    metrics = _metrics(settings, candidates, decisions)
    return {
        "status": "ok",
        "rules_count": len(rules),
        "active_rules_count": len(active_rules),
        "candidates": candidates,
        "decisions": decisions,
        "sent_count": len(sent),
        "metrics": metrics,
    }


def generate_candidates_for_rule(
    rule: dict[str, Any],
    settings: dict[str, str],
    legacy: ModuleType,
    now_millis: int,
) -> list[dict[str, Any]]:
    if not _schedule_due(rule.get("schedule_time"), now_millis, legacy):
        return []
    reminder_type = str(rule.get("reminder_type") or "")
    if reminder_type == "meal_missing":
        return _meal_missing_candidates(rule, settings, legacy, now_millis)
    if reminder_type == "target_gap":
        return _target_gap_candidates(rule, settings, legacy, now_millis)
    if reminder_type == "anomaly":
        return _anomaly_candidates(rule, settings, legacy, now_millis)
    if reminder_type == "pending":
        return _pending_candidates(rule, settings, legacy, now_millis)
    if reminder_type == "daily_summary":
        return _daily_summary_candidates(rule, settings, legacy, now_millis)
    if reminder_type == "inactivity":
        return _inactivity_candidates(rule, settings, legacy, now_millis)
    return []


def skip_reason_for_candidate(
    candidate: dict[str, Any],
    settings: dict[str, str],
    legacy: ModuleType,
    now_millis: int,
) -> str:
    if not event_table_enabled(settings):
        return "reminder_event_table_not_configured"
    if not candidate.get("chat_id"):
        return "no_chat_id"
    if _is_group_chat(candidate) and not _truthy(candidate.get("allow_group")):
        return "group_chat_not_authorized"
    if _in_quiet_hours(now_millis, str(candidate.get("quiet_hours_start") or settings.get("reminder_quiet_hours_start")), str(candidate.get("quiet_hours_end") or settings.get("reminder_quiet_hours_end")), legacy):
        return "quiet_hours"
    events = list_events(candidate, settings, legacy)
    today_events = [event for event in events if _same_local_day(_num(event.get("sent_at")), now_millis, legacy)]
    sent_today = [event for event in today_events if str(event.get("status") or "") == "sent"]
    max_per_day = int(_num(candidate.get("max_per_day")) or _int(settings.get("reminder_max_per_day"), 2))
    if max_per_day > 0 and len(sent_today) >= max_per_day:
        return "max_per_day_reached"
    same_type = [
        event
        for event in sent_today
        if event.get("reminder_type") == candidate.get("reminder_type")
        and event.get("trigger_reason") == candidate.get("trigger_reason")
    ]
    if same_type:
        return "duplicate_reminder_today"
    if candidate.get("reminder_type") == "pending":
        max_times = _int(settings.get("reminder_pending_max_times"), 2)
        previous = [event for event in events if event.get("trigger_reason") == candidate.get("trigger_reason") and event.get("status") == "sent"]
        if len(previous) >= max_times:
            return "pending_max_times_reached"
    return ""


def send_candidate(
    candidate: dict[str, Any],
    settings: dict[str, str],
    legacy: ModuleType,
    now_millis: int | None = None,
) -> dict[str, Any]:
    now_millis = now_millis or legacy._now_millis()
    text = str(candidate.get("message_text") or _compose_reminder_text(candidate))
    try:
        send_result = legacy._send_feishu_text_message(
            str(candidate.get("chat_id") or ""),
            text,
            legacy._stable_uuid4_from_text(f"calorie-agent:reminder-reply:{candidate.get('reminder_event_id') or candidate.get('trigger_reason')}"),
        )
    except Exception as exc:
        event = record_event(candidate, "failed", settings, legacy, now_millis, message_text=text, error=str(exc))
        return {"status": "failed", "error": str(exc), "candidate": candidate, "event": event}
    event = record_event(candidate, "sent", settings, legacy, now_millis, message_text=text)
    return {"status": "sent", "candidate": candidate, "event": event, "send_result": send_result}


def record_event(
    candidate: dict[str, Any],
    status: str,
    settings: dict[str, str],
    legacy: ModuleType,
    now_millis: int,
    message_text: str = "",
    error: str = "",
) -> dict[str, Any]:
    if not event_table_enabled(settings):
        return {"status": "skipped", "reason": "reminder_event_table_not_configured"}
    event_id = str(candidate.get("reminder_event_id") or _event_id(candidate, status, legacy))
    text = message_text or str(candidate.get("message_text") or _compose_reminder_text(candidate))
    fields = {
        event_field(settings, "id"): event_id,
        event_field(settings, "user_id"): str(candidate.get("user_id") or ""),
        event_field(settings, "rule_id"): str(candidate.get("rule_id") or ""),
        event_field(settings, "type"): str(candidate.get("reminder_type") or ""),
        event_field(settings, "trigger_reason"): str(candidate.get("trigger_reason") or ""),
        event_field(settings, "facts_json"): json.dumps(candidate.get("facts") or {}, ensure_ascii=False, sort_keys=True),
        event_field(settings, "message_text"): text,
        event_field(settings, "status"): status,
        event_field(settings, "sent_at"): now_millis,
        event_field(settings, "acknowledged_at"): 0,
        event_field(settings, "source"): str(candidate.get("source") or "timer"),
        event_field(settings, "chat_id"): str(candidate.get("chat_id") or ""),
        event_field(settings, "error"): error,
    }
    data = legacy._batch_create_bitable_records(
        settings["bitable_app_token"],
        settings["bitable_reminder_event_table_id"],
        [{"fields": fields}],
        client_token=legacy._stable_uuid4_from_text(f"calorie-agent:reminder-event:{event_id}:{status}"),
    )
    return {"status": "created", "event_id": event_id, "create_result": data}


def list_events(candidate_or_message: dict[str, Any], settings: dict[str, str], legacy: ModuleType) -> list[dict[str, Any]]:
    if not event_table_enabled(settings):
        return []
    records = legacy._list_bitable_records(
        settings["bitable_app_token"],
        settings["bitable_reminder_event_table_id"],
        field_names=event_field_names(settings, legacy),
    )
    user_id = str(candidate_or_message.get("user_id") or "").strip()
    rows = [_event_from_record(record, settings, legacy) for record in records]
    return [row for row in rows if row and (not user_id or row.get("user_id") in {"", user_id})]


def generate_daily_summary_candidate(
    message: dict[str, str],
    legacy: ModuleType = legacy_app,
    now_millis: int | None = None,
) -> dict[str, Any]:
    settings = legacy._settings()
    rule = {
        "reminder_rule_id": "",
        "user_id": message.get("user_id", ""),
        "chat_id": message.get("chat_id", ""),
        "reminder_type": "daily_summary",
        "enabled": True,
        "schedule_time": settings.get("reminder_daily_summary_time"),
        "max_per_day": settings.get("reminder_max_per_day"),
    }
    candidates = _daily_summary_candidates(rule, settings, legacy, now_millis or legacy._now_millis())
    return {"status": "generated" if candidates else "empty", "candidate": candidates[0] if candidates else {}}


def event_table_enabled(settings: dict[str, str]) -> bool:
    return bool(settings.get("bitable_app_token") and settings.get("bitable_reminder_event_table_id"))


def event_field_names(settings: dict[str, str], legacy: ModuleType) -> list[str]:
    return legacy._unique_non_empty([event_field(settings, key) for key in EVENT_FIELDS])


def event_field(settings: dict[str, str], key: str) -> str:
    return settings.get(f"bitable_field_reminder_event_{key}") or EVENT_FIELDS[key]


def _meal_missing_candidates(rule: dict[str, Any], settings: dict[str, str], legacy: ModuleType, now_millis: int) -> list[dict[str, Any]]:
    message = _message_for_rule(rule)
    summary = legacy._load_today_intake_summary(message)
    meal_type = str(rule.get("meal_type") or "unknown")
    if any(str(item.get("meal_type") or "") == meal_type for item in summary.get("items", []) if isinstance(item, dict)):
        return []
    return [_candidate(rule, "meal_missing", "info", f"meal_missing:{meal_type}", {"meal_type": meal_type, "today_count": summary.get("count", 0), "now": now_millis})]


def _target_gap_candidates(rule: dict[str, Any], settings: dict[str, str], legacy: ModuleType, now_millis: int) -> list[dict[str, Any]]:
    message = _message_for_rule(rule)
    summary = legacy._load_today_intake_summary(message)
    standard = legacy._load_daily_standard(message)
    totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
    thresholds = rule.get("thresholds") if isinstance(rule.get("thresholds"), dict) else {}
    protein = _num(totals.get("protein"))
    target_protein = _num(standard.get("protein"))
    protein_gap = round(target_protein - protein, 1)
    kcal = _num(totals.get("kcal"))
    target_kcal = _num(standard.get("kcal"))
    kcal_remaining = round(target_kcal - kcal, 1)
    protein_threshold = _float(thresholds.get("protein_min_grams"), _float(settings.get("target_gap_protein_min_grams"), 30))
    kcal_threshold = _float(thresholds.get("kcal_min_remaining"), _float(settings.get("target_gap_kcal_min_remaining"), 500))
    if protein_gap < protein_threshold and kcal_remaining < kcal_threshold:
        return []
    facts = {
        "protein": protein,
        "target_protein": target_protein,
        "protein_gap": protein_gap,
        "kcal": kcal,
        "target_kcal": target_kcal,
        "kcal_remaining": kcal_remaining,
        "now": now_millis,
    }
    return [_candidate(rule, "target_gap", "warning", "target_gap:today", facts)]


def _anomaly_candidates(rule: dict[str, Any], settings: dict[str, str], legacy: ModuleType, now_millis: int) -> list[dict[str, Any]]:
    message = _message_for_rule(rule)
    detected = anomaly.detect_anomalies(message, settings=settings, legacy=legacy, now_millis=now_millis)
    candidates = []
    for item in detected.get("anomalies", []) if isinstance(detected.get("anomalies"), list) else []:
        if not isinstance(item, dict):
            continue
        facts = {**(item.get("facts") if isinstance(item.get("facts"), dict) else {}), "anomaly_type": item.get("anomaly_type")}
        candidates.append(_candidate(rule, "anomaly", str(item.get("severity") or "warning"), f"anomaly:{item.get('anomaly_type')}:{item.get('related_record_id')}", facts))
    return candidates


def _pending_candidates(rule: dict[str, Any], settings: dict[str, str], legacy: ModuleType, now_millis: int) -> list[dict[str, Any]]:
    message = _message_for_rule(rule)
    tasks = _pending_tasks(message, settings, legacy, now_millis)
    min_age_ms = int(_float(settings.get("reminder_pending_after_hours"), 6) * 3600 * 1000)
    result = []
    for task in tasks:
        if now_millis - _num(task.get("created_at")) < min_age_ms:
            continue
        facts = {
            "pending_id": task.get("pending_id"),
            "food_name": task.get("food_name"),
            "grams": task.get("grams"),
            "intent": task.get("intent"),
            "age_hours": round((now_millis - _num(task.get("created_at"))) / 3600000, 1),
            "now": now_millis,
        }
        result.append(_candidate(rule, "pending", "info", f"pending:{task.get('pending_id')}", facts))
    return result


def _daily_summary_candidates(rule: dict[str, Any], settings: dict[str, str], legacy: ModuleType, now_millis: int) -> list[dict[str, Any]]:
    message = _message_for_rule(rule)
    summary = legacy._load_today_intake_summary(message)
    standard = legacy._load_daily_standard(message)
    totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
    protein_gap = max(0.0, round(_num(standard.get("protein")) - _num(totals.get("protein")), 1))
    facts = {
        "kcal": _num(totals.get("kcal")),
        "target_kcal": _num(standard.get("kcal")),
        "protein": _num(totals.get("protein")),
        "target_protein": _num(standard.get("protein")),
        "protein_gap": protein_gap,
        "record_count": summary.get("count", 0),
        "now": now_millis,
    }
    return [_candidate(rule, "daily_summary", "info", "daily_summary:today", facts)]


def _inactivity_candidates(rule: dict[str, Any], settings: dict[str, str], legacy: ModuleType, now_millis: int) -> list[dict[str, Any]]:
    message = _message_for_rule(rule)
    summary = legacy._load_today_intake_summary(message)
    if int(summary.get("count") or 0) > 0:
        return []
    return [_candidate(rule, "inactivity", "info", "inactivity:today", {"days": 1, "now": now_millis})]


def _pending_tasks(message: dict[str, str], settings: dict[str, str], legacy: ModuleType, now_millis: int) -> list[dict[str, Any]]:
    if not settings.get("bitable_app_token") or not settings.get("bitable_pending_table_id"):
        return []
    records = legacy._list_bitable_records(settings["bitable_app_token"], settings["bitable_pending_table_id"], field_names=legacy._pending_field_names(settings))
    user_id = str(message.get("user_id") or "").strip()
    chat_id = str(message.get("chat_id") or "").strip()
    tasks = []
    for record in records:
        task = legacy._pending_task_from_record(record, settings)
        if not task or task.get("status") != "pending":
            continue
        if user_id and task.get("user_id") not in {"", user_id}:
            continue
        if chat_id and task.get("chat_id") not in {"", chat_id}:
            continue
        if _num(task.get("expires_at")) and _num(task.get("expires_at")) < now_millis:
            legacy._mark_pending_task_status(task, "expired")
            continue
        tasks.append(task)
    return tasks


def _candidate(rule: dict[str, Any], reminder_type: str, severity: str, trigger_reason: str, facts: dict[str, Any]) -> dict[str, Any]:
    candidate = {
        "reminder_type": reminder_type,
        "severity": severity,
        "rule_id": str(rule.get("reminder_rule_id") or ""),
        "user_id": str(rule.get("user_id") or ""),
        "chat_id": str(rule.get("chat_id") or ""),
        "allow_group": bool(rule.get("allow_group")),
        "trigger_reason": trigger_reason,
        "facts": facts,
        "source": "timer",
        "quiet_hours_start": str(rule.get("quiet_hours_start") or ""),
        "quiet_hours_end": str(rule.get("quiet_hours_end") or ""),
        "max_per_day": rule.get("max_per_day"),
    }
    candidate["message_text"] = _compose_reminder_text(candidate)
    candidate["reminder_event_id"] = _event_id(candidate, "planned", legacy_app)
    return candidate


def _message_for_rule(rule: dict[str, Any]) -> dict[str, str]:
    return {
        "message_id": f"reminder:{rule.get('reminder_rule_id') or rule.get('reminder_type')}",
        "chat_id": str(rule.get("chat_id") or ""),
        "user_id": str(rule.get("user_id") or ""),
        "text": "",
    }


def _event_from_record(record: dict[str, Any], settings: dict[str, str], legacy: ModuleType) -> dict[str, Any] | None:
    fields = record.get("fields")
    if not isinstance(fields, dict):
        return None
    facts_text = legacy._extract_text(fields.get(event_field(settings, "facts_json")))
    try:
        facts = json.loads(facts_text) if facts_text else {}
    except json.JSONDecodeError:
        facts = {}
    return {
        "record_id": str(record.get("record_id") or ""),
        "reminder_event_id": legacy._extract_text(fields.get(event_field(settings, "id"))),
        "user_id": legacy._extract_text(fields.get(event_field(settings, "user_id"))),
        "rule_id": legacy._extract_text(fields.get(event_field(settings, "rule_id"))),
        "reminder_type": legacy._extract_text(fields.get(event_field(settings, "type"))),
        "trigger_reason": legacy._extract_text(fields.get(event_field(settings, "trigger_reason"))),
        "facts": facts,
        "message_text": legacy._extract_text(fields.get(event_field(settings, "message_text"))),
        "status": legacy._extract_text(fields.get(event_field(settings, "status"))).lower(),
        "sent_at": legacy._extract_number(fields.get(event_field(settings, "sent_at"))) or 0,
        "acknowledged_at": legacy._extract_number(fields.get(event_field(settings, "acknowledged_at"))) or 0,
        "source": legacy._extract_text(fields.get(event_field(settings, "source"))),
        "chat_id": legacy._extract_text(fields.get(event_field(settings, "chat_id"))),
        "error": legacy._extract_text(fields.get(event_field(settings, "error"))),
    }


def _compose_reminder_text(candidate: dict[str, Any]) -> str:
    from calorie_agent.agent.reminder_prompts import compose_reminder_text

    return compose_reminder_text(candidate)


def _metrics(settings: dict[str, str], candidates: list[dict[str, Any]], decisions: list[dict[str, Any]]) -> dict[str, Any]:
    sent = [item for item in decisions if item.get("status") == "sent"]
    skipped = [item for item in decisions if item.get("status") == "skipped"]
    failed = [item for item in decisions if item.get("status") == "failed"]
    return {
        "reminder_enabled": _truthy(settings.get("reminder_enabled")),
        "reminder_candidates_count": len(candidates),
        "reminder_sent_count": len(sent),
        "reminder_skipped_count": len(skipped),
        "reminder_skip_reason": skipped[0].get("reason") if skipped else None,
        "anomaly_detected_count": len([item for item in candidates if item.get("reminder_type") == "anomaly"]),
        "pending_reminder_count": len([item for item in candidates if item.get("reminder_type") == "pending"]),
        "target_gap_reminder_count": len([item for item in candidates if item.get("reminder_type") == "target_gap"]),
        "daily_summary_sent": any(item.get("candidate", {}).get("reminder_type") == "daily_summary" for item in sent),
        "reminder_send_failed": bool(failed),
    }


def _result(status: str, candidates: list[dict[str, Any]], decisions: list[dict[str, Any]], reason: str = "") -> dict[str, Any]:
    return {
        "status": status,
        "reason": reason,
        "candidates": candidates,
        "decisions": decisions,
        "sent_count": 0,
        "metrics": {
            "reminder_enabled": status != "disabled",
            "reminder_candidates_count": len(candidates),
            "reminder_sent_count": 0,
            "reminder_skipped_count": len(decisions),
            "reminder_skip_reason": reason or None,
            "anomaly_detected_count": 0,
            "pending_reminder_count": 0,
            "target_gap_reminder_count": 0,
            "daily_summary_sent": False,
            "reminder_send_failed": False,
        },
    }


def _event_id(candidate: dict[str, Any], status: str, legacy: ModuleType) -> str:
    return legacy._stable_uuid4_from_text(
        f"calorie-agent:reminder-event:{candidate.get('user_id')}:{candidate.get('rule_id')}:{candidate.get('trigger_reason')}:{status}"
    )


def _schedule_due(schedule_time: Any, now_millis: int, legacy: ModuleType) -> bool:
    text = str(schedule_time or "").strip()
    if not text:
        return True
    target = _parse_time_minutes(text)
    if target is None:
        return True
    return _local_minutes(now_millis, legacy) >= target


def _in_quiet_hours(now_millis: int, start: str, end: str, legacy: ModuleType) -> bool:
    start_minutes = _parse_time_minutes(start)
    end_minutes = _parse_time_minutes(end)
    if start_minutes is None or end_minutes is None or start_minutes == end_minutes:
        return False
    current = _local_minutes(now_millis, legacy)
    if start_minutes < end_minutes:
        return start_minutes <= current < end_minutes
    return current >= start_minutes or current < end_minutes


def _same_local_day(left_millis: float, right_millis: int, legacy: ModuleType) -> bool:
    if left_millis <= 0:
        return False
    return _local_date(left_millis, legacy) == _local_date(right_millis, legacy)


def _local_date(millis: float, legacy: ModuleType) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(millis / 1000 + legacy._timezone_offset_seconds()))


def _local_minutes(millis: int, legacy: ModuleType) -> int:
    tm = time.gmtime(millis / 1000 + legacy._timezone_offset_seconds())
    return tm.tm_hour * 60 + tm.tm_min


def _parse_time_minutes(value: str) -> int | None:
    try:
        hour, minute = str(value).strip().split(":", 1)
        return max(0, min(23, int(hour))) * 60 + max(0, min(59, int(minute)))
    except (ValueError, TypeError):
        return None


def _is_group_chat(candidate: dict[str, Any]) -> bool:
    chat_id = str(candidate.get("chat_id") or "").lower()
    facts = candidate.get("facts") if isinstance(candidate.get("facts"), dict) else {}
    return str(facts.get("chat_type") or "").lower() == "group" or chat_id.startswith("oc_group") or chat_id.startswith("group_")


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


def _num(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
