from __future__ import annotations

import json
import os
from collections.abc import Callable
from typing import Any

from calorie_agent.legacy_app import DEFAULT_SETTINGS

from .fallback_reply import build_deterministic_fallback
from .planner import default_http_json_post
from .reply_guard import GuardResult
from .reply_guard import validate_reply as guard_validate_reply
from .schemas import AgentPlan, ToolResult


HttpJsonPost = Callable[[str, dict[str, Any], dict[str, str] | None], dict[str, Any]]


REPORTING_TOOLS = {
    "generate_daily_report",
    "generate_weekly_report",
    "generate_monthly_report",
    "query_trend",
    "compare_period",
    "analyze_target_gap",
    "get_report",
    "regenerate_report",
}

REMINDER_TOOLS = {
    "list_reminder_rules",
    "update_reminder_rule",
    "pause_reminders",
    "resume_reminders",
    "snooze_reminder",
    "generate_daily_summary_reminder",
    "detect_anomalies",
    "send_reminder",
}


REPLY_SYSTEM_PROMPT = """
You write the final Chinese reply for a Feishu calorie tracking agent.
Use only the execution facts JSON supplied by the backend.
Do not invent foods, grams, kcal, carbs, protein, fat, remaining amounts, or success states.
Do not mention implementation details, JSON, tools, prompts, or policies.
Keep the reply concise and natural.
""".strip()


MEAL_LABELS = {
    "breakfast": "早餐",
    "lunch": "午餐",
    "dinner": "晚餐",
    "post_workout": "练后",
    "snack": "加餐",
    "unknown": "",
}


def compose_reply(
    message: dict[str, str],
    plan: AgentPlan,
    results: list[ToolResult],
    settings: dict[str, str] | None = None,
    context: dict[str, Any] | None = None,
    http_json_post: HttpJsonPost = default_http_json_post,
) -> str:
    return compose_reply_with_metadata(
        message,
        plan,
        results,
        settings=settings,
        context=context,
        http_json_post=http_json_post,
    )["reply"]


def compose_reply_with_metadata(
    message: dict[str, str],
    plan: AgentPlan,
    results: list[ToolResult],
    settings: dict[str, str] | None = None,
    context: dict[str, Any] | None = None,
    http_json_post: HttpJsonPost = default_http_json_post,
) -> dict[str, Any]:
    settings = settings or {}
    facts = build_execution_facts(message, plan, results, context=context)
    result_payloads = [result.to_dict() for result in results]
    fallback = build_deterministic_fallback(facts, result_payloads, default_reply=compose_deterministic_reply(facts))
    fallback_guard = guard_validate_reply(fallback, facts, result_payloads, fallback_reply=fallback)
    reply_mode = settings.get("agent_reply_mode") or os.getenv("AGENT_REPLY_MODE") or DEFAULT_SETTINGS["AGENT_REPLY_MODE"]
    if str(reply_mode).lower() != "free":
        return _reply_metadata(fallback, facts, fallback_guard, fallback_used=False)
    api_key = settings.get("deepseek_api_key") or os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        return _reply_metadata(fallback, facts, fallback_guard, fallback_used=False)
    try:
        reply = _compose_reply_with_deepseek(facts, settings, http_json_post=http_json_post)
    except Exception:
        return _reply_metadata(fallback, facts, fallback_guard, fallback_used=True, error_type="reply_composer_error")
    reply_validate = (
        settings.get("agent_reply_validate") or os.getenv("AGENT_REPLY_VALIDATE") or DEFAULT_SETTINGS["AGENT_REPLY_VALIDATE"]
    )
    if _truthy(reply_validate):
        guard = guard_validate_reply(reply, facts, result_payloads, fallback_reply=fallback)
        if not guard.passed:
            return _reply_metadata(guard.safe_reply or fallback, facts, guard, fallback_used=True)
        return _reply_metadata(reply.strip(), facts, guard, fallback_used=False)
    return _reply_metadata(reply.strip() or fallback, facts, GuardResult(True, []), fallback_used=False)


def build_execution_facts(
    message: dict[str, str],
    plan: AgentPlan,
    results: list[ToolResult],
    context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    facts: dict[str, Any] = {
        "goal": plan.goal,
        "action_count": len(plan.actions),
        "message_text": message.get("text", ""),
        "recorded": [],
        "duplicates": [],
        "pending": [],
        "confirmations": [],
        "queries": [],
        "intake_nutrition_queries": [],
        "undo": [],
        "standard_updates": [],
        "cleanup": [],
        "reports": [],
        "reporting": {
            "enabled": False,
            "generated": False,
            "cache_hit": False,
            "trend_query_used": False,
            "write_failed": False,
            "events": [],
        },
        "reminders": {
            "enabled": False,
            "rule_updated": False,
            "candidates_count": 0,
            "sent_count": 0,
            "skipped_count": 0,
            "skip_reason": "",
            "anomaly_detected_count": 0,
            "pending_reminder_count": 0,
            "target_gap_reminder_count": 0,
            "daily_summary_sent": False,
            "send_failed": False,
            "events": [],
        },
        "memory": {
            "alias_learned": False,
            "alias_confirmed": False,
            "default_grams_suggested": False,
            "default_grams_used": False,
            "combo_used": False,
            "combo_created": False,
            "preference_updated": False,
            "memory_write_failed": False,
            "events": [],
        },
        "errors": [],
        "latest_summary": {},
        "latest_standard": {},
        "latest_remaining": {},
    }
    if context:
        facts["context"] = {
            key: value
            for key, value in context.items()
            if key in {"event_type", "today", "active_pending", "memory_context"}
        }

    for result in results:
        data = result.data if isinstance(result.data, dict) else {}
        if result.status == "error":
            facts["errors"].append({"tool": result.tool, "error": result.error})
            continue
        if result.status == "needs_confirmation":
            facts["confirmations"].append(_confirmation_fact(result))
            continue

        if result.tool == "record_food":
            _collect_record_food_result(facts, result)
            if data.get("default_grams_used"):
                _collect_memory(facts, result)
            if isinstance(data.get("default_grams_suggestion"), dict):
                _collect_memory(
                    facts,
                    ToolResult(
                        "suggest_default_grams",
                        str(data["default_grams_suggestion"].get("status") or ""),
                        data["default_grams_suggestion"],
                    ),
                )
            if isinstance(data.get("alias_learning_result"), dict):
                _collect_memory(
                    facts,
                    ToolResult(
                        "learn_alias",
                        str(data["alias_learning_result"].get("status") or ""),
                        data["alias_learning_result"],
                    ),
                )
        elif result.tool == "query_today":
            _collect_summary(facts, data)
            facts["queries"].append({"status": result.status})
        elif result.tool == "query_intake_food_nutrition":
            facts.setdefault("intake_nutrition_queries", []).append(data)
        elif result.tool == "undo_intake":
            facts["undo"].append(_undo_fact(data))
            _collect_summary(facts, data)
        elif result.tool == "change_standard":
            facts["standard_updates"].append(_standard_fact(data))
        elif result.tool in {"create_pending", "cancel_pending", "pending_decision"}:
            _collect_pending(facts, data)
        elif result.tool == "create_food":
            facts.setdefault("created_foods", []).append(data.get("food", {}))
        elif result.tool == "search_food":
            if result.status == "needs_confirmation":
                facts["confirmations"].append(_confirmation_fact(result))
        elif result.tool == "clear_daily_intake":
            facts["cleanup"].append(
                {
                    "status": result.status,
                    "deleted_count": _number(data.get("deleted_count")),
                    "scanned_count": _number(data.get("scanned_count")),
                }
            )
        elif result.tool in REPORTING_TOOLS:
            _collect_reporting(facts, result)
        elif result.tool in REMINDER_TOOLS:
            _collect_reminders(facts, result)
        elif result.tool in {
            "search_memory",
            "suggest_default_grams",
            "learn_alias",
            "confirm_alias",
            "create_combo",
            "use_combo",
            "list_memories",
            "delete_memory",
            "update_preference",
        }:
            _collect_memory(facts, result)
            if result.tool == "use_combo":
                for nested in (data.get("record_results", []) if isinstance(data.get("record_results"), list) else []):
                    if not isinstance(nested, dict):
                        continue
                    _collect_record_food_result(
                        facts,
                        ToolResult(
                            "record_food",
                            str(nested.get("status") or "ok"),
                            nested.get("data") if isinstance(nested.get("data"), dict) else {},
                            action_id=str(nested.get("action_id") or ""),
                        ),
                    )

    return facts


def compose_deterministic_reply(facts: dict[str, Any]) -> str:
    parts: list[str] = []
    recorded = facts.get("recorded") if isinstance(facts.get("recorded"), list) else []
    duplicates = facts.get("duplicates") if isinstance(facts.get("duplicates"), list) else []
    pending = facts.get("pending") if isinstance(facts.get("pending"), list) else []
    confirmations = facts.get("confirmations") if isinstance(facts.get("confirmations"), list) else []
    undo_items = facts.get("undo") if isinstance(facts.get("undo"), list) else []
    standard_updates = facts.get("standard_updates") if isinstance(facts.get("standard_updates"), list) else []
    cleanup = facts.get("cleanup") if isinstance(facts.get("cleanup"), list) else []
    reports = facts.get("reports") if isinstance(facts.get("reports"), list) else []
    reminder_facts = facts.get("reminders") if isinstance(facts.get("reminders"), dict) else {}
    memory_facts = facts.get("memory") if isinstance(facts.get("memory"), dict) else {}
    errors = facts.get("errors") if isinstance(facts.get("errors"), list) else []

    if recorded:
        parts.append("已记录：" + _join_items([_recorded_label(item) for item in recorded]) + "。")
    if duplicates and not recorded:
        parts.append("这条消息之前已经处理过，本次没有重复写入。")
    if pending:
        parts.extend(_pending_lines(pending))
    if confirmations:
        parts.extend(_confirmation_lines(confirmations))
    if undo_items:
        parts.extend(_undo_lines(undo_items))
    if standard_updates:
        parts.extend(_standard_lines(standard_updates))
    if cleanup:
        parts.extend(_cleanup_lines(cleanup))
    if reports:
        parts.extend(_reporting_lines(reports))
    if reminder_facts:
        parts.extend(_reminder_lines(reminder_facts))
    if memory_facts:
        parts.extend(_memory_lines(memory_facts))

    summary_line = _summary_line(facts)
    if summary_line:
        parts.append(summary_line)

    if errors:
        parts.append("有步骤处理失败：" + _join_items([str(item.get("error") or item.get("tool")) for item in errors]) + "。")
    if not parts:
        parts.append("我还没能确定要执行的记录或查询，请换一种说法告诉我食物、克重或要查询的内容。")
    return "\n".join(part for part in parts if part)


def validate_reply(reply: str, facts: dict[str, Any]) -> bool:
    return guard_validate_reply(reply, facts).passed


def _reply_metadata(
    reply: str,
    facts: dict[str, Any],
    guard: GuardResult,
    fallback_used: bool,
    error_type: str = "",
) -> dict[str, Any]:
    return {
        "reply": reply,
        "execution_facts": facts,
        "reply_guard": guard.to_dict(),
        "reply_guard_passed": guard.passed,
        "reply_guard_fallback_used": fallback_used,
        "reply_error_type": error_type,
    }


def _compose_reply_with_deepseek(
    facts: dict[str, Any],
    settings: dict[str, str],
    http_json_post: HttpJsonPost = default_http_json_post,
) -> str:
    api_key = settings.get("deepseek_api_key") or os.getenv("DEEPSEEK_API_KEY", "")
    api_base = (
        settings.get("deepseek_api_base") or os.getenv("DEEPSEEK_API_BASE") or DEFAULT_SETTINGS["DEEPSEEK_API_BASE"]
    ).rstrip("/")
    model = settings.get("deepseek_model") or os.getenv("DEEPSEEK_MODEL") or DEFAULT_SETTINGS["DEEPSEEK_MODEL"]
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": REPLY_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": "Execution facts JSON:\n"
                + json.dumps(facts, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            },
        ],
        "temperature": 0.2,
        "max_tokens": 500,
        "stream": False,
    }
    data = http_json_post(
        f"{api_base}/chat/completions",
        payload,
        {"Authorization": f"Bearer {api_key}"},
    )
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("DeepSeek reply composer response does not include choices.")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("DeepSeek reply composer returned empty content.")
    return content.strip()


def _collect_record_food_result(facts: dict[str, Any], result: ToolResult) -> None:
    data = result.data if isinstance(result.data, dict) else {}
    if data.get("status") == "needs_confirmation" or result.status == "needs_confirmation":
        facts["confirmations"].append(_confirmation_fact(result))
        return

    nested = data.get("result") if isinstance(data.get("result"), dict) else {}
    if not nested and isinstance(data.get("pending_result"), dict):
        _collect_pending(facts, {"pending_result": data["pending_result"]})
        return
    lookup = nested.get("lookup_result") if isinstance(nested.get("lookup_result"), dict) else {}
    write = nested.get("intake_write_result") if isinstance(nested.get("intake_write_result"), dict) else {}
    write_status = str(write.get("status") or result.status)
    items = lookup.get("items") if isinstance(lookup.get("items"), list) else []

    for item in items:
        if not isinstance(item, dict):
            continue
        nutrition = item.get("nutrition") if isinstance(item.get("nutrition"), dict) else {}
        fact = {
            "food_name": str(item.get("matched_name") or item.get("input_name") or ""),
            "input_name": str(item.get("input_name") or ""),
            "grams": _number(item.get("grams")),
            "meal_type": _extract_meal_type(nested),
            "write_status": write_status,
            "kcal": _number(nutrition.get("kcal")),
            "carbs": _number(nutrition.get("carbs")),
            "protein": _number(nutrition.get("protein")),
            "fat": _number(nutrition.get("fat")),
        }
        if write_status == "duplicate_skipped":
            facts["duplicates"].append(fact)
        else:
            facts["recorded"].append(fact)

    pending_result = nested.get("pending_result") if isinstance(nested.get("pending_result"), dict) else {}
    if pending_result:
        _collect_pending(facts, {"pending_result": pending_result})
    _collect_summary(facts, nested)


def _collect_summary(facts: dict[str, Any], data: dict[str, Any]) -> None:
    summary = data.get("today_summary") if isinstance(data.get("today_summary"), dict) else {}
    standard = data.get("daily_standard") if isinstance(data.get("daily_standard"), dict) else {}
    if not summary or not standard:
        return
    remaining = _remaining(summary, standard)
    facts["latest_summary"] = summary
    facts["latest_standard"] = standard
    facts["latest_remaining"] = remaining


def _collect_pending(facts: dict[str, Any], data: dict[str, Any]) -> None:
    pending_result = data.get("pending_result") if isinstance(data.get("pending_result"), dict) else data
    if not isinstance(pending_result, dict) or not pending_result:
        return
    facts["pending"].append(
        {
            "status": str(pending_result.get("status") or data.get("status") or ""),
            "intent": str(pending_result.get("intent") or ""),
            "food_name": str(pending_result.get("food_name") or ""),
            "grams": _number(pending_result.get("grams")),
            "reply": str(pending_result.get("reply") or ""),
        }
    )
    result = data.get("result") if isinstance(data.get("result"), dict) else {}
    if result:
        _collect_summary(facts, result)


def _collect_memory(facts: dict[str, Any], result: ToolResult) -> None:
    memory_facts = facts.setdefault("memory", {})
    data = result.data if isinstance(result.data, dict) else {}
    event = {"tool": result.tool, "status": result.status, **data}
    memory_facts.setdefault("events", []).append(event)
    if result.status == "error" or str(data.get("status") or "").endswith("failed"):
        memory_facts["memory_write_failed"] = True
    if result.tool == "learn_alias" and result.status in {"created", "updated", "candidate"}:
        memory_facts["alias_learned"] = True
    if result.tool == "confirm_alias" and result.status in {"created", "updated"}:
        memory_facts["alias_confirmed"] = True
    if result.tool == "suggest_default_grams" and result.status in {"suggested", "default_grams_suggested"}:
        memory_facts["default_grams_suggested"] = True
    if result.tool == "record_food" and data.get("default_grams_used"):
        memory_facts["default_grams_used"] = True
    if result.tool == "create_combo" and result.status in {"created", "updated"}:
        memory_facts["combo_created"] = True
    if result.tool == "use_combo" and result.status in {"combo_used", "found"}:
        memory_facts["combo_used"] = True
    if result.tool == "update_preference" and result.status in {"created", "updated"}:
        memory_facts["preference_updated"] = True


def _collect_reporting(facts: dict[str, Any], result: ToolResult) -> None:
    reporting_facts = facts.setdefault("reporting", {})
    data = result.data if isinstance(result.data, dict) else {}
    report = data.get("report") if isinstance(data.get("report"), dict) else {}
    report_facts = data.get("facts") if isinstance(data.get("facts"), dict) else report.get("facts") if isinstance(report.get("facts"), dict) else {}
    period = data.get("period") if isinstance(data.get("period"), dict) else report.get("period") if isinstance(report.get("period"), dict) else {}
    cache_result = data.get("cache_result") if isinstance(data.get("cache_result"), dict) else {}
    event = {
        "tool": result.tool,
        "status": result.status,
        "report_type": str(data.get("report_type") or report.get("report_type") or ""),
        "period": period,
        "summary_text": str(data.get("summary_text") or report.get("summary_text") or ""),
        "facts": report_facts if isinstance(report_facts, dict) else {},
        "cache_status": str(cache_result.get("status") or ""),
        "metric": str(data.get("metric") or ""),
    }
    facts.setdefault("reports", []).append(event)
    reporting_facts.setdefault("events", []).append(event)
    reporting_facts["enabled"] = result.status != "disabled"
    reporting_facts["generated"] = reporting_facts.get("generated") or result.status == "generated"
    reporting_facts["cache_hit"] = reporting_facts.get("cache_hit") or result.status == "cache_hit"
    reporting_facts["trend_query_used"] = reporting_facts.get("trend_query_used") or bool(data.get("trend_query_used")) or result.tool in {"query_trend", "compare_period"}
    reporting_facts["write_failed"] = reporting_facts.get("write_failed") or str(cache_result.get("status") or "") in {"invalid", "error", "failed"}
    if period:
        reporting_facts["latest_period"] = period
        reporting_facts["report_period_days"] = _number(period.get("days"))
    if event["report_type"]:
        reporting_facts["latest_report_type"] = event["report_type"]
    targets = report_facts.get("targets") if isinstance(report_facts, dict) and isinstance(report_facts.get("targets"), dict) else {}
    hit_rates = targets.get("hit_rates") if isinstance(targets.get("hit_rates"), dict) else {}
    if hit_rates:
        reporting_facts["target_hit_rate"] = _number(hit_rates.get("kcal"))


def _collect_reminders(facts: dict[str, Any], result: ToolResult) -> None:
    reminder_facts = facts.setdefault("reminders", {})
    data = result.data if isinstance(result.data, dict) else {}
    event = {"tool": result.tool, "status": result.status, **data}
    reminder_facts.setdefault("events", []).append(event)
    if result.tool == "update_reminder_rule" and result.status in {"created", "updated"}:
        reminder_facts["rule_updated"] = True
    if result.tool in {"pause_reminders", "resume_reminders", "snooze_reminder"} and result.status in {"paused", "resumed", "snoozed"}:
        reminder_facts["rule_updated"] = True
    metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else {}
    if metrics:
        reminder_facts["enabled"] = bool(metrics.get("reminder_enabled"))
        reminder_facts["candidates_count"] = _number(metrics.get("reminder_candidates_count")) or 0
        reminder_facts["sent_count"] = _number(metrics.get("reminder_sent_count")) or 0
        reminder_facts["skipped_count"] = _number(metrics.get("reminder_skipped_count")) or 0
        reminder_facts["skip_reason"] = str(metrics.get("reminder_skip_reason") or "")
        reminder_facts["anomaly_detected_count"] = _number(metrics.get("anomaly_detected_count")) or 0
        reminder_facts["pending_reminder_count"] = _number(metrics.get("pending_reminder_count")) or 0
        reminder_facts["target_gap_reminder_count"] = _number(metrics.get("target_gap_reminder_count")) or 0
        reminder_facts["daily_summary_sent"] = bool(metrics.get("daily_summary_sent"))
        reminder_facts["send_failed"] = bool(metrics.get("reminder_send_failed"))
    if result.tool == "detect_anomalies":
        reminder_facts["anomaly_detected_count"] = _number(data.get("count")) or 0
    if result.tool == "send_reminder":
        reminder_facts["sent_count"] = (reminder_facts.get("sent_count") or 0) + (1 if result.status == "sent" else 0)
        reminder_facts["send_failed"] = reminder_facts.get("send_failed") or result.status == "failed"
    candidates = data.get("candidates") if isinstance(data.get("candidates"), list) else []
    if candidates:
        reminder_facts["candidates_count"] = max(float(reminder_facts.get("candidates_count") or 0), float(len(candidates)))


def _confirmation_fact(result: ToolResult) -> dict[str, Any]:
    data = result.data if isinstance(result.data, dict) else {}
    candidates = data.get("candidates") if isinstance(data.get("candidates"), list) else []
    return {
        "tool": result.tool,
        "status": result.status,
        "query": str(data.get("query") or data.get("food_query") or ""),
        "candidates": candidates[:5],
    }


def _undo_fact(data: dict[str, Any]) -> dict[str, Any]:
    undo = data.get("undo_result") if isinstance(data.get("undo_result"), dict) else data
    items = undo.get("items") if isinstance(undo.get("items"), list) else []
    return {
        "status": str(undo.get("status") or ""),
        "deleted_count": _number(undo.get("deleted_count")),
        "items": [
            {
                "food_name": str(item.get("food_name") or ""),
                "grams": _number(item.get("grams")),
                "meal_type": str(item.get("meal_type") or ""),
            }
            for item in items
            if isinstance(item, dict)
        ],
    }


def _standard_fact(data: dict[str, Any]) -> dict[str, Any]:
    result = data.get("result") if isinstance(data.get("result"), dict) else data
    standard = result.get("standard") if isinstance(result.get("standard"), dict) else {}
    return {
        "status": str(result.get("status") or data.get("status") or ""),
        "kcal": _number(standard.get("kcal")),
        "carbs": _number(standard.get("carbs")),
        "protein": _number(standard.get("protein")),
        "fat": _number(standard.get("fat")),
    }


def _extract_meal_type(result: dict[str, Any]) -> str:
    lookup = result.get("lookup_result") if isinstance(result.get("lookup_result"), dict) else {}
    parse_result = result.get("parse_result") if isinstance(result.get("parse_result"), dict) else {}
    return str(parse_result.get("meal_type") or lookup.get("meal_type") or "")


def _remaining(summary: dict[str, Any], standard: dict[str, Any]) -> dict[str, float]:
    totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
    return {
        key: _round(max(0.0, float(standard.get(key) or 0) - float(totals.get(key) or 0)))
        for key in ("kcal", "carbs", "protein", "fat")
    }


def _summary_line(facts: dict[str, Any]) -> str:
    summary = facts.get("latest_summary") if isinstance(facts.get("latest_summary"), dict) else {}
    standard = facts.get("latest_standard") if isinstance(facts.get("latest_standard"), dict) else {}
    remaining = facts.get("latest_remaining") if isinstance(facts.get("latest_remaining"), dict) else {}
    totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
    if not totals or not standard:
        return ""
    return (
        f"今天累计 {_fmt(totals.get('kcal'))}/{_fmt(standard.get('kcal'))} kcal，"
        f"还剩 {_fmt(remaining.get('kcal'))} kcal。"
        f"碳水 {_fmt(totals.get('carbs'))}/{_fmt(standard.get('carbs'))}g，"
        f"蛋白质 {_fmt(totals.get('protein'))}/{_fmt(standard.get('protein'))}g，"
        f"脂肪 {_fmt(totals.get('fat'))}/{_fmt(standard.get('fat'))}g。"
    )


def _recorded_label(item: dict[str, Any]) -> str:
    meal = MEAL_LABELS.get(str(item.get("meal_type") or "unknown"), "")
    food = str(item.get("food_name") or item.get("input_name") or "该食物")
    grams = _fmt(item.get("grams"))
    return f"{meal}{food} {grams}g" if meal else f"{food} {grams}g"


def _pending_lines(pending: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in pending:
        status = str(item.get("status") or "")
        food = str(item.get("food_name") or "该食物")
        if status == "cancelled":
            lines.append(f"已取消 {food} 的待补全记录。")
        elif str(item.get("intent") or "") == "missing_grams":
            lines.append(f"{food} 还缺克重，请补充例如 200g。")
        elif str(item.get("intent") or "") == "missing_food":
            lines.append(f"食物库还没有 {food}，请补充每100g碳水、蛋白质、脂肪；也可以直接说取消这个。")
        elif item.get("reply"):
            lines.append(str(item["reply"]))
    return lines


def _confirmation_lines(confirmations: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in confirmations:
        candidates = item.get("candidates") if isinstance(item.get("candidates"), list) else []
        names = []
        for candidate in candidates:
            food = candidate.get("food") if isinstance(candidate, dict) else {}
            if isinstance(food, dict) and food.get("name"):
                names.append(str(food["name"]))
        if names:
            lines.append("我找到了多个可能的食物：" + _join_items(names) + "。请回复更准确的名称。")
        else:
            lines.append("这个操作需要你再确认一下目标。")
    return lines


def _undo_lines(undo_items: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in undo_items:
        status = str(item.get("status") or "")
        if status == "deleted":
            names = [str(row.get("food_name") or "") for row in item.get("items", []) if isinstance(row, dict)]
            lines.append("已撤销：" + (_join_items([name for name in names if name]) or "对应记录") + "。")
        elif status == "ambiguous":
            lines.append("找到多条可能要撤销的记录，请说得更具体一点。")
        elif status == "nothing_to_undo":
            lines.append("今天没有找到可撤销的记录。")
        else:
            lines.append("撤销没有完成，请再明确要撤销哪一条。")
    return lines


def _standard_lines(standards: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in standards:
        if item.get("status") in {"created", "updated"}:
            lines.append(
                f"已更新每日标准：{_fmt(item.get('kcal'))} kcal，"
                f"碳水 {_fmt(item.get('carbs'))}g，蛋白质 {_fmt(item.get('protein'))}g，脂肪 {_fmt(item.get('fat'))}g。"
            )
    return lines


def _cleanup_lines(cleanup: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in cleanup:
        lines.append(f"每日清理完成，已删除 {_fmt(item.get('deleted_count'))} 条历史摄入记录。")
    return lines


def _reminder_lines(reminder_facts: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    events = reminder_facts.get("events") if isinstance(reminder_facts.get("events"), list) else []
    for event in events:
        if not isinstance(event, dict):
            continue
        tool = str(event.get("tool") or "")
        status = str(event.get("status") or "")
        if tool == "list_reminder_rules":
            lines.append(f"当前提醒规则 {int(_number(event.get('count')) or 0)} 条。")
        elif tool == "update_reminder_rule" and status in {"created", "updated"}:
            lines.append("已更新主动提醒设置。")
        elif tool == "pause_reminders" and status == "paused":
            lines.append("已暂停主动提醒。")
        elif tool == "resume_reminders" and status == "resumed":
            lines.append("已恢复主动提醒。")
        elif tool == "snooze_reminder" and status == "snoozed":
            lines.append("已稍后提醒。")
        elif tool == "generate_daily_summary_reminder" and status == "generated":
            candidate = event.get("candidate") if isinstance(event.get("candidate"), dict) else {}
            lines.append(str(candidate.get("message_text") or "已生成今日总结提醒。"))
        elif tool == "detect_anomalies":
            lines.append(f"检测到 {int(_number(event.get('count')) or 0)} 个需要复核的提醒项。")
        elif tool == "send_reminder" and status == "sent":
            lines.append("提醒已发送。")
        elif tool == "send_reminder" and status == "failed":
            lines.append("提醒发送失败。")
    if not lines and reminder_facts.get("candidates_count"):
        lines.append(f"生成 {int(_number(reminder_facts.get('candidates_count')) or 0)} 个提醒候选。")
    return [line for line in lines if line.strip()]


def _memory_lines(memory_facts: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    events = memory_facts.get("events") if isinstance(memory_facts.get("events"), list) else []
    for event in events:
        if not isinstance(event, dict):
            continue
        tool = str(event.get("tool") or "")
        status = str(event.get("status") or "")
        if tool == "suggest_default_grams" and status in {"suggested", "default_grams_suggested"}:
            lines.append(
                f"{event.get('food_name') or 'this food'} usually uses {_fmt(event.get('default_grams'))}g. Reply yes to record it, or send another grams value."
            )
        elif tool == "confirm_alias" and status in {"created", "updated"}:
            lines.append(f"Saved alias: {event.get('alias') or ''}.")
        elif tool == "learn_alias" and status in {"created", "updated", "candidate"}:
            lines.append(f"Saved alias candidate: {event.get('alias') or ''}.")
        elif tool == "create_combo" and status in {"created", "updated"}:
            lines.append(f"Saved combo: {event.get('combo_name') or ''}.")
        elif tool == "use_combo" and status in {"combo_used", "found"}:
            lines.append(f"Used combo: {event.get('combo_name') or _nested_text(event, 'combo', 'combo_name')}.")
        elif tool == "update_preference" and status in {"created", "updated"}:
            lines.append("Preference updated.")
        elif tool == "delete_memory" and status == "deleted":
            lines.append(f"Deleted {_fmt(event.get('deleted_count'))} memory item(s).")
    return [line for line in lines if line.strip()]


def _reporting_lines(reports: list[dict[str, Any]]) -> list[str]:
    lines: list[str] = []
    for item in reports:
        status = str(item.get("status") or "")
        summary = str(item.get("summary_text") or "").strip()
        if summary:
            lines.append(summary)
        elif status == "disabled":
            lines.append("报告功能当前未启用。")
        elif status in {"not_found", "miss"}:
            lines.append("没有找到对应的缓存报告，可以重新生成。")
        elif status == "error":
            lines.append("报告生成失败，请稍后重试。")
    return lines


def _nested_text(payload: dict[str, Any], key: str, nested_key: str) -> str:
    value = payload.get(key)
    if isinstance(value, dict):
        return str(value.get(nested_key) or "")
    return ""


def _join_items(items: list[str]) -> str:
    cleaned = [item for item in items if item]
    if len(cleaned) <= 2:
        return "和".join(cleaned)
    return "、".join(cleaned[:-1]) + "和" + cleaned[-1]


def _number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return _round(float(value))
    except (TypeError, ValueError):
        return None


def _round(value: float) -> float:
    return round(float(value) + 1e-9, 1)


def _fmt(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "0"
    return f"{number:g}"


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
