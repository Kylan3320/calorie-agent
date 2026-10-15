"""Shared tool context and nutrition math."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..storage.food_match import choose_food_match
from ..storage.timeutil import round_number


@dataclass
class ToolContext:
    store: Any  # BitableStore | FakeStore (contracts/tool_protocol.md)
    config: Any
    context: dict[str, Any]  # graph-level facts: active_pending, etc.


def calculate_food_intake(food: dict[str, Any], grams: float) -> dict[str, float]:
    """Nutrition snapshot math: per-100g scaled by grams, kcal = 4/4/9 (V2 formula)."""
    multiplier = grams / 100
    carbs = round_number(float(food.get("carbs_per_100g") or 0) * multiplier)
    protein = round_number(float(food.get("protein_per_100g") or 0) * multiplier)
    fat = round_number(float(food.get("fat_per_100g") or 0) * multiplier)
    kcal = round_number(carbs * 4 + protein * 4 + fat * 9)
    return {"carbs": carbs, "protein": protein, "fat": fat, "kcal": kcal}


def match_food(ctx: ToolContext, food_query: str) -> dict[str, Any]:
    return choose_food_match(food_query, ctx.store.load_foods())


def build_intake_item(food: dict[str, Any], grams: float) -> dict[str, Any]:
    return {
        "food_id": str(food.get("food_id") or ""),
        "matched_name": str(food.get("name") or ""),
        "grams": round_number(float(grams)),
        "nutrition": calculate_food_intake(food, grams),
    }


__all__ = ["ToolContext", "build_intake_item", "calculate_food_intake", "match_food"]
