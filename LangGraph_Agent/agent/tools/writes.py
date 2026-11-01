"""Write tools own matching, pending transitions and fixed-target mutations."""

from __future__ import annotations

from typing import Any

from ..schemas import ToolResult
from ..storage.boohee_client import BooheeError, search_food
from .base import ToolContext, build_intake_item, calculate_food_intake, match_food, positive_grams


def _pending(message, ctx, intent, food_query, grams, meal_type, **payload):
    task = ctx.store.create_pending(message, intent, food_query, grams, meal_type, payload=payload)
    if task.get("status") == "not_configured":
        raise RuntimeError("pending_tasks is required for follow-up writes")
    return task


def run_record_food(message: dict[str, str], params: dict[str, Any], ctx: ToolContext) -> ToolResult:
    food_query = str(params.get("food_query") or params.get("food_name") or "").strip()
    if not food_query:
        return ToolResult("record_food", "error", error="food_query_missing")
    meal_type = str(params.get("meal_type") or "unknown")
    grams = positive_grams(params.get("grams"))
    task = ctx.context.get("active_pending")
    resolution = ctx.context.get("pending_route", {})
    food = None
    if resolution.get("route") == "pick_candidate":
        payload = task.get("payload") or {}
        candidates = payload.get("candidates", [])
        choice = resolution["choice"]
        if not 0 <= choice < len(candidates):
            return ToolResult("record_food", "error", error="candidate_out_of_range")
        food = candidates[choice]["food"]
        if payload["mode"] == "remote_candidates":
            write = ctx.store.write_food(food)
            if write["status"] not in {"created", "duplicate"}:
                raise RuntimeError(f"food_import_failed:{write['status']}")
            return ToolResult("record_food", "food_library_added", {
                "food_query": food_query, "imported": {**food, "kcal_per_100g": calculate_food_intake(food, 100)["kcal"]},
                "write_food_result": write})
    if grams is None:
        pending = _pending(message, ctx, "missing_grams", food_query, None, meal_type)
        return ToolResult("record_food", "pending_created", {"food_query": food_query, "grams": None, "pending_result": pending})
    if food is None:
        match = match_food(ctx, food_query)
        if match["status"] == "auto_matched":
            food = match["candidate"]["food"]
        else:
            mode, error = "local_candidates", ""
            candidates = [{"index": i + 1, "food_name": c["food"]["name"], "food": c["food"]}
                          for i, c in enumerate(match["candidates"])]
            if match["status"] == "not_found":
                mode = "remote_candidates"
                try:
                    remote = search_food(food_query)
                except BooheeError as exc:
                    remote, error = [], f"boohee_search_failed:{exc}"
                candidates = [{"index": i + 1, "name": f["name"],
                               "kcal_per_100g": calculate_food_intake(f, 100)["kcal"],
                               "food": {**f, "source": "boohee"}}
                              for i, f in enumerate(remote)]
            pending = _pending(message, ctx, "missing_food", food_query, grams, meal_type,
                               mode=mode, candidates=candidates)
            candidates = pending["payload"].get("candidates", [])
            mode = pending["payload"]["mode"]
            return ToolResult("record_food", "needs_confirmation" if candidates else "pending_created",
                              {"food_query": food_query, "grams": grams, "mode": mode,
                               "candidates": candidates, "pending_result": pending}, error=error)
    item = build_intake_item(food, grams)
    item["date"] = ctx.context.get("intake_date") or ctx.store.today_string()
    write = ctx.store.write_intake(message, [item], meal_type)
    if write["status"] == "duplicate_skipped":
        return ToolResult("record_food", "duplicate_skipped", {"food_query": food_query, "intake_write_result": write})
    if write["status"] != "created" or write["created_count"] <= 0:
        raise RuntimeError("intake_write_not_confirmed")
    return ToolResult("record_food", "ok", {
        "food_query": food_query, "matched_name": item["matched_name"], "grams": item["grams"],
        "nutrition": item["nutrition"], "source": food.get("source", ""), "intake_write_result": write})


def run_undo_last(message: dict[str, str], params: dict[str, Any], ctx: ToolContext) -> ToolResult:
    task = ctx.context.get("active_pending")
    resolution = ctx.context.get("pending_route", {})
    user_id = message.get("user_id", "")
    if resolution.get("route") == "answer" and task and task["intent"] == "confirm_undo":
        target = (task.get("payload") or {}).get("target")
        if not target:
            return ToolResult("undo_last", "error", error="confirmation_target_missing")
        result = ctx.store.undo_last(user_id, target["record_id"])
        return ToolResult("undo_last", "ok", {"undo_result": result})
    target = ctx.store.last_intake(user_id)
    if target is None:
        return ToolResult("undo_last", "ok", {"undo_result": {"status": "nothing_to_undo", "item": None}})
    pending = _pending(message, ctx, "confirm_undo", "undo_last", None, "unknown", target=target)
    return ToolResult("undo_last", "needs_confirmation_plan", {"undo_result": {"status": "needs_confirmation"}, "pending_result": pending})


__all__ = ["run_record_food", "run_undo_last"]
