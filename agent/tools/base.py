"""Shared portion validation and nutrition math."""

from __future__ import annotations

from typing import Any
import math

from ..storage.timeutil import round_number


def positive_grams(value: Any) -> float | None:
    if value is None:
        return None
    grams = float(value)
    if not math.isfinite(grams) or grams <= 0:
        raise ValueError("grams must be a finite positive number")
    return grams


def calculate_food_intake(food: dict[str, Any], grams: float) -> dict[str, float]:
    """Scale source facts; use the macro formula only when source kcal is absent."""
    multiplier = grams / 100
    carbs = round_number(float(food.get("carbs_per_100g") or 0) * multiplier)
    protein = round_number(float(food.get("protein_per_100g") or 0) * multiplier)
    fat = round_number(float(food.get("fat_per_100g") or 0) * multiplier)
    source_kcal = food.get("kcal_per_100g")
    if source_kcal is None:
        source_kcal = (float(food.get("carbs_per_100g") or 0) * 4
                       + float(food.get("protein_per_100g") or 0) * 4
                       + float(food.get("fat_per_100g") or 0) * 9)
    kcal = round_number(float(source_kcal) * multiplier)
    return {"carbs": carbs, "protein": protein, "fat": fat, "kcal": kcal}
