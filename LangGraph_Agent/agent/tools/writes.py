"""Write tools: record_food, undo_last.

record_food decision tree (P0 + Boohee M1 fallback, REQUIREMENTS.md §3/D23):
- grams missing -> pending(missing_grams), nothing written
- food ambiguous (needs_confirmation) -> return candidates, nothing written
- food not found locally -> Boohee search; remote candidates -> needs_confirmation
  (reply number imports the pick into the food library via pending_router);
  no remote hit / no key / failure -> pending(missing_food), nothing written
- auto matched -> write intake snapshot; duplicate message -> duplicate_skipped
"""

from __future__ import annotations

from typing import Any

from ..schemas import ToolResult
from ..storage.boohee_client import search_food
from ..storage.timeutil import round_number
from .base import ToolContext, build_intake_item, match_food


def run_record_food(message: dict[str, str], params: dict[str, Any], ctx: ToolContext) -> ToolResult:
    food_query = str(params.get("food_query") or params.get("food_name") or "").strip()
    if not food_query:
        return ToolResult("record_food", "error", error="food_query_missing")
    meal_type = str(params.get("meal_type") or "unknown")
    grams = _grams_or_none(params.get("grams"))

    if grams is None:
        pending = ctx.store.create_pending(message, "missing_grams", food_query, None, meal_type)
        return ToolResult(
            "record_food",
            "pending_created",
            {
                "food_query": food_query,
                "grams": None,
                "pending_result": {"status": pending.get("status"), "intent": "missing_grams", "food_name": food_query},
            },
        )

    match = match_food(ctx, food_query)
    if match.get("status") == "needs_confirmation":
        candidates = [
            {"food_name": str((c.get("food") or {}).get("name") or ""), "score": c.get("score")}
            for c in match.get("candidates", [])
            if isinstance(c, dict)
        ]
        return ToolResult(
            "record_food",
            "needs_confirmation",
            {"food_query": food_query, "grams": grams, "candidates": candidates, "intake_write_result": {"status": "not_written"}},
        )
    if match.get("status") != "auto_matched":
        remote = search_food(food_query)
        if remote:
            candidates = [
                {
                    "index": index + 1,
                    "boohee_id": item["boohee_id"],
                    "name": item["name"],
                    "carbs_per_100g": item["carbs_per_100g"],
                    "protein_per_100g": item["protein_per_100g"],
                    "fat_per_100g": item["fat_per_100g"],
                    "kcal_per_100g": round_number(item["carbs_per_100g"] * 4 + item["protein_per_100g"] * 4 + item["fat_per_100g"] * 9),
                }
                for index, item in enumerate(remote)
            ]
            ctx.store.create_pending(message, "missing_food", food_query, grams, meal_type)
            return ToolResult(
                "record_food",
                "needs_confirmation",
                {
                    "mode": "remote_candidates",
                    "food_query": food_query,
                    "grams": grams,
                    "candidates": candidates,
                    "intake_write_result": {"status": "not_written"},
                },
            )
        pending = ctx.store.create_pending(message, "missing_food", food_query, grams, meal_type)
        return ToolResult(
            "record_food",
            "pending_created",
            {
                "food_query": food_query,
                "grams": grams,
                "pending_result": {"status": pending.get("status"), "intent": "missing_food", "food_name": food_query},
            },
        )

    candidate = match.get("candidate") if isinstance(match.get("candidate"), dict) else {}
    food = candidate.get("food") if isinstance(candidate.get("food"), dict) else {}
    item = build_intake_item(food, grams)
    write = ctx.store.write_intake(message, [item], meal_type)
    if write.get("status") == "duplicate_skipped":
        return ToolResult(
            "record_food",
            "duplicate_skipped",
            {
                "food_query": food_query,
                "grams": round_number(grams),
                "matched_name": item["matched_name"],
                "intake_write_result": {"status": "duplicate_skipped", "created_count": 0},
            },
        )
    return ToolResult(
        "record_food",
        "ok",
        {
            "food_query": food_query,
            "matched_name": item["matched_name"],
            "grams": item["grams"],
            "nutrition": item["nutrition"],
            "intake_write_result": {"status": "created", "created_count": write.get("created_count", 0)},
        },
    )


def run_undo_last(message: dict[str, str], params: dict[str, Any], ctx: ToolContext) -> ToolResult:
    result = ctx.store.undo_last(message.get("user_id", ""))
    return ToolResult("undo_last", "ok", {"undo_result": result})


def _grams_or_none(value: Any) -> float | None:
    try:
        grams = float(value)
    except (TypeError, ValueError):
        return None
    return grams if grams > 0 else None


__all__ = ["run_record_food", "run_undo_last"]
