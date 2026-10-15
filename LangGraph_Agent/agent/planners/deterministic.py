"""Deterministic planner: rules + regex, no LLM.

Regex core ported from V2 planner.py (build_bare_number_food_plan), promoted
from fallback to first-class backend. Conservative on purpose: it only emits
record actions when a meal hint, record verb, a unit, or multiple items are
present, so ordinary chat never becomes an intake write.
"""

from __future__ import annotations

import re
from typing import Any

from ..schemas import PLAN_SCHEMA_VERSION, AgentPlan

_MEAL_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("breakfast", ("早餐", "早饭", "早上", "上午")),
    ("lunch", ("午餐", "午饭", "中午")),
    ("dinner", ("晚餐", "晚饭", "晚上")),
    ("post_workout", ("练后", "训练后", "运动后")),
    ("snack", ("加餐", "零食", "点心", "下午茶", "宵夜")),
)
_RECORD_VERBS = ("吃了", "吃", "记录", "录入", "摄入", "加上", "加了", "补记")
_MACRO_TERMS = {"碳水", "碳水化合物", "蛋白", "蛋白质", "脂肪", "热量", "卡路里", "大卡", "kcal"}
_TODAY_WORDS = ("今天", "今日", "还剩", "剩多少", "吃了什么", "汇总", "查一下", "摄入多少", "今日摄入", "今天摄入")
_UNDO_WORDS = ("撤销", "删除", "撤回", "去掉")
_UNDO_TARGET_WORDS = ("上一条", "一条", "刚才", "那条", "最后", "last")
_LEADING_NOISE = ("今天", "刚才", "我", "帮我", "麻烦", "请", "吃了", "吃", "记录", "录入", "摄入", "加上", "加了", "补记")
_BARE_NUMBER_FOOD_RE = re.compile(
    r"(?P<food>[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9（）()·._-]{0,40}?)\s*"
    r"(?P<grams>\d+(?:\.\d+)?)\s*(?P<unit>g|G|克)?"
)
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
    stripped = str(text or "").strip()
    if not stripped:
        return _unknown_plan()

    meal_type = _infer_meal_type(stripped)
    has_meal_hint = meal_type != "unknown"
    has_record_verb = any(word in stripped for word in _RECORD_VERBS)
    asks_today = any(word in stripped for word in _TODAY_WORDS)
    is_undo = any(word in stripped for word in _UNDO_WORDS) and (
        any(word in stripped for word in _UNDO_TARGET_WORDS) or stripped in {"撤销", "删除", "撤回"}
    )
    is_nutrition_query = (
        any(word in stripped for word in _NUTRITION_QUERY_WORDS) or "kcal" in stripped.lower()
    ) and not has_record_verb
    # 记录意图优先：只有当文本提不出任何可记录条目时，知识/历史提示词才接管路由。
    has_recordable_items = bool(_extract_record_items(stripped, meal_type))
    # 历史聚合（RAG M2）：时间窗 + 追问词；显式排除"今天/今日"（那些继续走 query_today）。
    # 判定用"提不出可记录条目"而不是记录动词——"这周吃了几次猪肉"含"吃"但不是记录指令。
    is_history_query = (
        not has_recordable_items
        and not any(word in stripped for word in ("今天", "今日"))
        and not is_nutrition_query
        and not re.search(r"\d+\s*(?:g|G|克)", stripped)
        and any(word in stripped for word in _HISTORY_WINDOW_WORDS)
        and any(word in stripped for word in _HISTORY_ASK_WORDS)
    )
    is_knowledge_question = (
        not has_recordable_items
        and not asks_today
        and not is_nutrition_query
        and not is_history_query
        and any(word in stripped for word in _KNOWLEDGE_HINTS)
    )

    if is_undo:
        return AgentPlan.from_dict(
            {
                "goal": "undo",
                "actions": [
                    {
                        "action_id": "a1",
                        "tool": "undo_last",
                        "parameters": {},
                        "reason": "用户请求撤销最近一条记录",
                        "safety": "destructive",
                    }
                ],
                "needs_confirmation": True,
                "confidence": 0.86,
            }
        )

    items = _extract_record_items(stripped, meal_type)
    if items:
        actions: list[dict[str, Any]] = [
            {
                "action_id": f"a{index + 1}",
                "tool": "record_food",
                "parameters": {
                    "food_query": item["food_query"],
                    "grams": item["grams"],
                    "meal_type": item["meal_type"],
                },
                "reason": "用户输入食物和克数",
                "safety": "write",
            }
            for index, item in enumerate(items)
        ]
        if asks_today:
            actions.append(
                {
                    "action_id": f"a{len(actions) + 1}",
                    "tool": "query_today",
                    "parameters": {},
                    "reason": "记录后查询今日汇总",
                    "safety": "read_only",
                    "depends_on": [action["action_id"] for action in actions],
                }
            )
        return AgentPlan.from_dict(
            {
                "goal": "record_and_query" if asks_today else "record_intake",
                "actions": actions,
                "needs_confirmation": False,
                "confidence": 0.86,
            }
        )

    if is_history_query:
        period = next((code for word, code in _HISTORY_PERIOD_MAP if word in stripped), "last_7_days")
        food_query = _extract_history_food(stripped)
        return AgentPlan.from_dict(
            {
                "goal": "history_query",
                "actions": [
                    {
                        "action_id": "a1",
                        "tool": "query_history",
                        "parameters": {"period": period, **({"food_query": food_query} if food_query else {})},
                        "reason": "用户查询历史摄入统计",
                        "safety": "read_only",
                    }
                ],
                "needs_confirmation": False,
                "confidence": 0.82,
            }
        )

    if asks_today:
        return AgentPlan.from_dict(
            {
                "goal": "query_today",
                "actions": [
                    {"action_id": "a1", "tool": "query_today", "parameters": {}, "reason": "用户查询今日摄入", "safety": "read_only"}
                ],
                "needs_confirmation": False,
                "confidence": 0.86,
            }
        )

    if is_nutrition_query:
        food_query = _extract_nutrition_query_food(stripped)
        if food_query:
            params: dict[str, Any] = {"food_query": food_query}
            grams_match = re.search(r"(\d+(?:\.\d+)?)\s*(?:g|G|克)", stripped)
            if grams_match:
                params["grams"] = float(grams_match.group(1))
            return AgentPlan.from_dict(
                {
                    "goal": "nutrition_query",
                    "actions": [
                        {
                            "action_id": "a1",
                            "tool": "query_food_nutrition",
                            "parameters": params,
                            "reason": "用户查询食物营养",
                            "safety": "read_only",
                        }
                    ],
                    "needs_confirmation": False,
                    "confidence": 0.88,
                }
            )

    if is_knowledge_question:
        return AgentPlan.from_dict(
            {
                "goal": "ask_knowledge",
                "actions": [
                    {
                        "action_id": "a1",
                        "tool": "query_knowledge",
                        "parameters": {"query": stripped},
                        "reason": "用户询问饮食知识",
                        "safety": "read_only",
                    }
                ],
                "needs_confirmation": False,
                "confidence": 0.8,
            }
        )

    bare_food = _looks_like_bare_food(stripped)
    if bare_food:
        return AgentPlan.from_dict(
            {
                "goal": "record_intake",
                "actions": [
                    {
                        "action_id": "a1",
                        "tool": "record_food",
                        "parameters": {"food_query": bare_food, "meal_type": "unknown"},
                        "reason": "用户输入食物但缺少克重",
                        "safety": "write",
                    }
                ],
                "needs_confirmation": False,
                "confidence": 0.6,
            }
        )

    return _unknown_plan()


def _looks_like_bare_food(text: str) -> str:
    """A short bare food name like "米饭" -> record with missing grams (pending ask)."""
    if not re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{2,6}", text):
        return ""
    blocked_words = (
        *_TODAY_WORDS,
        *_UNDO_WORDS,
        *_NUTRITION_QUERY_WORDS,
        *_RECORD_VERBS,
        "你好", "您好", "在吗", "谢谢", "感谢", "早安", "晚安", "好的", "嗯嗯", "哈哈", "请",
    )
    if any(word in text for word in blocked_words):
        return ""
    return text


def _unknown_plan() -> AgentPlan:
    return AgentPlan.from_dict({"goal": "unknown", "actions": [], "needs_confirmation": False, "confidence": 0.55})


def _extract_record_items(text: str, meal_type: str) -> list[dict[str, Any]]:
    has_meal_hint = meal_type != "unknown"
    has_record_verb = any(word in text for word in _RECORD_VERBS)
    if not has_meal_hint and not has_record_verb and any(term in text for term in _MACRO_TERMS):
        return []

    scan_text = _strip_meal_words(text)
    items: list[dict[str, Any]] = []
    for match in _BARE_NUMBER_FOOD_RE.finditer(scan_text):
        food_query = _clean_food_query(match.group("food"))
        if not food_query or food_query.lower() in _MACRO_TERMS:
            continue
        try:
            grams = float(match.group("grams"))
        except (TypeError, ValueError):
            continue
        if grams <= 0 or grams > 5000:
            continue
        items.append(
            {
                "food_query": food_query,
                "grams": int(grams) if grams.is_integer() else grams,
                "meal_type": meal_type,
                "has_unit": bool(match.group("unit")),
            }
        )

    if not items:
        return []
    if not has_meal_hint and not has_record_verb and len(items) < 2 and not items[0]["has_unit"]:
        return []
    return items


def _infer_meal_type(text: str) -> str:
    for meal_type, aliases in _MEAL_ALIASES:
        if any(alias in text for alias in aliases):
            return meal_type
    return "unknown"


def _strip_meal_words(text: str) -> str:
    cleaned = text
    for _meal_type, aliases in _MEAL_ALIASES:
        for alias in aliases:
            cleaned = cleaned.replace(alias, " ")
    return cleaned


def _clean_food_query(value: str) -> str:
    food = str(value or "").strip(" \t\r\n,，、;；:：。.!！?？")
    changed = True
    while changed:
        changed = False
        for prefix in _LEADING_NOISE:
            if food.startswith(prefix) and len(food) > len(prefix):
                food = food[len(prefix) :].strip(" \t\r\n,，、;；:：。.!！?？")
                changed = True
    return food


def _extract_nutrition_query_food(text: str) -> str:
    value = re.sub(r"\d+(?:\.\d+)?\s*(?:g|G|克)", "", text)
    for token in (
        *_NUTRITION_QUERY_WORDS,
        "多少",
        "查询",
        "查一下",
        "帮我",
        "一下",
        *_RECORD_VERBS,
        *_TODAY_WORDS,
        "?",
        "？",
    ):
        value = value.replace(token, "")
    return value.strip(" \t\r\n,，、;；:：。.!！?？")


def _extract_history_food(text: str) -> str:
    """Strip window/ask vocabulary; what remains (2-8 CJK chars) is the food filter."""
    value = str(text or "")
    for token in (*_HISTORY_WINDOW_WORDS, *_HISTORY_ASK_WORDS, "我", "的", "了", "有", "吗", "帮我", "一下", "看看", "查", "记录"):
        value = value.replace(token, " ")
    value = re.sub(r"[?？!！。,，、;；:：\s]+", " ", value).strip()
    parts = [part for part in value.split() if part]
    if len(parts) == 1 and re.fullmatch(r"[\u4e00-\u9fffA-Za-z]{2,8}", parts[0]):
        return parts[0]
    return ""


__all__ = ["build_plan"]
