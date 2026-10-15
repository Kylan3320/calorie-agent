"""Read-only tools: query_today, query_food_nutrition, query_knowledge, query_history.

ToolResults carry facts only (contracts/tool_protocol.md); reply text is
composed by nodes/reply_composer. action_id is stamped by the executor.
"""

from __future__ import annotations

import datetime as _dt
from typing import Any

from ..schemas import ToolResult
from ..storage.food_match import normalize_food_name
from ..storage.knowledge_retriever import build_knowledge_retriever
from ..storage.timeutil import round_number
from .base import ToolContext, calculate_food_intake, match_food

PERIOD_LABELS = {
    "today": "今天",
    "yesterday": "昨天",
    "this_week": "本周",
    "last_week": "上周",
    "this_month": "本月",
    "last_month": "上月",
    "last_7_days": "最近7天",
    "last_30_days": "最近30天",
}


def run_query_today(message: dict[str, str], params: dict[str, Any], ctx: ToolContext) -> ToolResult:
    user_id = message.get("user_id", "")
    summary = ctx.store.today_summary(user_id)
    standard = ctx.store.get_standard(user_id)
    return ToolResult("query_today", "ok", {"today_summary": summary, "daily_standard": standard})


def run_query_food_nutrition(message: dict[str, str], params: dict[str, Any], ctx: ToolContext) -> ToolResult:
    food_query = str(params.get("food_query") or params.get("food_name") or "").strip()
    if not food_query:
        return ToolResult("query_food_nutrition", "error", error="food_query_required")
    grams = _grams_or_none(params.get("grams"))
    user_id = message.get("user_id", "")

    stored = _query_stored_intake(ctx, food_query, user_id)
    if stored is not None:
        return ToolResult("query_food_nutrition", "ok", stored)

    match = match_food(ctx, food_query)
    if match.get("status") == "needs_confirmation":
        return ToolResult(
            "query_food_nutrition",
            "needs_confirmation",
            {"mode": "ambiguous", "food_query": food_query, "candidates": match.get("candidates", [])},
        )
    if match.get("status") != "auto_matched":
        return ToolResult("query_food_nutrition", "not_found", {"mode": "not_found", "food_query": food_query})

    candidate = match.get("candidate") if isinstance(match.get("candidate"), dict) else {}
    food = candidate.get("food") if isinstance(candidate.get("food"), dict) else {}
    grams = grams or 100.0
    nutrition = calculate_food_intake(food, grams)
    return ToolResult(
        "query_food_nutrition",
        "ok",
        {
            "mode": "food_database",
            "food_query": food_query,
            "food_name": str(food.get("name") or food_query),
            "grams": round_number(grams),
            "nutrition": nutrition,
        },
    )


def run_query_knowledge(message: dict[str, str], params: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Dietary knowledge Q&A via the knowledge_retriever seam (contracts/knowledge_schema.md).

    Answers are returned verbatim from the retrieved entry (never model-written);
    evidence travels in data so reply_guard's number tracing covers knowledge text.
    """
    query = str(params.get("query") or message.get("text") or "").strip()
    if not query:
        return ToolResult("query_knowledge", "not_found", {"status": "not_found", "entries": [], "evidence": "", "query": ""})
    kind = str(getattr(ctx.config, "knowledge_retriever", "") or "faq")
    try:
        retriever = build_knowledge_retriever(kind)
        result = retriever(query, ctx.store.load_knowledge())
    except ValueError as exc:
        return ToolResult("query_knowledge", "error", error=f"knowledge_retriever_config:{exc}")
    result = {**result, "query": query, "retriever": kind}
    return ToolResult("query_knowledge", str(result.get("status") or "not_found"), result)


def run_query_history(message: dict[str, str], params: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Historical aggregation (RAG M2): time window + optional food filter.

    The period string is resolved here against the configured timezone so
    planner backends only emit coarse vocabulary (contracts/tool_protocol.md).
    """
    period = str(params.get("period") or "last_7_days").strip()
    food_query = str(params.get("food_query") or params.get("food_name") or "").strip()
    # 时段窗口锚定 store 的"今天"（contracts/tool_protocol.md）：与记录/查询同一日期口径，
    # 评测可注入固定日期，不受真实时钟推移影响
    today = _dt.datetime.strptime(ctx.store.today_string(), "%Y-%m-%d").date()

    def day_str(d: _dt.date) -> str:
        return d.strftime("%Y-%m-%d")

    start_of_week = today - _dt.timedelta(days=today.weekday())
    windows = {
        "today": (today, today + _dt.timedelta(days=1)),
        "yesterday": (today - _dt.timedelta(days=1), today),
        "this_week": (start_of_week, start_of_week + _dt.timedelta(days=7)),
        "last_week": (start_of_week - _dt.timedelta(days=7), start_of_week),
        "this_month": (today.replace(day=1), (today.replace(day=28) + _dt.timedelta(days=4)).replace(day=1)),
        "last_month": ((today.replace(day=28) - _dt.timedelta(days=31)).replace(day=1), today.replace(day=1)),
        "last_7_days": (today - _dt.timedelta(days=6), today + _dt.timedelta(days=1)),
        "last_30_days": (today - _dt.timedelta(days=29), today + _dt.timedelta(days=1)),
    }
    if period not in windows:
        period = "last_7_days"
    start, end = windows[period]
    summary = ctx.store.summarize_history(
        message.get("user_id", ""),
        day_str(start),
        day_str(end),
        food_query,
    )
    return ToolResult(
        "query_history",
        "ok",
        {"period": period, "period_label": PERIOD_LABELS[period], "summary": summary},
    )


def _query_stored_intake(ctx: ToolContext, food_query: str, user_id: str) -> dict[str, Any] | None:
    summary = ctx.store.today_summary(user_id)
    items = summary.get("items") if isinstance(summary.get("items"), list) else []
    normalized = normalize_food_name(food_query)
    if not normalized:
        return None
    matched = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = normalize_food_name(item.get("food_name"))
        if name and (name == normalized or name in normalized or normalized in name):
            matched.append(item)
    if not matched:
        return None
    total_grams = 0.0
    totals = {"kcal": 0.0, "carbs": 0.0, "protein": 0.0, "fat": 0.0}
    for item in matched:
        total_grams += float(item.get("grams") or 0)
        nutrition = item.get("nutrition") if isinstance(item.get("nutrition"), dict) else {}
        for key in totals:
            totals[key] = round_number(totals[key] + float(nutrition.get(key) or 0))
    return {
        "mode": "stored_intake",
        "food_query": food_query,
        "matched_count": len(matched),
        "matched_items": [
            {"food_name": item.get("food_name", ""), "grams": item.get("grams", 0), "nutrition": item.get("nutrition", {})}
            for item in matched
        ],
        "total": {"grams": round_number(total_grams), **totals},
    }


def _grams_or_none(value: Any) -> float | None:
    try:
        grams = float(value)
    except (TypeError, ValueError):
        return None
    return grams if grams > 0 else None


__all__ = ["run_query_food_nutrition", "run_query_history", "run_query_knowledge", "run_query_today"]
