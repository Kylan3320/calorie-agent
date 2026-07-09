from __future__ import annotations

from typing import Any

from langchain_core.tools import StructuredTool

from ..schemas import AgentAction, ToolResult


def execute_fake_tool(action: AgentAction, policy_decision: dict[str, Any]) -> ToolResult:
    blocked = _is_blocked(action.action_id, policy_decision)
    if action.tool == "record_food":
        return _record_food(action, blocked)
    if action.tool == "query_today":
        return _query_today(action)
    if action.tool == "search_food":
        return _search_food(action)
    if action.tool == "query_food_nutrition":
        return _query_food_nutrition(action)
    if action.tool == "undo_intake":
        return _undo_intake(action)
    if action.tool == "generate_daily_report":
        return _generate_daily_report(action)
    return ToolResult(action.tool, "error", error=f"unsupported fake tool: {action.tool}", action_id=action.action_id)


def build_langchain_fake_tools() -> list[StructuredTool]:
    return [
        StructuredTool.from_function(
            func=lambda food_query, grams=None: {"status": "blocked_fake_recorded", "food_query": food_query, "grams": grams},
            name="record_food",
            description="V0 fake write tool. It never writes real data.",
        ),
        StructuredTool.from_function(
            func=lambda: {"status": "ok", "summary": "fake today summary"},
            name="query_today",
            description="V0 fake read-only today summary tool.",
        ),
    ]


def _record_food(action: AgentAction, blocked: bool) -> ToolResult:
    params = action.parameters
    status = "blocked_fake_recorded" if blocked else "fake_recorded"
    return ToolResult(
        action.tool,
        status,
        {
            "food_query": params.get("food_query") or params.get("food_name") or "",
            "grams": params.get("grams"),
            "write_blocked": blocked,
            "real_write": False,
        },
        action_id=action.action_id,
    )


def _query_today(action: AgentAction) -> ToolResult:
    return ToolResult(
        action.tool,
        "ok",
        {
            "count": 2,
            "items": [
                {"food_name": "鸡蛋", "grams": 100, "kcal": 143},
                {"food_name": "米饭", "grams": 200, "kcal": 232},
            ],
            "totals": {"kcal": 375, "carbs": 57.8, "protein": 15.6, "fat": 9.8},
            "source": "fake_tool",
        },
        action_id=action.action_id,
    )


def _search_food(action: AgentAction) -> ToolResult:
    query = str(action.parameters.get("food_query") or action.parameters.get("query") or "")
    return ToolResult(
        action.tool,
        "ok",
        {"query": query, "candidates": [{"food_name": query or "鸡蛋", "score": 0.9}], "source": "fake_tool"},
        action_id=action.action_id,
    )


def _query_food_nutrition(action: AgentAction) -> ToolResult:
    query = str(action.parameters.get("food_query") or "西瓜")
    return ToolResult(
        action.tool,
        "ok",
        {
            "food_name": query,
            "matched_record": {"food_name": query, "grams": 300},
            "total": {"grams": 300, "kcal": 84, "carbs": 18.0, "protein": 1.8, "fat": 0.6},
            "source": "fake_stored_intake_snapshot",
        },
        action_id=action.action_id,
    )


def _undo_intake(action: AgentAction) -> ToolResult:
    return ToolResult(
        action.tool,
        "blocked_requires_confirmation",
        {"target": dict(action.parameters), "real_delete": False},
        action_id=action.action_id,
    )


def _generate_daily_report(action: AgentAction) -> ToolResult:
    return ToolResult(
        action.tool,
        "ok",
        {
            "report_type": "daily",
            "summary": "fake daily report",
            "totals": {"kcal": 375, "carbs": 57.8, "protein": 15.6, "fat": 9.8},
            "source": "fake_tool",
        },
        action_id=action.action_id,
    )


def _is_blocked(action_id: str, policy_decision: dict[str, Any]) -> bool:
    blocked = policy_decision.get("blocked_actions") if isinstance(policy_decision, dict) else []
    return any(isinstance(item, dict) and item.get("action_id") == action_id for item in blocked)
