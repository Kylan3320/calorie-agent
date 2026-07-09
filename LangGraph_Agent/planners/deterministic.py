"""Deterministic planner implementation and helpers."""

from __future__ import annotations

import re
from typing import Any

from ..schemas import AgentPlan
from ..state import LangGraphAgentState


def deterministic_planner(state: LangGraphAgentState) -> dict[str, Any]:
    text = str(state.get("normalized_text") or "")
    plan = _build_plan(text)
    return {"plan": plan.to_dict(), "errors": list(state.get("errors", []))}


def build_plan(text: str) -> AgentPlan:
    return _build_plan(text)

def _build_plan(text: str) -> AgentPlan:
    actions: list[dict[str, Any]] = []
    used_context = ["project_docs"]
    lower_text = text.lower()
    if _is_small_talk(text):
        return AgentPlan(goal="unknown", actions=[], used_context=used_context, confidence=0.7)
    if _contains_any(text, ("撤销", "删除", "上一条")):
        return AgentPlan(
            goal="semantic_undo",
            actions=[
                {
                    "action_id": "a1",
                    "tool": "undo_intake",
                    "parameters": {"mode": "previous"},
                    "reason": "用户请求撤销上一条记录",
                    "safety": "destructive",
                }
            ],
            used_context=used_context,
            needs_confirmation=True,
            confidence=0.86,
        )
    if _contains_any(text, ("总结", "日报", "报告", "今日汇总")):
        return AgentPlan(
            goal="daily_report",
            actions=[
                {
                    "action_id": "a1",
                    "tool": "generate_daily_report",
                    "parameters": {"period": "today"},
                    "reason": "用户请求今日总结",
                    "safety": "read_only",
                }
            ],
            used_context=used_context,
            confidence=0.88,
        )
    if _is_nutrition_query(text):
        food_query = _extract_food_query(text) or "西瓜"
        params: dict[str, Any] = {"food_query": food_query, "date_scope": "today"}
        grams = _extract_grams(text)
        if grams is not None:
            params["grams"] = int(grams) if grams.is_integer() else grams
        return AgentPlan(
            goal="query_food_nutrition",
            actions=[
                {
                    "action_id": "a1",
                    "tool": "query_food_nutrition",
                    "parameters": params,
                    "reason": "用户查询食物营养",
                    "safety": "read_only",
                }
            ],
            used_context=[*used_context, "user_history", "food_db"],
            confidence=0.88,
        )
    if _contains_any(text, ("撤销", "删除", "上一条")):
        return AgentPlan(
            goal="semantic_undo",
            actions=[
                {
                    "action_id": "a1",
                    "tool": "undo_intake",
                    "parameters": {"mode": "previous"},
                    "reason": "用户请求撤销上一条记录",
                    "safety": "destructive",
                }
            ],
            used_context=used_context,
            needs_confirmation=True,
            confidence=0.86,
        )
    if _contains_any(text, ("总结", "日报", "报告")) or _contains_any(
        lower_text,
        ("today summary", "daily summary", "today report", "daily report"),
    ):
        return AgentPlan(
            goal="daily_report",
            actions=[
                {
                    "action_id": "a1",
                    "tool": "generate_daily_report",
                    "parameters": {"period": "today"},
                    "reason": "用户请求今日总结",
                    "safety": "read_only",
                }
            ],
            used_context=used_context,
            confidence=0.88,
        )
    if _contains_any(text, ("那条", "热量", "营养")) and "西瓜" in text:
        return AgentPlan(
            goal="query_recorded_food_nutrition",
            actions=[
                {
                    "action_id": "a1",
                    "tool": "query_food_nutrition",
                    "parameters": {"food_query": "西瓜", "date_scope": "today"},
                    "reason": "用户查询已记录西瓜的营养",
                    "safety": "read_only",
                }
            ],
            used_context=[*used_context, "user_history", "food_db"],
            confidence=0.9,
        )

    record_actions = _record_actions(text)
    asks_today = _contains_any(text, ("今天吃", "今天", "还剩", "剩多少", "查一下", "今天摄入", "今日摄入", "今日汇总", "吃了什么"))
    if record_actions:
        actions.extend(record_actions)
        if asks_today:
            actions.append(
                {
                    "action_id": f"a{len(actions) + 1}",
                    "tool": "query_today",
                    "parameters": {},
                    "reason": "记录后查询今日汇总",
                    "safety": "read_only",
                    "depends_on": [item["action_id"] for item in actions],
                }
            )
        return AgentPlan(
            goal="record_and_query" if asks_today else "record_intake",
            actions=actions,
            used_context=[*used_context, "food_db"],
            confidence=0.86,
        )
    if asks_today:
        return AgentPlan(
            goal="query_today",
            actions=[
                {
                    "action_id": "a1",
                    "tool": "query_today",
                    "parameters": {},
                    "reason": "用户查询今日摄入",
                    "safety": "read_only",
                }
            ],
            used_context=used_context,
            confidence=0.86,
        )
    return AgentPlan(goal="unknown", actions=[], used_context=used_context, confidence=0.55)


def _record_actions(text: str) -> list[dict[str, Any]]:
    pattern = re.compile(r"(?P<food>[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9_-]{0,20}?)\s*(?P<grams>\d+(?:\.\d+)?)\s*(?:g|G|克)?")
    actions: list[dict[str, Any]] = []
    for match in pattern.finditer(text):
        food = _clean_food(match.group("food"))
        if not food or food in {"今天", "查一下", "还剩"}:
            continue
        grams = float(match.group("grams"))
        actions.append(
            {
                "action_id": f"a{len(actions) + 1}",
                "tool": "record_food",
                "parameters": {
                    "food_query": food,
                    "grams": int(grams) if grams.is_integer() else grams,
                    "meal_type": "unknown",
                },
                "reason": "用户输入食物和克数",
                "safety": "write",
            }
        )
    return actions


def _clean_food(value: str) -> str:
    food = str(value or "").strip(" ，,。.;；:：")
    for prefix in ("今天", "吃了", "记录", "帮我", "查一下"):
        if food.startswith(prefix) and len(food) > len(prefix):
            food = food[len(prefix) :].strip(" ，,。.;；:：")
    return food


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def _is_small_talk(text: str) -> bool:
    return _contains_any(text, ("随便聊", "聊天", "你好")) and not _record_actions(text)


def _is_nutrition_query(text: str) -> bool:
    lower_text = text.lower()
    if _contains_any(text, ("记录", "记一下", "我吃了", "吃了")):
        return False
    return _contains_any(text, ("热量", "营养", "卡路里", "多少大卡", "大概多少")) or any(
        word in lower_text for word in ("kcal", "calorie", "calories", "nutrition")
    )


def _extract_food_query(text: str) -> str:
    value = str(text or "")
    value = re.sub(r"\d+(?:\.\d+)?\s*(?:g|G|克)", "", value)
    for token in (
        "大概多少热量",
        "多少热量",
        "多少大卡",
        "卡路里",
        "营养",
        "热量",
        "那条",
        "这条",
        "查询",
        "查一下",
        "帮我",
        "一下",
        "?",
        "？",
    ):
        value = value.replace(token, "")
    return value.strip(" ，,。.;；:：")


def _extract_grams(text: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)\s*(?:g|G|克)", text)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


__all__ = ["deterministic_planner", "build_plan", "_build_plan"]
