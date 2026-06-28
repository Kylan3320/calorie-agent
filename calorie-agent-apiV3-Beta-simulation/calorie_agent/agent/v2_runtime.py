from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

from calorie_agent.context import ContextOptions

from .dynamic_planner import HttpJsonPost, plan_shadow_message
from .reply_composer import compose_reply_with_metadata
from .schemas import AgentAction as V1AgentAction
from .schemas import AgentPlan as V1AgentPlan
from .schemas import ToolResult
from .tools import execute_agent_plan
from .v2_schemas import AgentAction as V2AgentAction
from .v2_schemas import AgentPlan as V2AgentPlan
from .v2_schemas import PlannerResult
from .. import legacy_app
from ..tools.intake_query_tools import query_intake_food_nutrition_from_legacy


DEFAULT_ALLOWED_READ_ONLY_TOOLS = {
    "query_intake_food_nutrition",
    "query_today",
    "generate_daily_report",
    "generate_weekly_report",
    "query_trend",
    "compare_period",
}

EXECUTABLE_READ_ONLY_TOOLS = {
    "query_intake_food_nutrition",
    "query_today",
    "search_food",
    "list_reminder_rules",
    "compare_period",
}

READ_ONLY_TOOLS = {
    "query_intake_food_nutrition",
    "query_today",
    "search_food",
    "generate_daily_report",
    "generate_weekly_report",
    "generate_monthly_report",
    "query_trend",
    "compare_period",
    "analyze_target_gap",
    "get_report",
    "list_reminder_rules",
}

DESTRUCTIVE_TOOLS = {"undo_intake", "clear_daily_intake", "delete_memory"}


@dataclass(frozen=True)
class ActionModeDecision:
    action_id: str
    tool: str
    safety: str
    allowed: bool
    executable: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "tool": self.tool,
            "safety": self.safety,
            "allowed": self.allowed,
            "executable": self.executable,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class V2GrayResult:
    enabled: bool
    shadow_enabled: bool
    read_only_mode: bool
    takeover_result: dict[str, Any] | None = None
    shadow: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)


def evaluate_v2_runtime(
    message: dict[str, str],
    v1_result: dict[str, Any],
    *,
    legacy: ModuleType = legacy_app,
    settings: dict[str, str] | None = None,
    http_json_post: HttpJsonPost | None = None,
) -> V2GrayResult:
    settings = settings or legacy._settings()
    enabled = _flag(settings, "AGENT_RUNTIME_V2_ENABLED", False)
    shadow_enabled = _flag(settings, "AGENT_RUNTIME_V2_SHADOW", False)
    read_only_mode = _flag(settings, "AGENT_RUNTIME_V2_READ_ONLY_TOOLS", False)
    fallback_to_v1 = _flag(settings, "AGENT_RUNTIME_V2_FALLBACK_TO_V1", True)
    compare_with_v1 = _flag(settings, "AGENT_RUNTIME_V2_COMPARE_WITH_V1", True)

    base_metrics = {
        "v2_enabled": enabled,
        "v2_shadow": shadow_enabled,
        "v2_read_only_mode": read_only_mode,
        "v2_plan_success": False,
        "v2_plan_failed": False,
        "v2_tool_executed_count": 0,
        "v2_tool_blocked_count": 0,
        "v2_fallback_to_v1": False,
        "v2_reply_guard_fallback": False,
        "v2_shadow_diff_count": 0,
        "v2_latency_ms": None,
    }
    if not enabled and not shadow_enabled:
        return V2GrayResult(enabled, shadow_enabled, read_only_mode, metrics=base_metrics)
    if not _sampled_in(message, settings):
        metrics = {**base_metrics, "v2_sampled": False}
        return V2GrayResult(enabled, shadow_enabled, read_only_mode, metrics=metrics)

    started_at = time.monotonic()
    planner_result = plan_shadow_message(
        message,
        context_options=_context_options(settings, legacy=legacy),
        settings=settings,
        http_json_post=http_json_post,
    )
    latency_ms = int((time.monotonic() - started_at) * 1000)
    plan = planner_result.plan
    decisions = classify_v2_actions(plan, settings) if plan is not None else []
    diff = compare_v1_v2(v1_result, plan) if compare_with_v1 else {}
    shadow = _shadow_payload(
        planner_result,
        decisions,
        diff,
        v1_result,
        enabled=enabled,
        shadow_enabled=shadow_enabled,
        latency_ms=latency_ms,
    )
    metrics = {
        **base_metrics,
        "v2_sampled": True,
        "v2_plan_success": planner_result.status == "success",
        "v2_plan_failed": planner_result.status != "success",
        "v2_tool_blocked_count": len([decision for decision in decisions if not decision.allowed or not decision.executable]),
        "v2_shadow_diff_count": 0 if diff.get("same_goal", True) else 1,
        "v2_latency_ms": latency_ms,
        "v2_invalid_tool": planner_result.status == "invalid_tool",
    }

    if not enabled:
        return V2GrayResult(enabled, shadow_enabled, read_only_mode, shadow=shadow, metrics=metrics)
    if planner_result.status != "success" or plan is None:
        metrics["v2_fallback_to_v1"] = fallback_to_v1
        return V2GrayResult(enabled, shadow_enabled, read_only_mode, shadow=shadow, metrics=metrics)
    if not read_only_mode:
        metrics["v2_fallback_to_v1"] = fallback_to_v1
        shadow["v2_blocked_by_mode"] = _blocked_payload(decisions, extra_reason="read_only_mode_disabled")
        metrics["v2_tool_blocked_count"] = len(shadow["v2_blocked_by_mode"])
        return V2GrayResult(enabled, shadow_enabled, read_only_mode, shadow=shadow, metrics=metrics)
    if not plan.actions:
        metrics["v2_fallback_to_v1"] = fallback_to_v1
        return V2GrayResult(enabled, shadow_enabled, read_only_mode, shadow=shadow, metrics=metrics)

    blocked = [decision for decision in decisions if not decision.allowed or not decision.executable]
    if blocked:
        metrics["v2_fallback_to_v1"] = fallback_to_v1
        shadow["v2_blocked_by_mode"] = _blocked_payload(blocked)
        metrics["v2_tool_blocked_count"] = len(blocked)
        return V2GrayResult(enabled, shadow_enabled, read_only_mode, shadow=shadow, metrics=metrics)

    takeover = _execute_read_only_takeover(message, plan, settings, legacy=legacy, latency_ms=latency_ms, shadow=shadow)
    takeover_metrics = takeover.get("agent_metrics") if isinstance(takeover.get("agent_metrics"), dict) else {}
    shadow = {**shadow, "v2_reply_guard_passed": takeover_metrics.get("reply_guard_passed")}
    metrics.update(takeover_metrics)
    metrics["v2_tool_executed_count"] = len(takeover.get("tool_results", []) if isinstance(takeover.get("tool_results"), list) else [])
    metrics["v2_reply_guard_fallback"] = bool(takeover_metrics.get("reply_guard_fallback_used"))
    if not takeover_metrics.get("reply_guard_passed", True):
        metrics["v2_fallback_to_v1"] = fallback_to_v1
        return V2GrayResult(enabled, shadow_enabled, read_only_mode, shadow=shadow, metrics=metrics)

    return V2GrayResult(enabled, shadow_enabled, read_only_mode, takeover_result=takeover, shadow=shadow, metrics=metrics)


def attach_v2_gray_result(base_result: dict[str, Any], gray: V2GrayResult) -> dict[str, Any]:
    if not gray.shadow and not gray.metrics:
        return base_result
    result = dict(base_result)
    facts = dict(result.get("execution_facts") if isinstance(result.get("execution_facts"), dict) else {})
    if gray.shadow:
        facts["v2_shadow"] = gray.shadow
    result["execution_facts"] = facts
    metrics = dict(result.get("agent_metrics") if isinstance(result.get("agent_metrics"), dict) else {})
    metrics.update(gray.metrics)
    result["agent_metrics"] = metrics
    if gray.shadow:
        result["v2_shadow"] = gray.shadow
    return result


def compare_v1_v2(v1_result: dict[str, Any] | None, v2_plan: V2AgentPlan | None) -> dict[str, Any]:
    v1_result = v1_result or {}
    parse_result = v1_result.get("parse_result") if isinstance(v1_result.get("parse_result"), dict) else {}
    v1_goal = str(parse_result.get("goal") or parse_result.get("intent") or "").strip()
    v2_goal = str(v2_plan.goal if v2_plan is not None else "").strip()
    v1_tools = [
        str(action.get("tool") or "")
        for action in parse_result.get("actions", [])
        if isinstance(action, dict) and action.get("tool")
    ]
    v2_tools = [action.tool for action in v2_plan.actions] if v2_plan is not None else []
    same_goal = bool(v1_goal and v2_goal and _goal_bucket(v1_goal) == _goal_bucket(v2_goal))
    if not v1_goal or not v2_goal:
        same_goal = False
    risk_level = "low"
    if any(tool in DESTRUCTIVE_TOOLS for tool in v2_tools):
        risk_level = "high"
    elif any(tool not in READ_ONLY_TOOLS for tool in v2_tools):
        risk_level = "medium"
    notes: list[str] = []
    if v1_tools and v2_tools and set(v1_tools) != set(v2_tools):
        notes.append("tool_set_differs")
    if not same_goal:
        notes.append("goal_differs")
    return {
        "same_goal": same_goal,
        "v1_intent": v1_goal,
        "v2_goal": v2_goal,
        "v1_tools": v1_tools,
        "v2_tools": v2_tools,
        "risk_level": risk_level,
        "notes": notes,
    }


def classify_v2_actions(plan: V2AgentPlan | None, settings: dict[str, str] | None = None) -> list[ActionModeDecision]:
    if plan is None:
        return []
    settings = settings or {}
    allowed_tools = _allowed_tools(settings)
    write_enabled = _flag(settings, "AGENT_RUNTIME_V2_WRITE_TOOLS", False)
    destructive_enabled = _flag(settings, "AGENT_RUNTIME_V2_DESTRUCTIVE_TOOLS", False)
    decisions: list[ActionModeDecision] = []
    for action in plan.actions:
        allowed = True
        executable = True
        reason = ""
        if action.tool in DESTRUCTIVE_TOOLS or action.safety == "destructive":
            allowed = destructive_enabled
            executable = False
            reason = "destructive_disabled" if not destructive_enabled else "destructive_executor_not_connected"
        elif action.safety in {"write", "permission_sensitive"} or action.tool not in READ_ONLY_TOOLS:
            allowed = write_enabled
            executable = False
            reason = "write_disabled" if not write_enabled else "write_executor_not_connected"
        elif action.tool not in allowed_tools:
            allowed = False
            executable = False
            reason = "tool_not_allowed"
        elif action.tool not in EXECUTABLE_READ_ONLY_TOOLS:
            executable = False
            reason = "read_only_executor_not_connected"
        decisions.append(
            ActionModeDecision(
                action_id=action.action_id,
                tool=action.tool,
                safety=action.safety,
                allowed=allowed,
                executable=executable,
                reason=reason,
            )
        )
    return decisions


def _execute_read_only_takeover(
    message: dict[str, str],
    plan: V2AgentPlan,
    settings: dict[str, str],
    *,
    legacy: ModuleType,
    latency_ms: int,
    shadow: dict[str, Any],
) -> dict[str, Any]:
    results = [_execute_read_only_action(message, action, index, legacy) for index, action in enumerate(plan.actions)]
    reply_plan = _v1_plan_from_v2(plan)
    metadata = compose_reply_with_metadata(message, reply_plan, results, settings, _reply_context(message, legacy))
    metrics = _takeover_metrics(plan, results, metadata, latency_ms)
    facts = dict(metadata["execution_facts"])
    facts["v2_shadow"] = shadow
    return {
        "status": "agent_runtime_v2_read_only_replied",
        "reply": metadata["reply"],
        "parse_result": {
            "intent": "agent_runtime_v2",
            "goal": plan.goal,
            "schema_version": plan.schema_version,
            "action_count": len(plan.actions),
            "actions": [action.to_dict() for action in plan.actions],
        },
        "tool_results": [result.to_dict() for result in results],
        "execution_facts": facts,
        "reply_guard": metadata["reply_guard"],
        "agent_metrics": metrics,
        **_legacy_log_fields(results),
    }


def _execute_read_only_action(
    message: dict[str, str],
    action: V2AgentAction,
    index: int,
    legacy: ModuleType,
) -> ToolResult:
    try:
        if action.tool == "query_intake_food_nutrition":
            data = query_intake_food_nutrition_from_legacy(message, action.parameters, legacy=legacy)
            return ToolResult(action.tool, str(data.get("status") or "ok"), data, action_id=action.action_id)
        plan = V1AgentPlan(goal=action.tool, actions=[_v1_action_from_v2(action)])
        return execute_agent_plan(message, plan, legacy=legacy)[0]
    except Exception as exc:
        return ToolResult(action.tool, "error", error=str(exc), action_id=action.action_id)


def _v1_plan_from_v2(plan: V2AgentPlan) -> V1AgentPlan:
    return V1AgentPlan(
        goal=plan.goal,
        actions=[_v1_action_from_v2(action) for action in plan.actions],
        reply_style="compact",
        needs_confirmation=plan.needs_confirmation,
        confidence=plan.confidence,
        notes=plan.planner_notes or "",
        raw=plan.to_dict(),
    )


def _v1_action_from_v2(action: V2AgentAction) -> V1AgentAction:
    params = dict(action.parameters)
    target = params.pop("target", {}) if isinstance(params.get("target"), dict) else {}
    return V1AgentAction(
        tool=action.tool,
        params=params,
        target=target,
        action_id=action.action_id,
        reason=action.reason,
        confidence=None,
    )


def _takeover_metrics(
    plan: V2AgentPlan,
    results: list[ToolResult],
    metadata: dict[str, Any],
    latency_ms: int,
) -> dict[str, Any]:
    failed = [result for result in results if result.status == "error"]
    reply_guard = metadata.get("reply_guard") if isinstance(metadata.get("reply_guard"), dict) else {}
    violations = reply_guard.get("violations") if isinstance(reply_guard.get("violations"), list) else []
    violation_types = [
        str(item.get("type") or "")
        for item in violations
        if isinstance(item, dict) and item.get("type")
    ]
    return {
        "agent_runtime_used": True,
        "planner_success": True,
        "planner_fallback_used": False,
        "action_count": len(plan.actions),
        "tool_success_count": len(results) - len(failed),
        "tool_failed_count": len(failed),
        "reply_guard_v2_enabled": _flag({}, "AGENT_REPLY_GUARD_V2_ENABLED", True),
        "reply_guard_passed": bool(reply_guard.get("passed", True)),
        "reply_guard_fallback_used": bool(metadata.get("reply_guard_fallback_used")),
        "reply_guard_violation_count": len(violations),
        "reply_guard_violation_types": violation_types,
        "reply_error_type": metadata.get("reply_error_type") or None,
        "agent_eval_version": "agent_eval.v2.8",
        "agent_runtime_latency_ms": latency_ms or None,
        "v2_enabled": True,
        "v2_read_only_mode": True,
        "v2_plan_success": True,
        "v2_plan_failed": False,
        "v2_tool_executed_count": len(results),
        "v2_tool_blocked_count": 0,
        "v2_fallback_to_v1": False,
        "v2_reply_guard_fallback": bool(metadata.get("reply_guard_fallback_used")),
        "v2_latency_ms": latency_ms or None,
    }


def _shadow_payload(
    planner_result: PlannerResult,
    decisions: list[ActionModeDecision],
    diff: dict[str, Any],
    v1_result: dict[str, Any],
    *,
    enabled: bool,
    shadow_enabled: bool,
    latency_ms: int,
) -> dict[str, Any]:
    plan = planner_result.plan
    allowed = [decision.tool for decision in decisions if decision.allowed and decision.executable]
    blocked = [decision for decision in decisions if not decision.allowed or not decision.executable]
    return {
        "v2_enabled": enabled,
        "v2_shadow_enabled": shadow_enabled,
        "v2_plan_generated": plan is not None,
        "v2_plan_status": planner_result.status,
        "v2_plan_errors": list(planner_result.errors),
        "v2_plan_warnings": list(planner_result.warnings),
        "v2_used_skills": list(plan.used_skills if plan is not None else planner_result.loaded_skill_names),
        "v2_action_count": len(plan.actions) if plan is not None else 0,
        "v2_invalid_tool": planner_result.status == "invalid_tool",
        "v2_reply_guard_passed": None,
        "v2_would_execute_tools": allowed,
        "v2_blocked_by_mode": _blocked_payload(blocked),
        "v1_intent": _v1_intent(v1_result),
        "v2_goal": plan.goal if plan is not None else "",
        "v1_v2_diff_summary": diff,
        "v2_latency_ms": latency_ms,
    }


def _blocked_payload(decisions: list[ActionModeDecision], extra_reason: str = "") -> list[dict[str, Any]]:
    payload = []
    for decision in decisions:
        item = decision.to_dict()
        if extra_reason and not item.get("reason"):
            item["reason"] = extra_reason
        payload.append(item)
    return payload


def _context_options(settings: dict[str, str], *, legacy: ModuleType) -> ContextOptions:
    return ContextOptions(
        max_skills=_int_setting(settings, "AGENT_RUNTIME_V2_MAX_SKILLS", 5),
        context_budget_chars=_int_setting(settings, "AGENT_RUNTIME_V2_CONTEXT_BUDGET_CHARS", 8000),
        include_short_term=_flag(settings, "AGENT_RUNTIME_V2_INCLUDE_SHORT_TERM", True),
        include_memory=_flag(settings, "AGENT_RUNTIME_V2_INCLUDE_MEMORY", True),
        include_retrieved_facts=_flag(settings, "AGENT_RUNTIME_V2_INCLUDE_RETRIEVED_FACTS", True),
        read_only=True,
        skill_dir=_value(settings, "AGENT_RUNTIME_V2_SKILL_DIR", "calorie_agent/skills"),
        current_date=legacy._today_string(),
    )


def _reply_context(message: dict[str, str], legacy: ModuleType) -> dict[str, Any]:
    return {
        "event_type": "feishu_message",
        "today": legacy._today_string(),
        "user_id": message.get("user_id", ""),
        "chat_id": message.get("chat_id", ""),
    }


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
    for result in results:
        data = result.data if isinstance(result.data, dict) else {}
        if result.tool == "query_today":
            fields["today_summary"] = data.get("today_summary") if isinstance(data.get("today_summary"), dict) else {}
            fields["daily_standard"] = data.get("daily_standard") if isinstance(data.get("daily_standard"), dict) else {}
        if result.tool == "query_intake_food_nutrition":
            fields["lookup_result"] = data
    return fields


def _allowed_tools(settings: dict[str, str]) -> set[str]:
    raw = _value(settings, "AGENT_RUNTIME_V2_ALLOWED_TOOLS", "")
    if not raw:
        return set(DEFAULT_ALLOWED_READ_ONLY_TOOLS)
    return {item.strip() for item in raw.split(",") if item.strip()}


def _sampled_in(message: dict[str, str], settings: dict[str, str]) -> bool:
    rate = _float_setting(settings, "AGENT_RUNTIME_V2_SAMPLE_RATE", 1.0)
    if rate >= 1.0:
        return True
    if rate <= 0:
        return False
    key = str(message.get("message_id") or message.get("chat_id") or message.get("text") or "")
    digest = hashlib.sha256(f"calorie-agent:v2:{key}".encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) / 0xFFFFFFFF
    return bucket <= rate


def _goal_bucket(value: str) -> str:
    text = value.strip().lower()
    if "query" in text or "report" in text or "trend" in text:
        return "read"
    if "record" in text or "add_food" in text:
        return "write"
    if "undo" in text or "delete" in text:
        return "destructive"
    return text


def _v1_intent(v1_result: dict[str, Any]) -> str:
    parse_result = v1_result.get("parse_result") if isinstance(v1_result.get("parse_result"), dict) else {}
    return str(parse_result.get("goal") or parse_result.get("intent") or "")


def _flag(settings: dict[str, str], key: str, default: bool) -> bool:
    value = _value(settings, key, str(default).lower())
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _int_setting(settings: dict[str, str], key: str, default: int) -> int:
    try:
        return int(_value(settings, key, str(default)))
    except ValueError:
        return default


def _float_setting(settings: dict[str, str], key: str, default: float) -> float:
    try:
        return float(_value(settings, key, str(default)))
    except ValueError:
        return default


def _value(settings: dict[str, str], key: str, default: str) -> str:
    lower_key = key.lower()
    normalized_key = lower_key[6:] if lower_key.startswith("agent_") else lower_key
    return (
        str(settings.get(lower_key) or settings.get(normalized_key) or settings.get(key) or "")
        or os.getenv(key)
        or legacy_app.DEFAULT_SETTINGS.get(key, default)
    )
