"""Restore follow-up actions; all mutations run through the policy and tools."""

from __future__ import annotations

import re
from typing import Any

from ..schemas import AgentAction, AgentPlan
from ..state import AgentState
from ..storage.timeutil import parse_grams_from_text

CANCEL_WORDS = {"取消", "取消这个", "取消该", "取消食物", "不要这个", "不要了", "算了", "cancel this", "cancel it", "不", "先别", "别撤"}
DEFER_WORDS = {"先不管", "稍后", "待会", "以后再说", "先放着", "skip for now", "later"}
YES_WORDS = {"确认", "是", "是的", "好", "好的", "可以", "ok", "yes", "confirm", "确认撤销", "撤吧"}


def classify_pending_message(text: str, task: dict[str, Any]) -> str:
    answer = re.sub(r"\s+", " ", text.lower()).strip(" !！。.?？")
    if answer in CANCEL_WORDS:
        return "cancel"
    if answer in DEFER_WORDS:
        return "defer"
    intent = task["intent"]
    if intent == "confirm_undo" and answer in YES_WORDS:
        return "answer"
    if intent == "missing_grams" and re.fullmatch(r"\d+(?:\.\d+)?\s*(?:kg|g|克|千克|公斤|斤)?", text.strip(), re.I):
        return "answer"
    if intent == "missing_food" and (task.get("payload") or {}).get("candidates") and re.fullmatch(r"[1-9]", text.strip()):
        return "pick_candidate"
    return "pass_through"


def pending_router(state: AgentState) -> dict[str, Any]:
    execution = state.get("execution")
    saved = execution.get("payload", {}) if execution else {}
    if saved.get("plan") is not None:
        return {"plan": saved["plan"], "pending_route": saved.get("pending_route", {})}
    task = state["context"].get("active_pending")
    if not task:
        return {"pending_route": {}}
    route = classify_pending_message(state["normalized_text"], task)
    if route == "pass_through":
        return {"pending_route": {}}
    tool = "undo_last" if task["intent"] == "confirm_undo" else "record_food"
    params = {} if tool == "undo_last" else {
        "food_query": task["food_name"], "grams": task.get("grams"), "meal_type": task["meal_type"]}
    if route == "answer" and task["intent"] == "missing_grams":
        params["grams"] = parse_grams_from_text(state["normalized_text"])
    resolution = {"handled": True, "route": route}
    if route == "pick_candidate":
        resolution["choice"] = int(state["normalized_text"].strip()) - 1
    return {"pending_route": resolution,
            "plan": AgentPlan("pending_resolution", [AgentAction("a1", tool, params)]).to_dict()}


__all__ = ["classify_pending_message", "pending_router"]
