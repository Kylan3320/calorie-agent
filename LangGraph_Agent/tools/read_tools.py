from __future__ import annotations

from types import ModuleType
from typing import Any

from ... import legacy_app
from ...domain import reporting
from ...domain.food_match import choose_food_match
from ...tools.intake_query_tools import (
    format_intake_food_nutrition_reply,
    query_intake_food_nutrition_from_legacy,
)
from ..schemas import AgentAction, ToolResult


READONLY_ALLOWED_TOOLS = {
    "query_today",
    "query_food_nutrition",
    "generate_daily_report",
}


def execute_readonly_tool(
    action: AgentAction,
    message: dict[str, str],
    *,
    legacy: ModuleType = legacy_app,
    allowed_tools: set[str] | None = None,
) -> ToolResult:
    allowed = allowed_tools or READONLY_ALLOWED_TOOLS
    if action.tool not in allowed:
        return ToolResult(action.tool, "blocked_not_readonly_allowed", error="tool_not_allowed", action_id=action.action_id)
    try:
        if action.tool == "query_today":
            return _query_today(action, message, legacy)
        if action.tool == "query_food_nutrition":
            return _query_food_nutrition(action, message, legacy)
        if action.tool == "generate_daily_report":
            return _generate_daily_report(action, message, legacy)
    except Exception as exc:
        return ToolResult(action.tool, "error", error=f"{type(exc).__name__}:{exc}", action_id=action.action_id)
    return ToolResult(action.tool, "error", error=f"unsupported readonly tool: {action.tool}", action_id=action.action_id)


def _query_today(action: AgentAction, message: dict[str, str], legacy: ModuleType) -> ToolResult:
    summary = legacy._load_today_intake_summary(message)
    standard = legacy._load_daily_standard(message)
    reply = legacy._format_today_query_reply(summary, standard)
    return ToolResult(
        action.tool,
        "ok",
        {
            "today_summary": summary,
            "daily_standard": standard,
            "reply": reply,
            "source": "legacy_readonly",
        },
        action_id=action.action_id,
    )


def _query_food_nutrition(action: AgentAction, message: dict[str, str], legacy: ModuleType) -> ToolResult:
    params = dict(action.parameters)
    intake_result = query_intake_food_nutrition_from_legacy(message, params, legacy=legacy)
    if intake_result.get("status") == "success":
        return ToolResult(
            action.tool,
            "ok",
            {
                "mode": "stored_intake",
                "result": intake_result,
                "reply": format_intake_food_nutrition_reply(intake_result),
                "source": "legacy_intake_query",
            },
            action_id=action.action_id,
        )

    food_db_result = _query_food_database_nutrition(params, message, legacy)
    if food_db_result.get("status") == "ok":
        return ToolResult(
            action.tool,
            "ok",
            {
                "mode": "food_database",
                "result": food_db_result,
                "reply": _format_food_database_reply(food_db_result),
                "intake_result": intake_result,
                "source": "legacy_food_database",
            },
            action_id=action.action_id,
        )

    status = str(intake_result.get("status") or food_db_result.get("status") or "no_match")
    return ToolResult(
        action.tool,
        status,
        {
            "mode": "not_found",
            "intake_result": intake_result,
            "food_database_result": food_db_result,
            "reply": format_intake_food_nutrition_reply(intake_result),
        },
        action_id=action.action_id,
    )


def _query_food_database_nutrition(params: dict[str, Any], message: dict[str, str], legacy: ModuleType) -> dict[str, Any]:
    food_query = str(params.get("food_query") or params.get("food_name") or params.get("query") or "").strip()
    grams = _number_or_none(params.get("grams"))
    if not food_query:
        return {"status": "invalid", "reason": "food_query_required"}
    if grams is None:
        grams = 100.0
    foods = legacy._load_food_database(message)
    match = choose_food_match(food_query, foods)
    if match.get("status") != "auto_matched":
        return {"status": str(match.get("status") or "not_found"), "food_query": food_query, "match": match}
    candidate = match.get("candidate") if isinstance(match.get("candidate"), dict) else {}
    food = candidate.get("food") if isinstance(candidate.get("food"), dict) else {}
    if not food:
        return {"status": "not_found", "food_query": food_query, "match": match}
    nutrition = _calculate_nutrition(food, grams)
    return {
        "status": "ok",
        "food_query": food_query,
        "food_name": str(food.get("name") or food_query),
        "grams": grams,
        "nutrition": nutrition,
        "match": match,
    }


def _generate_daily_report(action: AgentAction, message: dict[str, str], legacy: ModuleType) -> ToolResult:
    data = reporting.generate_report(message, "daily", dict(action.parameters), legacy=legacy)
    status = "ok" if str(data.get("status") or "") in {"generated", "cache_hit"} else str(data.get("status") or "error")
    return ToolResult(
        action.tool,
        status,
        {
            **data,
            "reply": str(data.get("summary_text") or ""),
            "source": "reporting_readonly",
        },
        action_id=action.action_id,
    )


def _calculate_nutrition(food: dict[str, Any], grams: float) -> dict[str, float]:
    multiplier = grams / 100.0
    carbs = _round_number(float(food.get("carbs_per_100g") or 0) * multiplier)
    protein = _round_number(float(food.get("protein_per_100g") or 0) * multiplier)
    fat = _round_number(float(food.get("fat_per_100g") or 0) * multiplier)
    kcal = _round_number(carbs * 4 + protein * 4 + fat * 9)
    return {"grams": _round_number(grams), "kcal": kcal, "carbs": carbs, "protein": protein, "fat": fat}


def _format_food_database_reply(result: dict[str, Any]) -> str:
    nutrition = result.get("nutrition") if isinstance(result.get("nutrition"), dict) else {}
    return (
        f"{result.get('food_name') or result.get('food_query')} {nutrition.get('grams', 0):g}g: "
        f"{nutrition.get('kcal', 0):g} kcal, "
        f"carbs {nutrition.get('carbs', 0):g}g, "
        f"protein {nutrition.get('protein', 0):g}g, "
        f"fat {nutrition.get('fat', 0):g}g."
    )


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round_number(value: float) -> float:
    return round(float(value) + 1e-9, 1)


__all__ = ["READONLY_ALLOWED_TOOLS", "execute_readonly_tool"]
