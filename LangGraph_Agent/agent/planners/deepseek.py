"""DeepSeek planner: raw HTTP call + strict plan validation (ported from V2 planner.py).

No langchain-openai: one HTTP POST with response_format=json_object, then
AgentPlan.from_dict enforces the tool whitelist. Caller falls back to the
deterministic planner on any failure.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from ..schemas import PLAN_SCHEMA_VERSION, AgentPlan

HttpJsonPost = Callable[[str, dict[str, Any], dict[str, str] | None], dict[str, Any]]

PLANNER_PROMPT_VERSION = "calorie_agent_planner.v4"

SYSTEM_PROMPT = """
You are the planner for a single-user Feishu calorie tracking agent.
Return valid JSON only. No markdown. You never calculate nutrition and never
execute tools; the backend tool executor does all reads and writes.

Allowed tools:
- record_food: record one food item. parameters: {food_query, grams?, meal_type?}
- query_today: query today's intake summary and remaining budget. parameters: {}
- query_food_nutrition: nutrition of one food (today's logged snapshot first, then food database). parameters: {food_query, grams?}
- undo_last: soft-delete the most recent valid intake record. parameters: {}
- query_knowledge: dietary knowledge Q&A from the curated knowledge base (diet advice, food suitability, portion guidance). parameters: {query}
- query_history: aggregate intake over a past time window ("这周吃了几次猪肉", "上周吃了什么", "最近超标了吗"). parameters: {period, food_query?} where period is one of: today, yesterday, this_week, last_week, this_month, last_month, last_7_days, last_30_days. Note "今天吃了什么" still uses query_today, not this.

Return this JSON shape:
{
  "schema_version": "agent_plan.v4",
  "goal": "record_intake|query_today|nutrition_query|undo|ask_knowledge|history_query|unknown",
  "actions": [
    {
      "action_id": "a1",
      "tool": "record_food|query_today|query_food_nutrition|undo_last|query_knowledge|query_history",
      "parameters": {},
      "reason": "short reason",
      "safety": "read_only|write|destructive",
      "depends_on": []
    }
  ],
  "needs_confirmation": false,
  "confidence": 0.9
}

Planning rules:
- One message may become multiple actions in semantic order.
- Use one record_food action per food item. Example: "早餐鸡蛋100g，中午米饭200g鸡胸肉150g，
  查下今天还剩多少" becomes three record_food actions then one query_today.
- If a number immediately follows a food name in a record context, treat it as grams
  even when "g" or "克" is omitted.
- Convert jin/catty to grams (1 jin = 500g), kg to grams (x1000).
- Missing grams: omit grams entirely. The backend will ask the user; never guess grams.
- Meal types: breakfast, lunch, dinner, post_workout, snack, unknown. Map
  早餐/午餐/晚餐/练后/加餐 accordingly.
- 撤销/删除上一条 -> undo_last with needs_confirmation=true.
- Nutrition questions (热量/营养/卡路里) about a specific food -> query_food_nutrition.
- Diet-advice or suitability questions (能吃吗/适合吗/会胖吗/怎么吃/吃多少合适/一天吃几个/
  健康吗/有帮助吗) -> query_knowledge with the user's original question as query.
  Examples: "减脂期间能吃米饭吗" -> query_knowledge; "一天吃几个鸡蛋" -> query_knowledge;
  "晚上吃东西会胖吗" -> query_knowledge.
- Past-window aggregation ("这周吃了几次猪肉" / "上周吃了什么" / "最近有没有超标") ->
  query_history {period, food_query?}. Map 这周/本周=this_week, 上周=last_week,
  这个月/本月=this_month, 上个月/上月=last_month, 昨天=yesterday, 最近/近几天=last_7_days.
  Include food_query only when the question names a specific food.
- Small talk, greetings, or anything unrelated to eating records -> return no actions
  and goal="unknown".
- safety values: record_food=write, undo_last=destructive, others read_only.
""".strip()


def plan_with_deepseek(
    user_text: str,
    context: dict[str, Any] | None = None,
    *,
    model: str = "",
    http_json_post: HttpJsonPost | None = None,
) -> AgentPlan:
    poster = http_json_post or _default_http_json_post
    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured.")
    api_base = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com").rstrip("/")
    payload = {
        "model": model or os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"Context JSON:\n{_compact_json(context or {})}\n\nUser message:\n{user_text}",
            },
        ],
        "temperature": 0,
        "max_tokens": 800,
        "stream": False,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
    }
    data = poster(f"{api_base}/chat/completions", payload, {"Authorization": f"Bearer {api_key}"})
    if "error" in data:
        raise RuntimeError(f"DeepSeek API error: {data['error']}")
    plan = _parse_plan_content(_extract_content(data))
    if plan.schema_version != PLAN_SCHEMA_VERSION:
        plan = AgentPlan.from_dict({**plan.to_dict(), "schema_version": PLAN_SCHEMA_VERSION})
    return plan


def _compact_json(payload: Any) -> str:
    return json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _default_http_json_post(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} POST {url}: {body}") from exc
    parsed = json.loads(raw.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def _extract_content(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"DeepSeek response does not include choices: {data}")
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        raise RuntimeError(f"DeepSeek choice does not include message: {data}")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"DeepSeek message content is empty: {data}")
    return content.strip()


def _parse_plan_content(content: str) -> AgentPlan:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError(f"DeepSeek did not return a plan JSON object: {content}") from None
        try:
            parsed = json.loads(content[start : end + 1])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"DeepSeek plan JSON is invalid: {content}") from exc
    return AgentPlan.from_dict(parsed)


__all__ = ["PLANNER_PROMPT_VERSION", "SYSTEM_PROMPT", "plan_with_deepseek"]
