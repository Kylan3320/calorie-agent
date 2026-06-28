from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Callable
from typing import Any

from calorie_agent.legacy_app import DEFAULT_SETTINGS

from .schemas import AgentPlan


HttpJsonPost = Callable[[str, dict[str, Any], dict[str, str] | None], dict[str, Any]]


AGENT_PLANNER_PROMPT_VERSION = "agent_planner.v2.1"


AGENT_PLANNER_SYSTEM_PROMPT = """
You are the planner for a Feishu calorie tracking agent.
Return valid JSON only. Do not include markdown.

You do not calculate nutrition, modify tables, or delete records. You only plan
safe tool calls. The Python tool executor will calculate, validate, write, and
delete.

Allowed tools:
- record_food: record one food intake item.
- query_today: query today's intake list and progress.
- undo_intake: undo/delete intake records.
- change_standard: update daily macro targets.
- search_food: find candidate foods.
- create_food: create a food after per-100g macros are provided.
- create_pending: create a follow-up task for missing grams/macros/confirmation.
- cancel_pending: cancel a follow-up task.
- clear_daily_intake: clear daily intake records, only for scheduled cleanup context.
- search_memory: read user food memories, aliases, combos, or preferences.
- suggest_default_grams: ask backend for a historical default grams suggestion.
- learn_alias: record a user-level alias learning candidate.
- confirm_alias: save an explicit user-level food alias.
- create_combo: save an explicit user-level food combo.
- use_combo: expand a saved combo through backend record_food actions.
- list_memories: list user memories.
- delete_memory: soft-delete a user memory, alias, combo, or preference.
- update_preference: update a low-risk user preference.
- generate_daily_report: generate a read-only daily intake report.
- generate_weekly_report: generate a read-only weekly intake report.
- generate_monthly_report: generate a read-only monthly intake report.
- query_trend: answer a read-only nutrition trend question.
- compare_period: compare two read-only intake periods.
- analyze_target_gap: analyze target hit rate and gaps for a period.
- get_report: read an existing cached report.
- regenerate_report: bypass report cache and regenerate a report.
- list_reminder_rules: show current reminder settings.
- update_reminder_rule: create or update a reminder preference.
- pause_reminders: pause proactive reminders for a bounded time.
- resume_reminders: resume proactive reminders.
- snooze_reminder: delay active reminders.
- generate_daily_summary_reminder: create a daily-summary reminder candidate.
- detect_anomalies: detect intake anomalies from backend facts.
- send_reminder: send a reminder only from backend reminder facts.

Return this JSON shape:
{
  "schema_version": "agent_plan.v1",
  "goal": "record_intake|query_today|semantic_undo|change_standard|pending|cleanup|reporting|trend|compare_period|target_analysis|unknown",
  "actions": [
    {
      "tool": "record_food|query_today|undo_intake|change_standard|search_food|create_food|create_pending|cancel_pending|clear_daily_intake|search_memory|suggest_default_grams|learn_alias|confirm_alias|create_combo|use_combo|list_memories|delete_memory|update_preference|generate_daily_report|generate_weekly_report|generate_monthly_report|query_trend|compare_period|analyze_target_gap|get_report|regenerate_report|list_reminder_rules|update_reminder_rule|pause_reminders|resume_reminders|snooze_reminder|generate_daily_summary_reminder|detect_anomalies|send_reminder",
      "params": {},
      "target": {},
      "reason": "short reason",
      "confidence": 0.0
    }
  ],
  "reply_style": "compact|confirm|explain",
  "needs_confirmation": false,
  "confidence": 0.0,
  "notes": ""
}

Planning rules:
- One user message may become multiple actions.
- For food records, use one record_food action per food item. Put food_query,
  grams, and meal_type in params when known.
- If a number immediately follows a food name in a record context, treat it as
  grams even when "g" or "克" is omitted. Example: "午餐 玉米粒270 盒马鸡腿170"
  should become two record_food actions: 玉米粒 270g and 盒马鸡腿 170g,
  both with meal_type="lunch".
- Example: "早餐鸡蛋100g，中午米饭200g鸡胸肉150g，查下今天还剩多少"
  should become three record_food actions in that semantic order, followed by
  one query_today action.
- Meal type values: breakfast, lunch, dinner, post_workout, snack, unknown.
- Convert jin/catty to grams: 1 jin = 500g. Convert kg to grams.
- If grams are missing, use create_pending with params.reason="missing_grams",
  suggest_default_grams, or record_food without grams. The backend will ask
  for confirmation before writing unless AUTO_USE_DEFAULT_GRAMS is enabled.
- If food macros are supplied for a new food, use create_food before record_food.
- If macros are missing for an unknown food, use create_pending with
  params.reason="missing_food".
- For "cancel this", "skip it", "先不管", or "取消这个", use cancel_pending
  or leave existing pending tasks untouched when the user is clearly starting
  a new task.
- For "撤销上一条", use undo_intake target.mode="ordinal_group" and ordinal=-1.
- For "撤销上上条", use undo_intake target.mode="ordinal_group" and ordinal=-2.
- For "撤销之前带西瓜的那条", use undo_intake target.mode="contains_food",
  food_query="西瓜", date_scope="today", group_scope="message_group".
- For explicit alias teaching like "from now on X means Y", use confirm_alias
  with params.alias=X and params.food_query=Y.
- For explicit combo teaching, use create_combo with params.combo_name,
  meal_type, and items.
- For "record/use usual breakfast", use use_combo with params.combo_name.
- For "do not remember this", "delete X alias", or "turn off auto learning",
  use delete_memory or update_preference. Do not delete intake records for
  memory-management requests.
- For daily summaries such as "today summary", "yesterday report", or a custom
  date summary, use generate_daily_report with params.period="today" or
  "yesterday", or params.date="YYYY-MM-DD".
- For weekly summaries, use generate_weekly_report with params.period set to
  "this_week", "last_week", or "last_7_days".
- For monthly summaries, use generate_monthly_report with params.period
  "this_month" or "last_month", or params.month="YYYY-MM".
- For trend questions, use query_trend with params.metric in
  "kcal|carbs|protein|fat" and an explicit period when possible.
- For comparison questions like "this week vs last week", use compare_period
  with params.period and params.compare_to.
- For target hit, gap, under, or over target questions, use analyze_target_gap
  with params.metric and params.period.
- For "regenerate report", use regenerate_report and include report_type when
  the requested report type is clear.
- For reminder preference requests, use reminder tools, not intake tools.
- For "关闭所有主动提醒", use update_reminder_rule with
  params.scope="all", params.enabled=false when the user clearly says all
  reminders.
- For "以后别提醒我午餐", use update_reminder_rule with
  params.reminder_type="meal_missing", params.meal_type="lunch",
  params.enabled=false.
- For "每天晚上九点半发总结", use update_reminder_rule with
  params.reminder_type="daily_summary", params.enabled=true,
  params.schedule_time="21:30".
- For "暂停一周提醒", use pause_reminders with params.days=7.
- For "恢复提醒", use resume_reminders.
- For "稍后提醒", use snooze_reminder with a short hours value.
- Dangerous or ambiguous deletes should set needs_confirmation=true and use
  reply_style="confirm".
- Never choose clear_daily_intake for a normal user chat message.
- If the context says event_type="scheduled_cleanup", use clear_daily_intake
  with target.before_date="today".
""".strip()


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _compact_json(payload: Any) -> str:
    return json.dumps(payload or {}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def default_http_json_post(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=_json_bytes(payload),
        headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} POST {url}: {body}") from exc
    parsed = json.loads(raw.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def build_agent_planner_messages(
    user_text: str,
    context: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    context_text = _compact_json(context)
    return [
        {"role": "system", "content": AGENT_PLANNER_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Context JSON:\n{context_text}\n\nUser message:\n{user_text}",
        },
    ]


def extract_deepseek_content(data: dict[str, Any]) -> str:
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


def parse_agent_plan_content(content: str) -> AgentPlan:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        start = content.find("{")
        end = content.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError(f"DeepSeek did not return an agent plan JSON object: {content}") from None
        try:
            parsed = json.loads(content[start : end + 1])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"DeepSeek agent plan JSON is invalid: {content}") from exc
    return AgentPlan.from_dict(parsed)


_MEAL_ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("breakfast", ("早餐", "早饭", "早上", "上午")),
    ("lunch", ("午餐", "午饭", "中午")),
    ("dinner", ("晚餐", "晚饭", "晚上")),
    ("post_workout", ("练后", "训练后", "运动后")),
    ("snack", ("加餐", "零食", "点心", "下午茶", "宵夜")),
)
_RECORD_VERBS = ("吃了", "吃", "记录", "录入", "摄入", "加上", "加了", "补记")
_MACRO_TERMS = {"碳水", "碳水化合物", "蛋白", "蛋白质", "脂肪", "热量", "卡路里", "大卡", "kcal"}
_LEADING_NOISE = ("今天", "刚才", "我", "帮我", "麻烦", "请", "吃了", "吃", "记录", "录入", "摄入", "加上", "加了", "补记")
_BARE_NUMBER_FOOD_RE = re.compile(
    r"(?P<food>[\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9（）()·._-]{0,40}?)\s*"
    r"(?P<grams>\d+(?:\.\d+)?)\s*(?P<unit>g|G|克)?"
)


def build_bare_number_food_plan(user_text: str) -> AgentPlan | None:
    """Build a conservative fallback plan for inputs like "午餐 玉米粒270 鸡腿170"."""
    text = str(user_text or "").strip()
    if not text:
        return None

    meal_type = _infer_meal_type(text)
    has_meal_hint = meal_type != "unknown"
    has_record_verb = any(word in text for word in _RECORD_VERBS)
    if not has_meal_hint and not has_record_verb and any(term in text for term in _MACRO_TERMS):
        return None

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
        return None
    if not has_meal_hint and not has_record_verb and len(items) < 2 and not items[0]["has_unit"]:
        return None

    return AgentPlan.from_dict(
        {
            "schema_version": "agent_plan.v1",
            "goal": "record_intake",
            "actions": [
                {
                    "tool": "record_food",
                    "params": {
                        "food_query": item["food_query"],
                        "grams": item["grams"],
                        "meal_type": item["meal_type"],
                    },
                    "reason": "deterministic fallback: food name followed by grams",
                    "confidence": 0.82,
                }
                for item in items
            ],
            "reply_style": "compact",
            "needs_confirmation": False,
            "confidence": 0.82,
            "notes": "bare_number_grams_fallback",
        }
    )


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


def plan_user_message_with_deepseek(
    user_text: str,
    settings: dict[str, str],
    context: dict[str, Any] | None = None,
    http_json_post: HttpJsonPost = default_http_json_post,
) -> AgentPlan:
    api_key = settings.get("deepseek_api_key") or os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured.")

    api_base = (
        settings.get("deepseek_api_base") or os.getenv("DEEPSEEK_API_BASE") or DEFAULT_SETTINGS["DEEPSEEK_API_BASE"]
    ).rstrip("/")
    model = settings.get("deepseek_model") or os.getenv("DEEPSEEK_MODEL") or DEFAULT_SETTINGS["DEEPSEEK_MODEL"]
    payload = {
        "model": model,
        "messages": build_agent_planner_messages(user_text, context),
        "temperature": 0,
        "max_tokens": 1200,
        "stream": False,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
    }
    data = http_json_post(
        f"{api_base}/chat/completions",
        payload,
        {"Authorization": f"Bearer {api_key}"},
    )
    if "error" in data:
        raise RuntimeError(f"DeepSeek API error: {data['error']}")
    plan = parse_agent_plan_content(extract_deepseek_content(data))
    if not plan.actions:
        fallback_plan = build_bare_number_food_plan(user_text)
        if fallback_plan is not None:
            return fallback_plan
    return plan
