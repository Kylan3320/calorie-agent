"""Pending interception node: close the follow-up loop (追问闭环).

Ported from V2 domain/pending.py classification plus the missing-grams and
confirm-undo completion paths. When the newest active pending task matches the
user's reply, the message is answered here and the planner is skipped.
"""

from __future__ import annotations

import re
from typing import Any

from ..schemas import ToolResult
from ..storage.boohee_client import cached_candidates, search_food
from ..storage.food_match import choose_food_match
from ..storage.timeutil import parse_grams_from_text
from ..state import AgentState
from ..tools.base import build_intake_item

CANCEL_WORDS = (
    "取消这个", "取消该", "取消食物", "撤销这个", "撤销该食物", "不要这个", "不要了",
    "算了", "cancel this", "cancel it",
)
DEFER_WORDS = ("先不管", "稍后", "待会", "以后再说", "先放着", "skip for now", "later")
YES_WORDS = ("确认", "是", "是的", "好", "好的", "可以", "ok", "yes", "confirm", "确认撤销", "撤吧")
NO_WORDS = ("不", "取消", "先别", "别撤")


def pending_router(state: AgentState) -> dict[str, Any]:
    task = (state.get("context") or {}).get("active_pending")
    if not task:
        print("[pending_router] no active pending")
        return {"pending_route": {}}
    preset = state.get("preset")
    if preset is not None and not getattr(preset, "allow_write", True):
        # 只读影子：不完成、不取消、不延迟——落回 planner，让 policy_gate 决定回复
        print("[pending_router] readonly preset, pass through")
        return {"pending_route": {}}
    message = state.get("message") or {}
    text = str(state.get("normalized_text") or "")
    store = state.get("store")
    route = classify_pending_message(text, task)
    print(f"[pending_router] task_intent={task.get('intent')} food={task.get('food_name')} text={text!r} route={route}")

    try:
        if route == "cancel":
            store.mark_pending_status(task, "cancelled")
            return {"pending_route": _route("cancel", "已取消这条待补全记录。")}
        if route == "defer":
            return {"pending_route": _route("defer", "已保留这条待补全记录，你可以稍后补充。")}

        intent = str(task.get("intent") or "")
        if route == "answer" and intent == "missing_grams":
            return _complete_missing_grams(state, task)
        if route == "answer" and intent == "confirm_undo":
            return _complete_confirm_undo(state, task)
        if route == "pick_candidate" and intent == "missing_food":
            return _complete_food_candidate(state, task)
    except Exception as exc:  # noqa: BLE001 - pending closure must never crash the graph
        print(f"[pending_router] ERROR {type(exc).__name__}: {exc}")
        return {
            "pending_route": _route("answer", "这条待补全记录处理时遇到问题，请换个说法重试。"),
            "errors": [f"pending_router_failed:{type(exc).__name__}:{exc}"],
        }
    print("[pending_router] pass through")
    return {"pending_route": {}}


def classify_pending_message(text: str, task: dict[str, Any]) -> str:
    normalized = re.sub(r"\s+", "", str(text or "").lower())
    intent = str(task.get("intent") or "")
    if any(word in normalized for word in CANCEL_WORDS):
        return "cancel"
    if any(word in normalized for word in DEFER_WORDS):
        return "defer"
    if intent == "missing_grams" and _looks_like_grams_answer(str(text or "")):
        return "answer"
    if intent == "confirm_undo":
        if any(word in normalized for word in NO_WORDS):
            return "cancel"
        if any(word in normalized for word in YES_WORDS):
            return "answer"
    if intent == "missing_food" and re.fullmatch(r"[1-9]", str(text or "").strip()):
        return "pick_candidate"
    return "pass_through"


def _complete_missing_grams(state: AgentState, task: dict[str, Any]) -> dict[str, Any]:
    store = state.get("store")
    message = state.get("message") or {}
    grams = parse_grams_from_text(str(state.get("normalized_text") or ""))
    food_name = str(task.get("food_name") or "")
    if grams is None or grams <= 0:
        return {"pending_route": _route("answer", "没识别出克重，请直接回复数字加单位，例如：200g。")}
    match = choose_food_match(food_name, store.load_foods())
    if match.get("status") != "auto_matched":
        return {
            "pending_route": _route(
                "answer",
                f"食物库里暂时没有「{food_name}」，这条待补全记录仍保留，你可以在多维表格里补充它。",
            )
        }
    candidate = match.get("candidate") if isinstance(match.get("candidate"), dict) else {}
    food = candidate.get("food") if isinstance(candidate.get("food"), dict) else {}
    item = build_intake_item(food, grams)
    write = store.write_intake(message, [item], str(task.get("meal_type") or "unknown"))
    if write.get("status") == "duplicate_skipped":
        store.mark_pending_status(task, "completed")
        return {"pending_route": _route("answer", "这条消息之前处理过，已跳过重复写入。"), "tool_results": [
            ToolResult("record_food", "duplicate_skipped", {"food_query": food_name, "grams": grams}).to_dict()
        ]}
    store.mark_pending_status(task, "completed")
    nutrition = item["nutrition"]
    reply = (
        f"已记录{item['matched_name']} {item['grams']:g}g：{nutrition['kcal']:g} kcal"
        f"（碳水 {nutrition['carbs']:g}g、蛋白质 {nutrition['protein']:g}g、脂肪 {nutrition['fat']:g}g）。"
    )
    return {
        "pending_route": _route("answer", reply),
        "tool_results": [
            ToolResult(
                "record_food",
                "ok",
                {
                    "food_query": food_name,
                    "matched_name": item["matched_name"],
                    "grams": item["grams"],
                    "nutrition": nutrition,
                    "intake_write_result": {"status": "created", "created_count": write.get("created_count", 0)},
                    "completed_pending": task.get("pending_id", ""),
                },
            ).to_dict()
        ],
    }


def _complete_confirm_undo(state: AgentState, task: dict[str, Any]) -> dict[str, Any]:
    store = state.get("store")
    message = state.get("message") or {}
    undo = store.undo_last(message.get("user_id", ""))
    store.mark_pending_status(task, "completed")
    if undo.get("status") == "deleted":
        item = undo.get("item") or {}
        reply = f"已撤销 {item.get('food_name', '记录')} {float(item.get('grams') or 0):g}g。"
    else:
        reply = "当前没有可撤销的记录。"
    facts = [ToolResult("undo_last", "ok", {"undo_result": undo}).to_dict()]
    return {"pending_route": _route("answer", reply), "tool_results": facts}


def _complete_food_candidate(state: AgentState, task: dict[str, Any]) -> dict[str, Any]:
    """Boohee M1: user replied a candidate number -> import it into the food library."""
    store = state.get("store")
    food_query = str(task.get("food_name") or "")
    choice = int(str(state.get("normalized_text") or "").strip()) - 1
    candidates = search_food(food_query)  # process cache hit; refetch only after restart
    pick = candidates[choice] if 0 <= choice < len(candidates) else cached_candidates(food_query, choice)
    if pick is None:
        return {"pending_route": _route("answer", f"候选缓存已失效，请重新发送「{food_query} + 克重」再选一次。")}
    write = store.write_food(
        {
            "name": pick["name"],
            "carbs_per_100g": pick["carbs_per_100g"],
            "protein_per_100g": pick["protein_per_100g"],
            "fat_per_100g": pick["fat_per_100g"],
            "source": "boohee",
            "boohee_id": pick["boohee_id"],
        }
    )
    store.mark_pending_status(task, "completed")
    if write.get("status") == "duplicate":
        reply = f"「{pick['name']}」已经在食物库里了，直接发「{pick['name']} + 克重」即可记录。"
    else:
        kcal = round(pick["carbs_per_100g"] * 4 + pick["protein_per_100g"] * 4 + pick["fat_per_100g"] * 9, 1)
        pick = {**pick, "kcal_per_100g": kcal}
        reply = (
            f"已把「{pick['name']}」加入食物库（每100g：{kcal:g} kcal，碳水 {pick['carbs_per_100g']:g}g、"
            f"蛋白质 {pick['protein_per_100g']:g}g、脂肪 {pick['fat_per_100g']:g}g）。"
            f"现在发「{pick['name']} + 克重」即可记录。数据来源：薄荷健康。"
        )
    facts = [ToolResult("record_food", "food_library_added", {"food_query": food_query, "imported": pick, "write_food_result": write}).to_dict()]
    return {"pending_route": _route("answer", reply), "tool_results": facts}


def _route(route: str, reply: str, **extra: Any) -> dict[str, Any]:
    return {"handled": True, "route": route, "reply": reply, **extra}


def _looks_like_grams_answer(text: str) -> bool:
    grams = parse_grams_from_text(text)
    if grams is None:
        return False
    remainder = re.sub(r"\d+(?:\.\d+)?", "", text.lower())
    remainder = re.sub(r"(?:kg|g|克|千克|公斤|斤|大概|约|左右|补充|克重|是|=|：|:|\s)+", "", remainder)
    return not remainder


__all__ = ["classify_pending_message", "pending_router"]
