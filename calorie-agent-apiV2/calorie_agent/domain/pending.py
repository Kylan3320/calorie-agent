from __future__ import annotations

import re
from types import ModuleType
from typing import Any

from .. import legacy_app


CANCEL_WORDS = (
    "取消这个",
    "取消该",
    "取消食物",
    "撤销这个",
    "撤销该食物",
    "不要这个",
    "不要了",
    "cancel this",
    "cancel it",
)

DEFER_WORDS = (
    "先不管",
    "稍后",
    "待会",
    "以后再说",
    "先放着",
    "skip for now",
    "later",
)

YES_WORDS = (
    "yes",
    "ok",
    "okay",
    "confirm",
    "sure",
    "use it",
    "是",
    "可以",
    "确认",
    "按这个",
    "用这个",
)


def classify_pending_message(
    message: dict[str, str],
    task: dict[str, Any],
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    text = str(message.get("text") or "").strip()
    normalized = _normalize(text)
    intent = str(task.get("intent") or "")

    if _contains_any(normalized, CANCEL_WORDS):
        return {"route": "cancel", "task": task, "reason": "cancel_keyword"}
    if _contains_any(normalized, DEFER_WORDS):
        return {"route": "defer", "task": task, "reason": "defer_keyword"}
    if intent == "missing_grams" and _looks_like_grams_answer(text, legacy):
        return {"route": "answer", "task": task, "reason": "grams_answer"}
    if intent == "confirm_default_grams" and (_contains_any(normalized, YES_WORDS) or _looks_like_grams_answer(text, legacy)):
        return {"route": "answer", "task": task, "reason": "default_grams_confirmed"}
    if intent == "missing_food" and legacy._parse_per_100g_macros_from_text(text) is not None:
        return {"route": "answer", "task": task, "reason": "macro_answer"}
    return {"route": "pass_through", "task": task, "reason": "not_pending_answer"}


def handle_pending_decision(
    message: dict[str, str],
    decision: dict[str, Any],
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    route = str(decision.get("route") or "")
    task = decision.get("task") if isinstance(decision.get("task"), dict) else {}
    if route == "answer":
        result = legacy._complete_pending_task(message, task)
        return {"route": route, "status": result.get("status", "ok"), "result": result}
    if route == "cancel":
        update = legacy._mark_pending_task_status(task, "cancelled")
        return {
            "route": route,
            "status": "cancelled",
            "result": {
                "status": "cancelled",
                "reply": "已取消这条待补全记录。",
                "pending_task": task,
                "update_result": update,
            },
        }
    if route == "defer":
        return {
            "route": route,
            "status": "deferred",
            "result": {
                "status": "deferred",
                "reply": "已保留这条待补全记录，你可以稍后继续补充。",
                "pending_task": task,
            },
        }
    return {"route": route, "status": "pass_through", "result": {}}


def handle_active_pending_message(
    message: dict[str, str],
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    task = legacy._load_active_pending_task(message)
    if not task:
        return {"route": "none", "status": "no_active_pending", "result": {}, "task": {}}
    decision = classify_pending_message(message, task, legacy=legacy)
    handled = handle_pending_decision(message, decision, legacy=legacy)
    return {**handled, "task": task, "decision": decision}


def _looks_like_grams_answer(text: str, legacy: ModuleType) -> bool:
    grams = legacy._parse_grams_from_text(text)
    if grams is None:
        return False
    remainder = re.sub(r"\d+(?:\.\d+)?", "", text.lower())
    remainder = re.sub(r"(?:kg|g|克|千克|公斤|斤|大概|约|左右|补充|克重|是|=|：|:|\s)+", "", remainder)
    return not remainder


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(_normalize(word) in text for word in words)


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").lower())
