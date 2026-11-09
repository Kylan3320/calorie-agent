from __future__ import annotations

import os
import time
from collections.abc import Callable
from types import ModuleType
from typing import Any

from .planner import AGENT_PLANNER_PROMPT_VERSION, plan_user_message_with_deepseek
from .reply_composer import build_execution_facts, compose_reply, compose_reply_with_metadata
from .reply_guard import validate_reply as guard_validate_reply
from .schemas import AgentPlan, PlanValidationError, ToolResult
from .tools import execute_agent_plan
from .trace import AgentTrace, build_error_trace, emit_agent_trace, make_trace_id
from .v2_runtime import attach_v2_gray_result, evaluate_v2_runtime
from .memory_context import build_memory_context
from .. import legacy_app
from ..domain.pending import classify_pending_message, handle_pending_decision


Planner = Callable[[str, dict[str, str], dict[str, Any] | None], AgentPlan]
ReplyComposer = Callable[[dict[str, str], AgentPlan, list[ToolResult], dict[str, str] | None, dict[str, Any] | None], str]


def handle_message(
    message: dict[str, str],
    legacy: ModuleType = legacy_app,
    settings: dict[str, str] | None = None,
    planner: Planner | None = None,
    reply_composer: ReplyComposer | None = None,
) -> dict[str, Any]:
    started_at = time.monotonic()
    settings = settings or legacy._settings()
    planner = planner or plan_user_message_with_deepseek
    context = load_context(message, legacy=legacy, settings=settings)

    try:
        pending_result = _try_handle_active_pending(
            message,
            context,
            settings=settings,
            legacy=legacy,
            started_at=started_at,
        )
        if pending_result is not None:
            emit_agent_trace(_trace_from_result(message, pending_result, started_at), settings, legacy=legacy)
            return pending_result

        plan = planner(str(message.get("text") or ""), settings, context)
        validate_plan(plan, settings=settings, context=context)
        results = execute_agent_plan(message, plan, legacy=legacy)
        metadata = _compose_reply_metadata(message, plan, results, settings, context, reply_composer)
        result = _runtime_result(
            "agent_runtime_replied",
            metadata["reply"],
            plan,
            results,
            execution_facts=metadata["execution_facts"],
            reply_guard=metadata["reply_guard"],
            reply_guard_fallback_used=bool(metadata["reply_guard_fallback_used"]),
            reply_error_type=str(metadata.get("reply_error_type") or ""),
            latency_ms=int((time.monotonic() - started_at) * 1000),
        )
        result = _apply_v2_gray(message, result, legacy=legacy, settings=settings)
        emit_agent_trace(_trace_from_result(message, result, started_at), settings, legacy=legacy)
        return result
    except Exception as exc:
        emit_agent_trace(
            build_error_trace(message, AGENT_PLANNER_PROMPT_VERSION, started_at, "agent_runtime_error", exc),
            settings,
            legacy=legacy,
        )
        raise


def load_context(
    message: dict[str, str],
    legacy: ModuleType = legacy_app,
    settings: dict[str, str] | None = None,
) -> dict[str, Any]:
    settings = settings or legacy._settings()
    context: dict[str, Any] = {
        "event_type": "feishu_message",
        "today": legacy._today_string(),
        "user_id": message.get("user_id", ""),
        "chat_id": message.get("chat_id", ""),
    }
    task = legacy._load_active_pending_task(message)
    if task:
        context["active_pending"] = task
    memory_context = build_memory_context(message, settings, legacy=legacy)
    if memory_context:
        context["memory_context"] = memory_context
    return context


def validate_plan(
    plan: AgentPlan,
    settings: dict[str, str] | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    settings = settings or {}
    context = context or {}
    max_actions = _int(
        settings.get("agent_max_actions") or os.getenv("AGENT_MAX_ACTIONS") or legacy_app.DEFAULT_SETTINGS["AGENT_MAX_ACTIONS"],
        int(legacy_app.DEFAULT_SETTINGS["AGENT_MAX_ACTIONS"]),
    )
    if len(plan.actions) > max_actions:
        raise PlanValidationError(f"agent plan has {len(plan.actions)} actions, max is {max_actions}")

    for action in plan.actions:
        if action.tool == "clear_daily_intake" and context.get("event_type") != "scheduled_cleanup":
            raise PlanValidationError("clear_daily_intake is not allowed for normal chat messages")
        if action.tool == "undo_intake" and not action.target:
            raise PlanValidationError("undo_intake requires an explicit target")


def _try_handle_active_pending(
    message: dict[str, str],
    context: dict[str, Any],
    settings: dict[str, str],
    legacy: ModuleType,
    started_at: float,
) -> dict[str, Any] | None:
    task = context.get("active_pending") if isinstance(context.get("active_pending"), dict) else {}
    if not task:
        return None
    decision = classify_pending_message(message, task, legacy=legacy)
    if decision.get("route") == "pass_through":
        return None

    handled = handle_pending_decision(message, decision, legacy=legacy)
    result = handled.get("result") if isinstance(handled.get("result"), dict) else {}
    data = {
        "status": handled.get("status", ""),
        "route": handled.get("route", ""),
        "pending_result": result,
        "result": result,
        "pending_task": task,
    }
    plan = AgentPlan.from_dict(
        {
            "goal": "pending",
            "actions": [
                {
                    "tool": "cancel_pending" if handled.get("route") == "cancel" else "create_pending",
                    "params": {"route": handled.get("route", "")},
                    "reason": "active pending handled by runtime",
                }
            ],
            "reply_style": "compact",
        }
    )
    tool_result = ToolResult("pending_decision", str(handled.get("status") or "handled"), data)
    metadata = compose_reply_with_metadata(message, plan, [tool_result], settings, context)
    return _runtime_result(
        f"agent_runtime_pending_{handled.get('status', 'handled')}",
        metadata["reply"],
        plan,
        [tool_result],
        execution_facts=metadata["execution_facts"],
        reply_guard=metadata["reply_guard"],
        reply_guard_fallback_used=bool(metadata["reply_guard_fallback_used"]),
        reply_error_type=str(metadata.get("reply_error_type") or ""),
        latency_ms=int((time.monotonic() - started_at) * 1000),
    )


def _compose_reply_metadata(
    message: dict[str, str],
    plan: AgentPlan,
    results: list[ToolResult],
    settings: dict[str, str],
    context: dict[str, Any],
    reply_composer: ReplyComposer | None,
) -> dict[str, Any]:
    if reply_composer is None or reply_composer is compose_reply:
        return compose_reply_with_metadata(message, plan, results, settings, context)

    reply = reply_composer(message, plan, results, settings, context)
    facts = build_execution_facts(message, plan, results, context=context)
    result_payloads = [result.to_dict() for result in results]
    guard = guard_validate_reply(reply, facts, result_payloads)
    return {
        "reply": guard.safe_reply if not guard.passed and guard.safe_reply else reply,
        "execution_facts": facts,
        "reply_guard": guard.to_dict(),
        "reply_guard_passed": guard.passed,
        "reply_guard_fallback_used": bool(guard.fallback_used),
        "reply_error_type": "",
    }


def _apply_v2_gray(
    message: dict[str, str],
    v1_result: dict[str, Any],
    legacy: ModuleType,
    settings: dict[str, str],
) -> dict[str, Any]:
    gray = evaluate_v2_runtime(message, v1_result, legacy=legacy, settings=settings)
    if gray.takeover_result is not None:
        return attach_v2_gray_result(gray.takeover_result, gray)
    return attach_v2_gray_result(v1_result, gray)


def _runtime_result(
    status: str,
    reply: str,
    plan: AgentPlan,
    results: list[ToolResult],
    execution_facts: dict[str, Any] | None = None,
    reply_guard: dict[str, Any] | None = None,
    reply_guard_fallback_used: bool = False,
    reply_error_type: str = "",
    latency_ms: int = 0,
) -> dict[str, Any]:
    execution_facts = execution_facts or {}
    reply_guard = reply_guard or {}
    return {
        "status": status,
        "reply": reply,
        "parse_result": {
            "intent": "agent_runtime",
            "goal": plan.goal,
            "action_count": len(plan.actions),
            "actions": [action.to_dict() for action in plan.actions],
        },
        "tool_results": [result.to_dict() for result in results],
        "execution_facts": execution_facts,
        "reply_guard": reply_guard,
        "agent_metrics": _agent_metrics(plan, results, reply_guard, reply_guard_fallback_used, reply_error_type, latency_ms, execution_facts),
        **_legacy_log_fields(results),
    }


def _agent_metrics(
    plan: AgentPlan,
    results: list[ToolResult],
    reply_guard: dict[str, Any],
    reply_guard_fallback_used: bool,
    reply_error_type: str,
    latency_ms: int,
    execution_facts: dict[str, Any] | None = None,
) -> dict[str, Any]:
    execution_facts = execution_facts or {}
    failed = [result for result in results if result.status == "error"]
    undo_confidence = [
        action.confidence
        for action in plan.actions
        if action.tool == "undo_intake" and action.confidence is not None
    ]
    memory_facts = execution_facts.get("memory") if isinstance(execution_facts.get("memory"), dict) else {}
    reporting_facts = execution_facts.get("reporting") if isinstance(execution_facts.get("reporting"), dict) else {}
    reminder_facts = execution_facts.get("reminders") if isinstance(execution_facts.get("reminders"), dict) else {}
    reply_guard_violations = reply_guard.get("violations") if isinstance(reply_guard.get("violations"), list) else []
    reply_guard_violation_types = [
        str(item.get("type") or "")
        for item in reply_guard_violations
        if isinstance(item, dict) and item.get("type")
    ]
    return {
        "agent_runtime_used": True,
        "planner_success": True,
        "planner_fallback_used": False,
        "action_count": len(plan.actions),
        "tool_success_count": len(results) - len(failed),
        "tool_failed_count": len(failed),
        "reply_guard_v2_enabled": _truthy_env("AGENT_REPLY_GUARD_V2_ENABLED", "true"),
        "reply_guard_passed": bool(reply_guard.get("passed", True)),
        "reply_guard_fallback_used": reply_guard_fallback_used,
        "reply_guard_violation_count": len(reply_guard_violations),
        "reply_guard_violation_types": reply_guard_violation_types,
        "unsupported_number_count": reply_guard_violation_types.count("unsupported_number"),
        "unsupported_status_count": reply_guard_violation_types.count("unsupported_status"),
        "privacy_risk_blocked": "privacy_risk" in reply_guard_violation_types,
        "reply_error_type": reply_error_type or None,
        "semantic_undo_confidence": max(undo_confidence) if undo_confidence else None,
        "pending_async_preserved": any(result.status in {"pending_created", "created"} and result.tool in {"record_food", "create_pending"} for result in results),
        "agent_eval_version": "agent_eval.v1",
        "agent_runtime_latency_ms": latency_ms or None,
        "memory_enabled": bool(_nested_bool(execution_facts, "context", "memory_context", "enabled")),
        "memory_context_used": bool(_nested_bool(execution_facts, "context", "memory_context", "enabled")),
        "alias_learned": bool(memory_facts.get("alias_learned")),
        "alias_confirmed": bool(memory_facts.get("alias_confirmed")),
        "default_grams_suggested": bool(memory_facts.get("default_grams_suggested")),
        "default_grams_used": bool(memory_facts.get("default_grams_used")),
        "combo_used": bool(memory_facts.get("combo_used")),
        "combo_created": bool(memory_facts.get("combo_created")),
        "preference_updated": bool(memory_facts.get("preference_updated")),
        "memory_write_failed": bool(memory_facts.get("memory_write_failed")),
        "reporting_enabled": bool(reporting_facts.get("enabled")),
        "report_type": reporting_facts.get("latest_report_type") or None,
        "report_cache_hit": bool(reporting_facts.get("cache_hit")),
        "report_generated": bool(reporting_facts.get("generated")),
        "report_period_days": reporting_facts.get("report_period_days"),
        "trend_query_used": bool(reporting_facts.get("trend_query_used")),
        "target_hit_rate": reporting_facts.get("target_hit_rate"),
        "report_reply_guard_passed": bool(reply_guard.get("passed", True)),
        "report_write_failed": bool(reporting_facts.get("write_failed")),
        "reminder_enabled": bool(reminder_facts.get("enabled")),
        "reminder_candidates_count": reminder_facts.get("candidates_count"),
        "reminder_sent_count": reminder_facts.get("sent_count"),
        "reminder_skipped_count": reminder_facts.get("skipped_count"),
        "reminder_skip_reason": reminder_facts.get("skip_reason") or None,
        "anomaly_detected_count": reminder_facts.get("anomaly_detected_count"),
        "pending_reminder_count": reminder_facts.get("pending_reminder_count"),
        "target_gap_reminder_count": reminder_facts.get("target_gap_reminder_count"),
        "daily_summary_sent": bool(reminder_facts.get("daily_summary_sent")),
        "reminder_rule_updated": bool(reminder_facts.get("rule_updated")),
        "reminder_send_failed": bool(reminder_facts.get("send_failed")),
    }


def _nested_bool(payload: dict[str, Any], *keys: str) -> bool:
    current: Any = payload
    for key in keys:
        if not isinstance(current, dict):
            return False
        current = current.get(key)
    return bool(current)


def _truthy_env(key: str, default: str) -> bool:
    return str(os.getenv(key, legacy_app.DEFAULT_SETTINGS.get(key, default))).strip().lower() in {"1", "true", "yes", "on"}


def _trace_from_result(
    message: dict[str, str],
    result: dict[str, Any],
    started_at: float,
) -> AgentTrace:
    plan_json = result.get("parse_result") if isinstance(result.get("parse_result"), dict) else {}
    tool_results = result.get("tool_results") if isinstance(result.get("tool_results"), list) else []
    metrics = result.get("agent_metrics") if isinstance(result.get("agent_metrics"), dict) else {}
    return AgentTrace(
        trace_id=make_trace_id(message),
        message_id=str(message.get("message_id") or ""),
        user_id=str(message.get("user_id") or ""),
        raw_text=str(message.get("text") or ""),
        planner_prompt_version=AGENT_PLANNER_PROMPT_VERSION,
        plan_json=plan_json,
        action_count=int(plan_json.get("action_count") or 0),
        tool_results=_compact_tool_results(tool_results),
        execution_facts=result.get("execution_facts", {}) if isinstance(result.get("execution_facts"), dict) else {},
        final_reply=str(result.get("reply") or ""),
        fallback_used=bool(metrics.get("planner_fallback_used")),
        error_type=str(metrics.get("reply_error_type") or ""),
        latency_ms=int((time.monotonic() - started_at) * 1000),
        reply_guard=result.get("reply_guard", {}) if isinstance(result.get("reply_guard"), dict) else {},
    )


def _compact_tool_results(tool_results: list[Any]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in tool_results:
        if not isinstance(item, dict):
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        nested = data.get("result") if isinstance(data.get("result"), dict) else {}
        source = nested or data
        lookup = source.get("lookup_result") if isinstance(source.get("lookup_result"), dict) else {}
        today = source.get("today_summary") if isinstance(source.get("today_summary"), dict) else {}
        undo = source.get("undo_result") if isinstance(source.get("undo_result"), dict) else {}
        pending = source.get("pending_result") if isinstance(source.get("pending_result"), dict) else data.get("pending_result", {})
        food_match = data.get("food_match") if isinstance(data.get("food_match"), dict) else {}
        report = data.get("report") if isinstance(data.get("report"), dict) else {}
        period = data.get("period") if isinstance(data.get("period"), dict) else report.get("period") if isinstance(report.get("period"), dict) else {}
        compact.append(
            {
                "tool": item.get("tool", ""),
                "status": item.get("status", ""),
                "action_id": item.get("action_id", ""),
                "error": item.get("error", ""),
                "food_match_status": _nested(data, "food_match", "status"),
                "food_match_query": food_match.get("query") if isinstance(food_match, dict) else "",
                "food_match_candidate": _compact_food_candidate(food_match.get("candidate") if isinstance(food_match.get("candidate"), dict) else {}),
                "food_match_candidates": [
                    _compact_food_candidate(candidate)
                    for candidate in (food_match.get("candidates") if isinstance(food_match.get("candidates"), list) else [])[:5]
                    if isinstance(candidate, dict)
                ],
                "items": _lookup_items(lookup),
                "missing": lookup.get("missing") if isinstance(lookup.get("missing"), list) else [],
                "match_diagnostics": lookup.get("match_diagnostics") if isinstance(lookup.get("match_diagnostics"), list) else [],
                "today_count": today.get("count"),
                "today_totals": today.get("totals") if isinstance(today.get("totals"), dict) else {},
                "undo_status": undo.get("status"),
                "undo_deleted_count": undo.get("deleted_count"),
                "pending_status": pending.get("status") if isinstance(pending, dict) else "",
                "pending_food": pending.get("food_name") if isinstance(pending, dict) else "",
                "report_type": data.get("report_type") or report.get("report_type") or "",
                "report_status": item.get("status") if item.get("tool") in {"generate_daily_report", "generate_weekly_report", "generate_monthly_report", "query_trend", "compare_period", "analyze_target_gap", "get_report", "regenerate_report"} else "",
                "report_period": period,
            }
        )
    return compact


def _compact_food_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    food = candidate.get("food") if isinstance(candidate.get("food"), dict) else {}
    return {
        "food_name": str(food.get("name") or ""),
        "food_id": str(food.get("food_id") or ""),
        "score": candidate.get("score"),
        "reason": str(candidate.get("reason") or ""),
        "matched_key": str(candidate.get("matched_key") or ""),
    }


def _lookup_items(lookup: dict[str, Any]) -> list[dict[str, Any]]:
    items = lookup.get("items") if isinstance(lookup.get("items"), list) else []
    return [
        {
            "food_name": str(item.get("matched_name") or item.get("input_name") or ""),
            "grams": item.get("grams"),
            "nutrition": item.get("nutrition") if isinstance(item.get("nutrition"), dict) else {},
        }
        for item in items
        if isinstance(item, dict)
    ]


def _nested(payload: dict[str, Any], key: str, nested_key: str) -> Any:
    value = payload.get(key)
    return value.get(nested_key) if isinstance(value, dict) else None


def _legacy_log_fields(results: list[ToolResult]) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "lookup_result": {},
        "intake_write_result": {},
        "today_summary": {},
        "daily_standard": {},
        "undo_result": {},
        "standard_write_result": {},
        "pending_task": {},
        "pending_result": {},
    }
    aggregate_lookup: dict[str, Any] = {
        "items": [],
        "missing": [],
        "totals": {"carbs": 0.0, "protein": 0.0, "fat": 0.0, "kcal": 0.0},
    }
    created_count = 0
    duplicate_skipped = False
    for result in results:
        data = result.data if isinstance(result.data, dict) else {}
        nested = data.get("result") if isinstance(data.get("result"), dict) else {}
        source = nested or data

        lookup = source.get("lookup_result") if isinstance(source.get("lookup_result"), dict) else {}
        if lookup:
            aggregate_lookup["items"].extend(lookup.get("items") if isinstance(lookup.get("items"), list) else [])
            aggregate_lookup["missing"].extend(lookup.get("missing") if isinstance(lookup.get("missing"), list) else [])
            totals = lookup.get("totals") if isinstance(lookup.get("totals"), dict) else {}
            for key in ("carbs", "protein", "fat", "kcal"):
                try:
                    aggregate_lookup["totals"][key] += float(totals.get(key) or 0)
                except (TypeError, ValueError):
                    pass

        write = source.get("intake_write_result") if isinstance(source.get("intake_write_result"), dict) else {}
        if write:
            duplicate_skipped = duplicate_skipped or write.get("status") == "duplicate_skipped"
            try:
                created_count += int(write.get("created_count") or 0)
            except (TypeError, ValueError):
                pass

        _copy_dict(fields, "intake_write_result", source)
        _copy_dict(fields, "today_summary", source, overwrite=True)
        _copy_dict(fields, "daily_standard", source, overwrite=True)
        _copy_dict(fields, "undo_result", source)
        _copy_dict(fields, "standard_write_result", source)
        _copy_dict(fields, "pending_task", source)
        _copy_dict(fields, "pending_result", source)

        if not fields["pending_result"] and isinstance(data.get("pending_result"), dict):
            fields["pending_result"] = data["pending_result"]
        if not fields["pending_task"] and isinstance(data.get("pending_task"), dict):
            fields["pending_task"] = data["pending_task"]
    if aggregate_lookup["items"] or aggregate_lookup["missing"]:
        fields["lookup_result"] = aggregate_lookup
    if created_count or duplicate_skipped:
        fields["intake_write_result"] = {
            "status": "duplicate_skipped" if duplicate_skipped and not created_count else "created",
            "created_count": created_count,
        }
    return fields


def _copy_dict(target: dict[str, Any], key: str, source: dict[str, Any], overwrite: bool = False) -> None:
    if target.get(key) and not overwrite:
        return
    value = source.get(key)
    if isinstance(value, dict):
        target[key] = value


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
