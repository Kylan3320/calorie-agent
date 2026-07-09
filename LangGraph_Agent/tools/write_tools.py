from __future__ import annotations

from typing import Any

from ...domain.food_match import choose_food_match
from ..schemas import AgentAction, ToolResult
from .read_tools import execute_readonly_tool


WRITE_ALLOWED_TOOLS = {
    "record_food",
    "query_today",
    "query_food_nutrition",
}


class FakeWriteLegacy:
    def __init__(self) -> None:
        self.seen_write_keys: set[str] = set()
        self.records: list[dict[str, Any]] = []
        self.foods = [
            {
                "food_id": "food_egg",
                "record_id": "food_rec_egg",
                "name": "鸡蛋",
                "aliases": ["egg"],
                "carbs_per_100g": 1.1,
                "protein_per_100g": 12.6,
                "fat_per_100g": 9.5,
            },
            {
                "food_id": "food_rice",
                "record_id": "food_rec_rice",
                "name": "米饭",
                "aliases": ["rice"],
                "carbs_per_100g": 25.9,
                "protein_per_100g": 2.6,
                "fat_per_100g": 0.3,
            },
            {
                "food_id": "food_noodle",
                "record_id": "food_rec_noodle",
                "name": "米粉",
                "aliases": [],
                "carbs_per_100g": 24.0,
                "protein_per_100g": 1.8,
                "fat_per_100g": 0.2,
            },
        ]

    def _load_food_database(self, _message: dict[str, str] | None = None) -> list[dict[str, Any]]:
        return list(self.foods)

    def _load_today_intake_summary(self, _message: dict[str, str]) -> dict[str, Any]:
        items = [
            {
                "food_name": item["food_name"],
                "grams": item["grams"],
                "kcal": item["kcal"],
                "carbs": item["carbs"],
                "protein": item["protein"],
                "fat": item["fat"],
            }
            for item in self.records
        ]
        totals = {"kcal": 0.0, "carbs": 0.0, "protein": 0.0, "fat": 0.0}
        for item in items:
            for key in totals:
                totals[key] = round(totals[key] + float(item.get(key) or 0), 1)
        return {"count": len(items), "items": items, "totals": totals}

    def _load_daily_standard(self, _message: dict[str, str]) -> dict[str, float]:
        return {"kcal": 1800.0, "carbs": 200.0, "protein": 100.0, "fat": 60.0}

    def _format_today_query_reply(self, summary: dict[str, Any], _standard: dict[str, Any]) -> str:
        totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
        return f"今天已记录 {summary.get('count', 0)} 条，总热量 {totals.get('kcal', 0):g} kcal。"


def execute_fake_write_tool(
    action: AgentAction,
    message: dict[str, str],
    *,
    legacy: FakeWriteLegacy,
    allowed_tools: set[str] | None = None,
    fixture_mode: str = "",
) -> ToolResult:
    allowed = allowed_tools or WRITE_ALLOWED_TOOLS
    if action.tool not in allowed:
        return ToolResult(action.tool, "blocked_tool_not_allowed", error="tool_not_allowed", action_id=action.action_id)
    if action.tool == "record_food":
        return _record_food(action, message, legacy, fixture_mode=fixture_mode)
    if action.tool in {"query_today", "query_food_nutrition"}:
        return execute_readonly_tool(action, message, legacy=legacy, allowed_tools=allowed)
    return ToolResult(action.tool, "error", error=f"unsupported fake write tool: {action.tool}", action_id=action.action_id)


def _record_food(
    action: AgentAction,
    message: dict[str, str],
    legacy: FakeWriteLegacy,
    *,
    fixture_mode: str,
) -> ToolResult:
    if fixture_mode == "write_error":
        return ToolResult(action.tool, "error", error="fake_write_error", action_id=action.action_id)

    params = action.parameters
    food_query = str(params.get("food_query") or params.get("food_name") or "").strip()
    grams = _number_or_none(params.get("grams"))
    if not food_query:
        return _pending(action, "food_query_missing", food_query, grams)
    if grams is None or grams <= 0:
        return _pending(action, "grams_missing", food_query, grams)
    if "不存在" in food_query:
        return _pending(action, "food_not_found", food_query, grams)

    match = choose_food_match(food_query, legacy._load_food_database(message))
    if match.get("status") == "needs_confirmation":
        return ToolResult(
            action.tool,
            "needs_confirmation",
            {
                "food_query": food_query,
                "grams": grams,
                "food_match": match,
                "intake_write_result": {"status": "not_written", "created_count": 0},
                "real_write": False,
            },
            action_id=action.action_id,
        )
    if match.get("status") != "auto_matched":
        return _pending(action, "food_not_found", food_query, grams, food_match=match)

    write_key = f"{message.get('message_id', '')}:{action.action_id}:{food_query}:{grams:g}"
    if write_key in legacy.seen_write_keys:
        return ToolResult(
            action.tool,
            "duplicate_skipped",
            {
                "food_query": food_query,
                "grams": grams,
                "food_match": match,
                "intake_write_result": {"status": "duplicate_skipped", "created_count": 0},
                "duplicate_result": {"status": "duplicate_skipped", "key": write_key},
                "real_write": False,
            },
            action_id=action.action_id,
        )

    legacy.seen_write_keys.add(write_key)
    food = match.get("candidate", {}).get("food", {}) if isinstance(match.get("candidate"), dict) else {}
    nutrition = _nutrition(food, grams)
    item = {
        "food_name": str(food.get("name") or food_query),
        "grams": grams,
        **nutrition,
    }
    legacy.records.append(item)
    return ToolResult(
        action.tool,
        "ok",
        {
            "food_query": food_query,
            "grams": grams,
            "food_match": match,
            "intake_write_result": {"status": "created", "created_count": 1},
            "recorded_item": item,
            "real_write": False,
            "reply": f"已记录{item['food_name']} {grams:g}g。",
        },
        action_id=action.action_id,
    )


def _pending(
    action: AgentAction,
    reason: str,
    food_query: str,
    grams: float | None,
    *,
    food_match: dict[str, Any] | None = None,
) -> ToolResult:
    return ToolResult(
        action.tool,
        "pending_created",
        {
            "food_query": food_query,
            "grams": grams,
            "food_match": food_match or {},
            "pending_result": {
                "status": "created",
                "reason": reason,
                "food_name": food_query,
                "grams": grams,
            },
            "intake_write_result": {"status": "not_written", "created_count": 0},
            "real_write": False,
        },
        action_id=action.action_id,
    )


def _nutrition(food: dict[str, Any], grams: float) -> dict[str, float]:
    multiplier = grams / 100.0
    carbs = _round(float(food.get("carbs_per_100g") or 0) * multiplier)
    protein = _round(float(food.get("protein_per_100g") or 0) * multiplier)
    fat = _round(float(food.get("fat_per_100g") or 0) * multiplier)
    kcal = _round(carbs * 4 + protein * 4 + fat * 9)
    return {"kcal": kcal, "carbs": carbs, "protein": protein, "fat": fat}


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _round(value: float) -> float:
    return round(float(value) + 1e-9, 1)
