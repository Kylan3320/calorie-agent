from __future__ import annotations

from collections.abc import Callable
import json
import os
import urllib.error
import urllib.request
from typing import Any

from calorie_agent.context import ContextOptions, DynamicContext, load_dynamic_context

from .v2_prompts import V2_PLANNER_PROMPT_VERSION, build_v2_planner_messages
from .v2_schemas import AgentPlan, PlannerResult, V2_SUPPORTED_TOOLS, V2PlanSchemaError


HttpJsonPost = Callable[[str, dict[str, Any], dict[str, str] | None], dict[str, Any]]

DEFAULT_DEEPSEEK_API_BASE = "https://api.deepseek.com"
DEFAULT_DEEPSEEK_MODEL = "deepseek-v4-flash"
DEFAULT_MAX_ACTIONS = 8

READ_ONLY_TOOLS = {
    "query_today",
    "search_food",
    "generate_daily_report",
    "generate_weekly_report",
    "generate_monthly_report",
    "query_trend",
    "compare_period",
    "analyze_target_gap",
    "get_report",
    "query_intake_food_nutrition",
    "list_reminder_rules",
}

DESTRUCTIVE_TOOLS = {"undo_intake"}
NUTRITION_FACT_KEYS = {
    "kcal",
    "calorie",
    "calories",
    "carbs",
    "carbs_g",
    "protein",
    "protein_g",
    "fat",
    "fat_g",
}
WRITE_FLAG_KEYS = {"write", "delete", "update", "create", "upsert", "hard_delete"}


def plan_shadow_message(
    message: dict[str, Any],
    context_options: ContextOptions | None = None,
    settings: dict[str, str] | None = None,
    http_json_post: HttpJsonPost | None = None,
) -> PlannerResult:
    dynamic_context = load_dynamic_context(message, context_options)
    return plan_with_dynamic_context(
        message,
        dynamic_context,
        settings=settings,
        http_json_post=http_json_post,
    )


def plan_with_dynamic_context(
    message: dict[str, Any],
    dynamic_context: DynamicContext,
    settings: dict[str, str] | None = None,
    http_json_post: HttpJsonPost | None = None,
) -> PlannerResult:
    settings = settings or {}
    loaded_skill_names = _loaded_skill_names(dynamic_context)
    api_key = settings.get("deepseek_api_key") or os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        return PlannerResult(
            status="fallback",
            raw_response="",
            errors=["deepseek_api_key_not_configured"],
            loaded_skill_names=loaded_skill_names,
        )

    post = http_json_post or default_http_json_post
    payload = {
        "model": settings.get("deepseek_model") or os.getenv("DEEPSEEK_MODEL") or DEFAULT_DEEPSEEK_MODEL,
        "messages": build_v2_planner_messages(dynamic_context),
        "temperature": 0,
        "max_tokens": 1200,
        "stream": False,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
    }
    api_base = (
        settings.get("deepseek_api_base") or os.getenv("DEEPSEEK_API_BASE") or DEFAULT_DEEPSEEK_API_BASE
    ).rstrip("/")

    try:
        data = post(f"{api_base}/chat/completions", payload, {"Authorization": f"Bearer {api_key}"})
        raw_response = _extract_deepseek_content(data)
    except Exception as exc:
        return PlannerResult(
            status="fallback",
            raw_response="",
            errors=[f"planner_call_failed:{type(exc).__name__}"],
            loaded_skill_names=loaded_skill_names,
        )

    parsed = _parse_json_object(raw_response)
    if parsed is None:
        return PlannerResult(
            status="invalid_json",
            raw_response=raw_response,
            errors=["invalid_json"],
            loaded_skill_names=loaded_skill_names,
        )

    try:
        plan = AgentPlan.from_dict(parsed)
    except V2PlanSchemaError as exc:
        return PlannerResult(
            status="validation_failed",
            raw_response=raw_response,
            errors=[str(exc)],
            loaded_skill_names=loaded_skill_names,
        )

    errors, warnings = _validate_agent_plan_detailed(plan, dynamic_context, settings=settings)
    if errors:
        status = "invalid_tool" if any(error.startswith(("invalid_tool", "tool_not_available")) for error in errors) else "validation_failed"
        return PlannerResult(
            status=status,
            plan=plan,
            raw_response=raw_response,
            errors=errors,
            warnings=warnings,
            loaded_skill_names=loaded_skill_names,
        )

    return PlannerResult(
        status="success",
        plan=plan,
        raw_response=raw_response,
        warnings=warnings,
        loaded_skill_names=loaded_skill_names,
    )


def validate_agent_plan(
    plan: AgentPlan | dict[str, Any],
    context: DynamicContext | dict[str, Any],
    settings: dict[str, str] | None = None,
) -> list[str]:
    parsed = plan if isinstance(plan, AgentPlan) else AgentPlan.from_dict(plan)
    errors, _warnings = _validate_agent_plan_detailed(parsed, context, settings=settings)
    return errors


def default_http_json_post(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} POST {url}: {body}") from exc
    parsed = json.loads(raw.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def _validate_agent_plan_detailed(
    plan: AgentPlan,
    context: DynamicContext | dict[str, Any],
    settings: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    settings = settings or {}
    errors: list[str] = []
    warnings: list[str] = []
    available_tools = set(_available_tools(context))
    loaded_skill_names = set(_loaded_skill_names(context))
    max_actions = _max_actions(settings)
    has_create_pending = any(action.tool == "create_pending" for action in plan.actions)

    if not isinstance(plan.actions, list):
        errors.append("actions_must_be_list")
    if len(plan.actions) > max_actions:
        errors.append(f"too_many_actions:{len(plan.actions)}>{max_actions}")

    for skill_name in plan.used_skills:
        if skill_name not in loaded_skill_names:
            errors.append(f"unknown_used_skill:{skill_name}")

    for action in plan.actions:
        if action.tool not in V2_SUPPORTED_TOOLS:
            errors.append(f"invalid_tool:{action.tool}")
            continue
        if available_tools and action.tool not in available_tools:
            errors.append(f"tool_not_available:{action.tool}")
            continue

        if action.safety == "read_only":
            bad_write_keys = _truthy_keys(action.parameters, WRITE_FLAG_KEYS)
            if bad_write_keys:
                errors.append(f"read_only_action_has_write_flags:{action.action_id}:{','.join(bad_write_keys)}")

        if action.tool in DESTRUCTIVE_TOOLS:
            if not action.reason:
                errors.append(f"destructive_action_missing_reason:{action.action_id}")
            if not _has_destructive_target(action.parameters):
                errors.append(f"destructive_action_missing_target:{action.action_id}")

        if action.tool == "query_intake_food_nutrition" and not _has_any(
            action.parameters,
            ("food_query", "record_id", "record_reference", "record_group", "target"),
        ):
            errors.append(f"query_intake_food_nutrition_missing_reference:{action.action_id}")

        if action.tool == "record_food":
            if not _has_any(action.parameters, ("food_query", "food_name")):
                errors.append(f"record_food_missing_food_query:{action.action_id}")
            if not _has_any(action.parameters, ("grams", "amount_grams")) and not (plan.needs_confirmation or has_create_pending):
                errors.append(f"record_food_missing_grams_without_confirmation:{action.action_id}")

        bad_nutrition_keys = _nutrition_fact_keys(action.parameters, action.tool)
        if bad_nutrition_keys:
            errors.append(f"planner_returned_nutrition_facts:{action.action_id}:{','.join(bad_nutrition_keys)}")

    if not plan.used_skills and loaded_skill_names:
        warnings.append("used_skills_empty")

    return errors, warnings


def _parse_json_object(content: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(content[start : end + 1])
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _extract_deepseek_content(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError("DeepSeek response does not include choices")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise RuntimeError("DeepSeek choice does not include message")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("DeepSeek message content is empty")
    return content.strip()


def _available_tools(context: DynamicContext | dict[str, Any]) -> list[str]:
    if hasattr(context, "available_tools"):
        return list(getattr(context, "available_tools") or [])
    value = context.get("available_tools") if isinstance(context, dict) else []
    return [str(item) for item in value] if isinstance(value, list) else []


def _loaded_skill_names(context: DynamicContext | dict[str, Any]) -> list[str]:
    loaded = getattr(context, "loaded_skills", None)
    if loaded is None and isinstance(context, dict):
        loaded = context.get("loaded_skills", [])
    names: list[str] = []
    for skill in loaded or []:
        if isinstance(skill, dict):
            name = skill.get("name")
        else:
            name = getattr(skill, "name", "")
        if name:
            names.append(str(name))
    return names


def _max_actions(settings: dict[str, str]) -> int:
    value = settings.get("agent_max_actions") or os.getenv("AGENT_MAX_ACTIONS") or str(DEFAULT_MAX_ACTIONS)
    try:
        return max(1, int(value))
    except ValueError:
        return DEFAULT_MAX_ACTIONS


def _has_any(payload: dict[str, Any], keys: tuple[str, ...]) -> bool:
    return any(payload.get(key) not in (None, "", []) for key in keys)


def _has_destructive_target(parameters: dict[str, Any]) -> bool:
    if _has_any(parameters, ("mode", "record_id", "food_query", "ordinal", "target")):
        return True
    target = parameters.get("target")
    return isinstance(target, dict) and bool(target)


def _truthy_keys(payload: dict[str, Any], keys: set[str]) -> list[str]:
    found: list[str] = []
    for key, value in _walk_items(payload):
        if key in keys and value not in (False, None, "", 0):
            found.append(key)
    return sorted(set(found))


def _nutrition_fact_keys(payload: dict[str, Any], tool: str) -> list[str]:
    found: list[str] = []
    for key, value in _walk_items(payload):
        if key not in NUTRITION_FACT_KEYS:
            continue
        if tool == "change_standard" and key in {"carbs", "protein", "fat"}:
            continue
        if value not in (None, "", []):
            found.append(key)
    return sorted(set(found))


def _walk_items(value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        items: list[tuple[str, Any]] = []
        for key, item in value.items():
            key_text = str(key).lower()
            items.append((key_text, item))
            items.extend(_walk_items(item))
        return items
    if isinstance(value, list):
        items = []
        for item in value:
            items.extend(_walk_items(item))
        return items
    return []
