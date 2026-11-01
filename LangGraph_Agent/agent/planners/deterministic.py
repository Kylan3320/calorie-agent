"""Deterministic intents and a fully consumed food/quantity record grammar."""

from __future__ import annotations

import re
from typing import Any

from ..schemas import AgentAction, AgentPlan
from ..storage.timeutil import parse_grams_from_text

_MEAL_ALIASES = {
    "breakfast": ("早餐", "早饭", "早上", "上午"),
    "lunch": ("午餐", "午饭", "中午"),
    "dinner": ("晚餐", "晚饭", "晚上"),
    "post_workout": ("练后", "训练后", "运动后"),
    "snack": ("加餐", "零食", "点心", "下午茶", "宵夜"),
}
_MEALS = {alias: meal for meal, aliases in _MEAL_ALIASES.items() for alias in aliases}
_MEAL_RE = re.compile("|".join(sorted(_MEALS, key=len, reverse=True)))
_RECORD_VERBS = ("吃了", "吃", "记录", "录入", "摄入", "加上", "加了", "补记")
_PREFIX_RE = re.compile(r"(?:减脂期间|减脂期|增肌期间|增肌期|今天|今日|刚才|帮我|麻烦|请|我)")
_RECORD_RE = re.compile("|".join(_RECORD_VERBS))
_RECORD_SUFFIX_RE = re.compile(r"(?:帮我)?(?:记录|录入|补记)(?:一下)?[。.!！]*$")
_QUANTITY_RE = re.compile(r"(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>kg|公斤|千克|斤|公克|g|克)?", re.I)
_FOOD_QUANTITY_RE = re.compile(
    r"(?P<food>[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z（）()·._-]{0,40}?)\s*"
    + _QUANTITY_RE.pattern,
    re.I,
)
_TODAY_QUERY_RE = re.compile(
    r"(?:帮我|请|麻烦)?(?:再|然后|顺便)?(?:查一下|查下|查询|看看|看下)?"
    r"(?:今天|今日)(?:吃了什么|吃了多少|摄入多少|摄入情况|摄入|还剩多少|剩多少|汇总|饮食记录|记录)?"
    r"(?:热量|卡路里|营养)?[?？。.!！]*$"
)
_TODAY_SHORT_RE = re.compile(r"(?:还剩多少|剩多少|汇总|查一下)[?？。.!！]*$")
_UNDO_RE = re.compile(r"(?:帮我|请|麻烦)?(?:撤销|删除|撤回|去掉)(?:上一条|一条|刚才那条|那条|最后一条)?[。.!！]*$")
_NUTRITION_QUERY_WORDS = ("热量", "卡路里", "大卡", "营养", "多少千卡")
_HISTORY_WINDOW_WORDS = ("这周", "本周", "上周", "最近", "近一周", "近几天", "这个月", "本月", "上个月", "上月", "近30天", "昨天", "过去一周", "过去一个月", "一个月内", "一周内")
_HISTORY_ASK_WORDS = ("吃了几次", "吃了多少", "记录了几次", "多少次", "几次", "超标", "吃了什么", "摄入多少", "吃了哪些", "饮食记录", "汇总", "趋势", "报告")
_HISTORY_PERIOD_MAP = (("上周", "last_week"), ("这周", "this_week"), ("本周", "this_week"), ("这个月", "this_month"), ("本月", "this_month"), ("上个月", "last_month"), ("上月", "last_month"), ("近30", "last_30_days"), ("过去一个月", "last_30_days"), ("一个月内", "last_30_days"), ("昨天", "yesterday"), ("最近", "last_7_days"), ("近一周", "last_7_days"), ("一周内", "last_7_days"), ("过去一周", "last_7_days"), ("近几天", "last_7_days"))
_KNOWLEDGE_HINTS = (
    "能吃", "可以吃", "适合吃", "适合吗", "该吃", "会胖", "会瘦", "减肥", "减脂", "增肌",
    "升糖", "血糖", "健康吗", "好不好", "有好处", "坏处", "注意什么", "推荐", "替代", "代替",
    "怎么选", "怎么吃", "如何吃", "帮助吗", "有效吗", "多少合适", "吃多少", "喝多少",
    "几个", "哪些", "建议吃",
)


def build_plan(text: str) -> AgentPlan:
    text = str(text or "").strip()
    if not text:
        return AgentPlan("unknown", confidence=0.55)
    if _UNDO_RE.fullmatch(text):
        return AgentPlan("undo", [AgentAction("a1", "undo_last", safety="destructive")], needs_confirmation=True, confidence=0.86)

    items, asks_today = _extract_record_items(text)
    if items:
        actions = [AgentAction(f"a{i + 1}", "record_food", item, safety="write") for i, item in enumerate(items)]
        if asks_today:
            actions.append(AgentAction(f"a{len(actions) + 1}", "query_today"))
        return AgentPlan("record_and_query" if asks_today else "record_intake", actions, confidence=0.86)

    is_nutrition = any(word in text for word in _NUTRITION_QUERY_WORDS) or "kcal" in text.lower()
    if (
        not is_nutrition
        and not any(word in text for word in ("今天", "今日"))
        and any(word in text for word in _HISTORY_WINDOW_WORDS)
        and any(word in text for word in _HISTORY_ASK_WORDS)
    ):
        period = next((code for word, code in _HISTORY_PERIOD_MAP if word in text), "last_7_days")
        food = _extract_history_food(text)
        return AgentPlan("history_query", [AgentAction("a1", "query_history", {"period": period, **({"food_query": food} if food else {})})], confidence=0.82)
    if _is_today_query(text):
        return AgentPlan("query_today", [AgentAction("a1", "query_today")], confidence=0.86)
    if is_nutrition:
        food = _extract_nutrition_query_food(text)
        if food:
            params: dict[str, Any] = {"food_query": food}
            quantity = _QUANTITY_RE.search(text)
            if quantity:
                grams = parse_grams_from_text(quantity.group()) if quantity.group("unit") else None
                if grams is None or not 0 < grams <= 5000:
                    return AgentPlan("unknown")
                params["grams"] = grams
            return AgentPlan("nutrition_query", [AgentAction("a1", "query_food_nutrition", params)], confidence=0.88)
    if any(word in text for word in _KNOWLEDGE_HINTS):
        return AgentPlan("ask_knowledge", [AgentAction("a1", "query_knowledge", {"query": text})], confidence=0.8)
    if re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{2,12}", text):
        # The node verifies a bare name against the food library before using it.
        return AgentPlan("record_intake", [AgentAction("a1", "record_food", {"food_query": text, "meal_type": "unknown"}, safety="write")], confidence=0.6)
    return AgentPlan("unknown", confidence=0.55)


def _is_today_query(text: str) -> bool:
    return bool(_TODAY_QUERY_RE.fullmatch(text) or _TODAY_SHORT_RE.fullmatch(text))


def _extract_record_items(text: str) -> tuple[list[dict[str, Any]], bool]:
    rest = text
    meal = "unknown"
    explicit = False
    has_unit = False
    prefix_pending = False
    items: list[dict[str, Any]] = []
    while rest:
        rest = rest.lstrip(" \t\n,，、;；。.!！")
        if not rest:
            break
        if items and _RECORD_SUFFIX_RE.fullmatch(rest):
            explicit = True
            break
        if items and _is_today_query(rest):
            return items, True
        prefix = _PREFIX_RE.match(rest)
        meal_match = _MEAL_RE.match(rest)
        verb = _RECORD_RE.match(rest)
        if prefix:
            prefix_pending = True
            rest = rest[prefix.end():]
            continue
        if meal_match:
            meal = _MEALS[meal_match.group()]
            explicit = True
            prefix_pending = True
            rest = rest[meal_match.end():]
            continue
        if verb:
            explicit = True
            prefix_pending = True
            rest = rest[verb.end():]
            continue
        match = _FOOD_QUANTITY_RE.match(rest)
        if match is None:
            return [], False
        food = match.group("food")
        # An unconsumed command/negative clause cannot be treated as a food name.
        if food.startswith(("不", "没", "别")) or any(word in food for word in _RECORD_VERBS):
            return [], False
        quantity = match.group("amount") + (match.group("unit") or "")
        grams = parse_grams_from_text(quantity)
        if grams is None or not 0 < grams <= 5000:
            return [], False
        has_unit = has_unit or bool(match.group("unit"))
        items.append({"food_query": food, "grams": grams, "meal_type": meal})
        prefix_pending = False
        rest = rest[match.end():]
    if prefix_pending:
        return [], False
    if not explicit and not has_unit and len(items) < 2:
        return [], False
    return items, False


def _extract_nutrition_query_food(text: str) -> str:
    value = _QUANTITY_RE.sub("", text)
    for token in (*_NUTRITION_QUERY_WORDS, "kcal", "有多少", "多少", "查询", "查一下", "帮我", "一下", *_RECORD_VERBS, "今天", "今日", "的", "?", "？"):
        value = value.replace(token, "")
    return value.strip(" \t\r\n,，、;；:：。.!！?？")


def _extract_history_food(text: str) -> str:
    value = text
    for token in (*_HISTORY_WINDOW_WORDS, *_HISTORY_ASK_WORDS, "我", "的", "了", "有", "吗", "帮我", "一下", "看看", "查", "记录"):
        value = value.replace(token, " ")
    value = re.sub(r"[?？!！。,，、;；:：\s]+", " ", value).strip()
    parts = value.split()
    return parts[0] if len(parts) == 1 and re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{2,8}", parts[0]) else ""


__all__ = ["build_plan"]
