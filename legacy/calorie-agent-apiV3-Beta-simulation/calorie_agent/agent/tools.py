from __future__ import annotations

from types import ModuleType
from typing import Any

from .schemas import AgentAction, AgentPlan, ToolResult
from .. import legacy_app
from ..domain import aliases, anomaly, combos, memory, notification_rules, preferences, reminders, reporting
from ..domain.food_match import choose_food_match, match_food_candidates
from ..domain.pending import handle_active_pending_message
from ..domain.rollover import clear_daily_intake
from ..domain.undo import undo_intake


def execute_agent_plan(
    message: dict[str, str],
    plan: AgentPlan,
    legacy: ModuleType = legacy_app,
) -> list[ToolResult]:
    if plan.needs_confirmation:
        return [
            ToolResult(action.tool, "needs_confirmation", {"target": action.target}, action_id=action.action_id)
            for action in plan.actions
        ]
    return [_execute_action(message, action, index, legacy) for index, action in enumerate(plan.actions)]


def _execute_action(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> ToolResult:
    try:
        data = _TOOLS[action.tool](message, action, index, legacy)
        return ToolResult(action.tool, str(data.get("status") or "ok"), data, action_id=action.action_id)
    except Exception as exc:
        return ToolResult(action.tool, "error", error=str(exc), action_id=action.action_id)


def _action_message(message: dict[str, str], action: AgentAction, index: int) -> dict[str, str]:
    return {
        **message,
        "message_id": f"{message.get('message_id', '')}:agent:{index}",
        "text": _action_text(message, action),
    }


def _action_text(message: dict[str, str], action: AgentAction) -> str:
    raw = str(message.get("text") or "")
    return f"{raw} | agent_action={action.action_id or action.tool}"


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _food_database_for_user(message: dict[str, str], legacy: ModuleType) -> list[dict[str, Any]]:
    return aliases.apply_user_aliases(legacy._load_food_database(message), message, legacy=legacy)


def _food_name(action: AgentAction) -> str:
    params = action.params
    target = action.target
    return str(
        params.get("food_query")
        or params.get("food_name")
        or params.get("name")
        or target.get("food_query")
        or target.get("food_name")
        or target.get("name")
        or ""
    ).strip()


def _meal_type(action: AgentAction) -> str:
    value = str(action.params.get("meal_type") or action.target.get("meal_type") or "unknown").strip()
    return value or "unknown"


def _add_food_parse_result(action: AgentAction, food_name: str | None = None) -> dict[str, Any]:
    return {
        "intent": "add_food",
        "date": str(action.params.get("date") or "today"),
        "meal_type": _meal_type(action),
        "foods": [{"name": food_name if food_name is not None else _food_name(action), "grams": _number_or_none(action.params.get("grams"))}],
        "standard": {"carbs": None, "protein": None, "fat": None},
        "missing": [],
        "notes": action.reason,
    }


def _add_food_parse_result_from_values(
    action: AgentAction,
    food_name: str,
    grams: float | None,
) -> dict[str, Any]:
    result = _add_food_parse_result(action, food_name)
    result["foods"][0]["grams"] = grams
    return result


def _tool_record_food(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    food_match = choose_food_match(_food_name(action), _food_database_for_user(message, legacy))
    if food_match["status"] == "needs_confirmation":
        return food_match
    if food_match["status"] == "not_found":
        parse_result = _add_food_parse_result(action)
        missing_reason = "grams_missing" if parse_result["foods"][0]["grams"] is None else "food_not_found"
        pending = legacy._create_pending_task_for_missing(
            _action_message(message, action, index),
            parse_result,
            {
                "name": _food_name(action),
                "grams": parse_result["foods"][0]["grams"],
                "reason": missing_reason,
            },
        )
        return {
            "status": "pending_created" if pending.get("status") == "created" else pending.get("status", "not_found"),
            "food_match": food_match,
            "pending_result": pending,
        }
    matched_food = food_match["candidate"]["food"] if isinstance(food_match.get("candidate"), dict) else {}
    matched_name = str(matched_food.get("name") or _food_name(action))
    grams = _number_or_none(action.params.get("grams"))
    if grams is None:
        default = memory.suggest_default_grams(message, matched_food, legacy=legacy)
        if default.get("status") == "suggested":
            if default.get("auto_use"):
                parse_result = _add_food_parse_result_from_values(action, matched_name, _number_or_none(default.get("default_grams")))
                result = legacy._handle_add_food_command(_action_message(message, action, index), parse_result)
                result = {**result, "parse_result": parse_result}
                return {
                    "status": result.get("status", "ok"),
                    "food_match": food_match,
                    "result": result,
                    "reply": result.get("reply", ""),
                    "default_grams_used": True,
                    "default_grams_suggestion": default,
                }

            parse_result = _add_food_parse_result_from_values(action, matched_name, _number_or_none(default.get("default_grams")))
            pending = legacy._create_pending_task_for_missing(
                _action_message(message, action, index),
                parse_result,
                {
                    "name": matched_name,
                    "grams": default.get("default_grams"),
                    "reason": "confirm_default_grams",
                },
            )
            return {
                "status": "default_grams_suggested",
                "food_match": food_match,
                "default_grams_suggestion": default,
                "pending_result": pending,
            }

        parse_result = _add_food_parse_result_from_values(action, matched_name, None)
        pending = legacy._create_pending_task_for_missing(
            _action_message(message, action, index),
            parse_result,
            {"name": matched_name, "grams": None, "reason": "grams_missing"},
        )
        return {
            "status": "pending_created" if pending.get("status") == "created" else pending.get("status", "missing_grams"),
            "food_match": food_match,
            "pending_result": pending,
        }

    parse_result = _add_food_parse_result(action, matched_name)
    result = legacy._handle_add_food_command(_action_message(message, action, index), parse_result)
    result = {**result, "parse_result": parse_result}
    alias_learning = aliases.learn_alias_candidate(message, food_match, legacy=legacy)
    return {
        "status": result.get("status", "ok"),
        "food_match": food_match,
        "result": result,
        "reply": result.get("reply", ""),
        "alias_learning_result": alias_learning,
    }


def _tool_query_today(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    summary = legacy._load_today_intake_summary(message)
    standard = legacy._load_daily_standard(message)
    return {
        "status": "ok",
        "today_summary": summary,
        "daily_standard": standard,
        "reply": legacy._format_today_query_reply(summary, standard),
    }


def _tool_undo_intake(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    undo = undo_intake(message, action.target, legacy=legacy)
    summary = legacy._load_today_intake_summary(message)
    standard = legacy._load_daily_standard(message)
    if undo.get("status") != "deleted":
        return {
            "status": undo.get("status", "ok"),
            "undo_result": undo,
            "today_summary": summary,
            "daily_standard": standard,
        }
    return {
        "status": undo.get("status", "ok"),
        "undo_result": undo,
        "today_summary": summary,
        "daily_standard": standard,
        "reply": _format_semantic_undo_reply(undo),
    }


def _tool_change_standard(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    params = action.params
    parse_result = {
        "standard": {
            "carbs": _number_or_none(params.get("carbs")),
            "protein": _number_or_none(params.get("protein")),
            "fat": _number_or_none(params.get("fat")),
        }
    }
    result = legacy._upsert_daily_standard(message, parse_result)
    return {"status": result.get("status", "ok"), "result": result, "reply": legacy._format_change_standard_reply(result)}


def _tool_search_food(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    foods = _food_database_for_user(message, legacy)
    candidates = match_food_candidates(_food_name(action), foods)
    if not candidates:
        return {"status": "not_found", "query": _food_name(action), "candidates": []}
    choice = choose_food_match(_food_name(action), foods)
    return {
        "status": "ok" if choice["status"] == "auto_matched" else "needs_confirmation",
        "match_status": choice["status"],
        "query": _food_name(action),
        "candidates": [candidate.to_dict() for candidate in candidates],
    }


def _tool_create_food(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    params = action.params
    per_100g = {
        "carbs": _number_or_none(params.get("carbs")),
        "protein": _number_or_none(params.get("protein")),
        "fat": _number_or_none(params.get("fat")),
    }
    if any(value is None for value in per_100g.values()):
        return {"status": "missing_macros", "food_name": _food_name(action)}
    food = legacy._create_food_database_record(_food_name(action), per_100g, message.get("user_id", ""))  # type: ignore[arg-type]
    return {"status": "created", "food": food}


def _tool_create_pending(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    reason = str(action.params.get("reason") or action.target.get("reason") or "missing_food")
    missing_reason = "grams_missing" if reason == "missing_grams" else "food_not_found"
    parse_result = _add_food_parse_result(action)
    pending = legacy._create_pending_task_for_missing(
        message,
        parse_result,
        {"name": _food_name(action), "grams": parse_result["foods"][0]["grams"], "reason": missing_reason},
    )
    return {"status": pending.get("status", "ok"), "pending_result": pending}


def _tool_cancel_pending(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    handled = handle_active_pending_message({**message, "text": "取消这个"}, legacy=legacy)
    result = handled.get("result") if isinstance(handled.get("result"), dict) else {}
    return {"status": handled.get("status", "not_found"), "pending_result": result, "pending_task": handled.get("task", {})}


def _tool_clear_daily_intake(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    return clear_daily_intake(legacy=legacy)


def _tool_search_memory(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    kind = str(action.params.get("memory_type") or action.params.get("kind") or action.target.get("memory_type") or "").strip()
    if kind == "combo":
        items = combos.list_combos(message, legacy=legacy)
    elif kind == "preference":
        items = preferences.list_preferences(message, legacy=legacy)
    else:
        source = "alias" if kind == "alias" else ""
        items = memory.list_user_memories(message, source=source, legacy=legacy)
    return {"status": "ok", "memory_type": kind or "all", "items": items, "count": len(items)}


def _tool_suggest_default_grams(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    food_match = choose_food_match(_food_name(action), _food_database_for_user(message, legacy))
    if food_match.get("status") != "auto_matched":
        return {"status": food_match.get("status", "not_found"), "food_match": food_match}
    food = food_match["candidate"]["food"]
    result = memory.suggest_default_grams(message, food, legacy=legacy)
    return {**result, "food_match": food_match}


def _tool_learn_alias(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    alias = str(action.params.get("alias") or action.target.get("alias") or _food_name(action)).strip()
    food_query = str(action.params.get("food_query") or action.params.get("food_name") or action.target.get("food_query") or "").strip()
    if not alias or not food_query:
        return {"status": "invalid", "reason": "alias_and_food_query_required"}
    food_match = choose_food_match(food_query, legacy._load_food_database())
    if food_match.get("status") not in {"auto_matched", "needs_confirmation"}:
        return {"status": "food_not_found", "food_match": food_match}
    candidate = food_match.get("candidate") if isinstance(food_match.get("candidate"), dict) else {}
    food = candidate.get("food") if isinstance(candidate.get("food"), dict) else {}
    result = memory.upsert_memory(
        message,
        {
            "source": "alias_candidate",
            "food_id": food.get("food_id", ""),
            "food_name": food.get("name", ""),
            "alias": alias,
            "confidence": action.confidence if action.confidence is not None else 0.85,
            "sample_count": 1,
            "status": "candidate",
        },
        legacy=legacy,
    )
    return {**result, "alias": alias, "food": food, "food_match": food_match}


def _tool_confirm_alias(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    alias = str(action.params.get("alias") or action.target.get("alias") or "").strip()
    food_query = str(action.params.get("food_query") or action.params.get("food_name") or action.target.get("food_query") or "").strip()
    return aliases.confirm_alias(
        message,
        alias=alias,
        food_query=food_query,
        confidence=action.confidence if action.confidence is not None else 1.0,
        legacy=legacy,
    )


def _tool_create_combo(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    params = action.params
    combo_name = str(params.get("combo_name") or params.get("name") or action.target.get("combo_name") or "").strip()
    items = params.get("items") if isinstance(params.get("items"), list) else []
    result = combos.create_combo(
        message,
        combo_name=combo_name,
        meal_type=_meal_type(action),
        items=items,
        confidence=action.confidence if action.confidence is not None else 1.0,
        legacy=legacy,
    )
    return {**result, "combo_name": combo_name}


def _tool_use_combo(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    combo_name = str(action.params.get("combo_name") or action.params.get("name") or action.target.get("combo_name") or "").strip()
    result = combos.use_combo(message, combo_name, legacy=legacy)
    if result.get("status") != "found":
        return result
    record_results: list[dict[str, Any]] = []
    for item_index, item in enumerate(result.get("items", [])):
        if not isinstance(item, dict):
            continue
        sub_action = AgentAction(
            tool="record_food",
            params={
                "food_query": item.get("food_query") or item.get("food_name") or item.get("name"),
                "grams": item.get("grams"),
                "meal_type": item.get("meal_type") or result.get("combo", {}).get("meal_type") or "unknown",
            },
            action_id=f"{action.action_id or 'combo'}:{item_index}",
            reason=f"use_combo:{combo_name}",
        )
        data = _tool_record_food(message, sub_action, (index + 1) * 1000 + item_index, legacy)
        record_results.append({"tool": "record_food", "status": data.get("status", "ok"), "data": data, "action_id": sub_action.action_id})
    return {**result, "status": "combo_used", "combo_name": combo_name, "record_results": record_results}


def _tool_list_memories(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    return _tool_search_memory(message, action, index, legacy)


def _tool_delete_memory(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    target = {**action.target, **action.params}
    kind = str(target.get("memory_type") or target.get("source") or target.get("kind") or "").strip()
    if kind == "combo":
        return combos.delete_combo(message, str(target.get("combo_name") or target.get("name") or ""), legacy=legacy)
    if kind == "preference":
        return preferences.delete_preferences(message, target, legacy=legacy)
    if kind == "alias":
        return aliases.delete_alias(message, target, legacy=legacy)
    return memory.soft_delete_memories(message, target, legacy=legacy)


def _tool_update_preference(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    key = str(action.params.get("key") or action.target.get("key") or "").strip()
    value = action.params.get("value")
    if value is None:
        value = action.target.get("value")
    return preferences.update_preference(
        message,
        key=key,
        value=value,
        confidence=action.confidence if action.confidence is not None else 1.0,
        legacy=legacy,
    )


def _report_params(action: AgentAction) -> dict[str, Any]:
    return {**action.target, **action.params}


def _tool_generate_daily_report(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    return reporting.generate_report(message, "daily", _report_params(action), legacy)


def _tool_generate_weekly_report(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    return reporting.generate_report(message, "weekly", _report_params(action), legacy)


def _tool_generate_monthly_report(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    return reporting.generate_report(message, "monthly", _report_params(action), legacy)


def _tool_query_trend(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    return reporting.query_trend(message, _report_params(action), legacy)


def _tool_compare_period(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    return reporting.compare_period(message, _report_params(action), legacy)


def _tool_analyze_target_gap(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    return reporting.analyze_target_gap(message, _report_params(action), legacy)


def _tool_get_report(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    return reporting.get_report(message, _report_params(action), legacy)


def _tool_regenerate_report(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    params = _report_params(action)
    report_type = str(params.get("report_type") or params.get("type") or "weekly")
    return reporting.regenerate_report(message, report_type, params, legacy)


def _tool_list_reminder_rules(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    rules = notification_rules.list_rules(message, legacy=legacy)
    return {"status": "ok", "rules": rules, "count": len(rules)}


def _tool_update_reminder_rule(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    params = {**action.target, **action.params}
    if params.get("all") is True or str(params.get("scope") or "").lower() == "all":
        results = []
        for reminder_type in sorted(notification_rules.REMINDER_TYPES):
            rule_params = {**params, "reminder_type": reminder_type}
            results.append(notification_rules.update_rule(message, rule_params, legacy=legacy))
        updated = len([item for item in results if item.get("status") in {"created", "updated"}])
        return {"status": "updated" if updated else "disabled", "updated_count": updated, "results": results}
    result = notification_rules.update_rule(message, params, legacy=legacy)
    return {"status": result.get("status", "ok"), "result": result}


def _tool_pause_reminders(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    return notification_rules.pause_reminders(message, {**action.target, **action.params}, legacy=legacy)


def _tool_resume_reminders(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    return notification_rules.resume_reminders(message, legacy=legacy)


def _tool_snooze_reminder(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    return notification_rules.snooze_reminder(message, {**action.target, **action.params}, legacy=legacy)


def _tool_generate_daily_summary_reminder(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    return reminders.generate_daily_summary_candidate(message, legacy=legacy)


def _tool_detect_anomalies(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    return anomaly.detect_anomalies(message, legacy=legacy)


def _tool_send_reminder(
    message: dict[str, str],
    action: AgentAction,
    index: int,
    legacy: ModuleType,
) -> dict[str, Any]:
    settings = legacy._settings()
    candidate = action.params.get("candidate") if isinstance(action.params.get("candidate"), dict) else {}
    if not candidate:
        generated = reminders.generate_daily_summary_candidate(message, legacy=legacy)
        candidate = generated.get("candidate") if isinstance(generated.get("candidate"), dict) else {}
    if not candidate:
        return {"status": "no_candidate"}
    candidate.setdefault("user_id", message.get("user_id", ""))
    candidate.setdefault("chat_id", message.get("chat_id", ""))
    return reminders.send_candidate(candidate, settings, legacy)


_TOOLS = {
    "record_food": _tool_record_food,
    "query_today": _tool_query_today,
    "undo_intake": _tool_undo_intake,
    "change_standard": _tool_change_standard,
    "search_food": _tool_search_food,
    "create_food": _tool_create_food,
    "create_pending": _tool_create_pending,
    "cancel_pending": _tool_cancel_pending,
    "clear_daily_intake": _tool_clear_daily_intake,
    "search_memory": _tool_search_memory,
    "suggest_default_grams": _tool_suggest_default_grams,
    "learn_alias": _tool_learn_alias,
    "confirm_alias": _tool_confirm_alias,
    "create_combo": _tool_create_combo,
    "use_combo": _tool_use_combo,
    "list_memories": _tool_list_memories,
    "delete_memory": _tool_delete_memory,
    "update_preference": _tool_update_preference,
    "generate_daily_report": _tool_generate_daily_report,
    "generate_weekly_report": _tool_generate_weekly_report,
    "generate_monthly_report": _tool_generate_monthly_report,
    "query_trend": _tool_query_trend,
    "compare_period": _tool_compare_period,
    "analyze_target_gap": _tool_analyze_target_gap,
    "get_report": _tool_get_report,
    "regenerate_report": _tool_regenerate_report,
    "list_reminder_rules": _tool_list_reminder_rules,
    "update_reminder_rule": _tool_update_reminder_rule,
    "pause_reminders": _tool_pause_reminders,
    "resume_reminders": _tool_resume_reminders,
    "snooze_reminder": _tool_snooze_reminder,
    "generate_daily_summary_reminder": _tool_generate_daily_summary_reminder,
    "detect_anomalies": _tool_detect_anomalies,
    "send_reminder": _tool_send_reminder,
}


def _format_semantic_undo_reply(undo_result: dict[str, Any]) -> str:
    items = undo_result.get("items") if isinstance(undo_result.get("items"), list) else []
    names = [str(item.get("food_name") or "") for item in items if isinstance(item, dict)]
    suffix = "、".join(name for name in names if name)
    if suffix:
        return f"已撤销：{suffix}"
    return "已撤销对应记录。"
