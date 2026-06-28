import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


SERVICE_NAME = "calorie-agent-api"

DEFAULT_SETTINGS = {
    "APP_ENV": "production",
    "APP_TIMEZONE_OFFSET_HOURS": "8",
    "FEISHU_API_BASE": "https://open.feishu.cn/open-apis",
    "DEEPSEEK_API_BASE": "https://api.deepseek.com",
    "DEEPSEEK_MODEL": "deepseek-v4-flash",
    "PENDING_TASK_TTL_HOURS": "24",
    "AGENT_RUNTIME_ENABLED": "true",
    "AGENT_REPLY_MODE": "free",
    "AGENT_FALLBACK_TO_LEGACY": "true",
    "AGENT_MAX_ACTIONS": "8",
    "AGENT_MAX_ROUNDS": "3",
    "AGENT_CONFIRM_DESTRUCTIVE_ACTIONS": "true",
    "AGENT_REPLY_VALIDATE": "true",
    "AGENT_REPLY_GUARD_V2_ENABLED": "true",
    "AGENT_REPLY_GUARD_STRICT_NUMBERS": "true",
    "AGENT_REPLY_GUARD_ALLOW_ROUNDING": "true",
    "AGENT_REPLY_GUARD_BLOCK_MEDICAL_CLAIMS": "true",
    "AGENT_REPLY_GUARD_DEBUG": "false",
    "AGENT_RUNTIME_V2_ENABLED": "false",
    "AGENT_RUNTIME_V2_SHADOW": "false",
    "AGENT_RUNTIME_V2_READ_ONLY_TOOLS": "false",
    "AGENT_RUNTIME_V2_WRITE_TOOLS": "false",
    "AGENT_RUNTIME_V2_DESTRUCTIVE_TOOLS": "false",
    "AGENT_RUNTIME_V2_FALLBACK_TO_V1": "true",
    "AGENT_RUNTIME_V2_COMPARE_WITH_V1": "true",
    "AGENT_RUNTIME_V2_SAMPLE_RATE": "1.0",
    "AGENT_RUNTIME_V2_ALLOWED_TOOLS": "query_intake_food_nutrition,query_today,generate_daily_report,generate_weekly_report,query_trend,compare_period",
    "AGENT_RUNTIME_V2_MAX_SKILLS": "5",
    "AGENT_RUNTIME_V2_CONTEXT_BUDGET_CHARS": "8000",
    "AGENT_RUNTIME_V2_SKILL_DIR": "calorie_agent/skills",
    "AGENT_RUNTIME_V2_SKILL_CONTEXT_BUDGET_CHARS": "6000",
    "AGENT_RUNTIME_V2_INCLUDE_MEMORY": "true",
    "AGENT_RUNTIME_V2_INCLUDE_RETRIEVED_FACTS": "true",
    "AGENT_RUNTIME_V2_INCLUDE_SHORT_TERM": "true",
    "AGENT_RUNTIME_V2_CONTEXT_DEBUG": "false",
    "PLANNER_PROFILE_ENABLED": "true",
    "PLANNER_PROFILE_MAX_ALIASES": "20",
    "PLANNER_PROFILE_MAX_PATTERNS": "10",
    "PLANNER_PROFILE_MIN_CONFIDENCE": "0.7",
    "PLANNER_PROFILE_INCLUDE_TRACE_STATS": "true",
    "PLANNER_PROFILE_DEBUG": "false",
    "MEMORY_LEARNER_ENABLED": "true",
    "MEMORY_LEARNER_MIN_CONFIDENCE": "0.8",
    "MEMORY_LEARNER_REQUIRE_CONFIRM_ALIAS": "true",
    "MEMORY_LEARNER_DEFAULT_GRAMS_MIN_SAMPLES": "3",
    "MEMORY_LEARNER_AUTO_CONFIRM_EXPLICIT": "true",
    "MEMORY_LEARNER_DEBUG": "false",
    "DAILY_CLEAR_ENABLED": "true",
    "REPORTING_ENABLED": "true",
    "REPORT_CACHE_ENABLED": "true",
    "REPORT_AUTO_DAILY_ENABLED": "false",
    "REPORT_AUTO_WEEKLY_ENABLED": "false",
    "REPORT_AUTO_MONTHLY_ENABLED": "false",
    "REPORT_TIMEZONE_OFFSET_HOURS": "8",
    "REPORT_KCAL_HIT_MIN_RATIO": "0.9",
    "REPORT_KCAL_HIT_MAX_RATIO": "1.1",
    "REPORT_PROTEIN_HIT_MIN_RATIO": "0.9",
    "REPORT_MACRO_HIT_MIN_RATIO": "0.8",
    "REPORT_MACRO_HIT_MAX_RATIO": "1.2",
    "PERSONAL_MEMORY_ENABLED": "true",
    "AUTO_ALIAS_LEARNING_ENABLED": "true",
    "AUTO_DEFAULT_GRAMS_ENABLED": "true",
    "AUTO_USE_DEFAULT_GRAMS": "false",
    "AUTO_COMBO_LEARNING_ENABLED": "true",
    "MEMORY_MIN_SAMPLE_COUNT": "3",
    "MEMORY_LOOKBACK_DAYS": "30",
    "MEMORY_CONFIDENCE_THRESHOLD": "0.85",
    "REMINDER_ENABLED": "true",
    "REMINDER_DEFAULT_ENABLED": "false",
    "REMINDER_MAX_PER_DAY": "2",
    "REMINDER_QUIET_HOURS_START": "23:00",
    "REMINDER_QUIET_HOURS_END": "08:00",
    "REMINDER_PENDING_AFTER_HOURS": "6",
    "REMINDER_PENDING_MAX_TIMES": "2",
    "REMINDER_DAILY_SUMMARY_TIME": "21:30",
    "ANOMALY_DETECTION_ENABLED": "true",
    "ANOMALY_MEAL_KCAL_RATIO": "1.8",
    "ANOMALY_FOOD_GRAMS_RATIO": "2.5",
    "ANOMALY_DAILY_KCAL_MAX_RATIO": "1.25",
    "TARGET_GAP_PROTEIN_MIN_GRAMS": "30",
    "TARGET_GAP_KCAL_MIN_REMAINING": "500",
    "BITABLE_FIELD_FOOD_ID": "food_id",
    "BITABLE_FIELD_FOOD_NAME": "food_name",
    "BITABLE_FIELD_ALIAS": "alias",
    "BITABLE_FIELD_CARBS": "carbs_per_100g",
    "BITABLE_FIELD_PROTEIN": "protein_per_100g",
    "BITABLE_FIELD_FAT": "fat_per_100g",
    "BITABLE_FIELD_STATUS": "status",
    "BITABLE_FIELD_INTAKE_USER_ID": "user_id",
    "BITABLE_FIELD_INTAKE_DATE": "date",
    "BITABLE_FIELD_INTAKE_DATE_VALUE_TYPE": "timestamp",
    "BITABLE_FIELD_INTAKE_MESSAGE_ID": "message_id",
    "BITABLE_FIELD_INTAKE_RAW_TEXT": "raw_text",
    "BITABLE_FIELD_INTAKE_MEAL_TYPE": "meal_type",
    "BITABLE_FIELD_INTAKE_FOOD_ID": "food_id",
    "BITABLE_FIELD_INTAKE_FOOD_NAME": "food_name_snapshot",
    "BITABLE_FIELD_INTAKE_GRAMS": "grams",
    "BITABLE_FIELD_INTAKE_CARBS": "carbs",
    "BITABLE_FIELD_INTAKE_PROTEIN": "protein",
    "BITABLE_FIELD_INTAKE_FAT": "fat",
    "BITABLE_FIELD_INTAKE_KCAL": "kcal",
    "BITABLE_FIELD_INTAKE_STATUS": "status",
    "BITABLE_FIELD_INTAKE_CREATED_AT": "created_at",
    "BITABLE_FIELD_STANDARD_USER_ID": "user_id",
    "BITABLE_FIELD_STANDARD_TYPE": "standard_type",
    "BITABLE_FIELD_STANDARD_DATE": "date",
    "BITABLE_FIELD_STANDARD_KCAL": "kstandard",
    "BITABLE_FIELD_STANDARD_CARBS": "cstandard",
    "BITABLE_FIELD_STANDARD_PROTEIN": "pstandard",
    "BITABLE_FIELD_STANDARD_FAT": "fstandard",
    "BITABLE_FIELD_PENDING_ID": "pending_id",
    "BITABLE_FIELD_PENDING_USER_ID": "user_id",
    "BITABLE_FIELD_PENDING_CHAT_ID": "chat_id",
    "BITABLE_FIELD_PENDING_SOURCE_MESSAGE_ID": "source_message_id",
    "BITABLE_FIELD_PENDING_INTENT": "intent",
    "BITABLE_FIELD_PENDING_FOOD_NAME": "food_name",
    "BITABLE_FIELD_PENDING_GRAMS": "grams",
    "BITABLE_FIELD_PENDING_MEAL_TYPE": "meal_type",
    "BITABLE_FIELD_PENDING_RAW_TEXT": "raw_text",
    "BITABLE_FIELD_PENDING_STATUS": "status",
    "BITABLE_FIELD_PENDING_CREATED_AT": "created_at",
    "BITABLE_FIELD_PENDING_EXPIRES_AT": "expires_at",
    "BITABLE_FIELD_TRACE_ID": "trace_id",
    "BITABLE_FIELD_TRACE_MESSAGE_ID": "message_id",
    "BITABLE_FIELD_TRACE_USER_ID": "user_id",
    "BITABLE_FIELD_TRACE_RAW_TEXT": "raw_text",
    "BITABLE_FIELD_TRACE_PLANNER_PROMPT_VERSION": "planner_prompt_version",
    "BITABLE_FIELD_TRACE_PLAN_JSON": "plan_json",
    "BITABLE_FIELD_TRACE_ACTION_COUNT": "action_count",
    "BITABLE_FIELD_TRACE_TOOL_RESULTS": "tool_results",
    "BITABLE_FIELD_TRACE_EXECUTION_FACTS": "execution_facts",
    "BITABLE_FIELD_TRACE_FINAL_REPLY": "final_reply",
    "BITABLE_FIELD_TRACE_FALLBACK_USED": "fallback_used",
    "BITABLE_FIELD_TRACE_ERROR_TYPE": "error_type",
    "BITABLE_FIELD_TRACE_LATENCY_MS": "latency_ms",
    "BITABLE_FIELD_TRACE_CREATED_AT": "created_at",
    "BITABLE_FIELD_REPORT_ID": "report_id",
    "BITABLE_FIELD_REPORT_USER_ID": "user_id",
    "BITABLE_FIELD_REPORT_TYPE": "report_type",
    "BITABLE_FIELD_REPORT_PERIOD_START": "period_start",
    "BITABLE_FIELD_REPORT_PERIOD_END": "period_end",
    "BITABLE_FIELD_REPORT_SUMMARY_TEXT": "summary_text",
    "BITABLE_FIELD_REPORT_FACTS_JSON": "facts_json",
    "BITABLE_FIELD_REPORT_STATUS": "status",
    "BITABLE_FIELD_REPORT_GENERATED_AT": "generated_at",
    "BITABLE_FIELD_REPORT_SOURCE": "source",
    "BITABLE_FIELD_REPORT_VERSION": "version",
    "BITABLE_FIELD_MEMORY_ID": "memory_id",
    "BITABLE_FIELD_MEMORY_USER_ID": "user_id",
    "BITABLE_FIELD_MEMORY_FOOD_ID": "food_id",
    "BITABLE_FIELD_MEMORY_FOOD_NAME": "food_name",
    "BITABLE_FIELD_MEMORY_ALIAS": "alias",
    "BITABLE_FIELD_MEMORY_DEFAULT_GRAMS": "default_grams",
    "BITABLE_FIELD_MEMORY_MEAL_TYPE": "meal_type",
    "BITABLE_FIELD_MEMORY_SAMPLE_COUNT": "sample_count",
    "BITABLE_FIELD_MEMORY_CONFIDENCE": "confidence",
    "BITABLE_FIELD_MEMORY_SOURCE": "source",
    "BITABLE_FIELD_MEMORY_SOURCE_MESSAGE_ID": "source_message_id",
    "BITABLE_FIELD_MEMORY_STATUS": "status",
    "BITABLE_FIELD_MEMORY_CREATED_AT": "created_at",
    "BITABLE_FIELD_MEMORY_UPDATED_AT": "updated_at",
    "BITABLE_FIELD_COMBO_ID": "combo_id",
    "BITABLE_FIELD_COMBO_USER_ID": "user_id",
    "BITABLE_FIELD_COMBO_NAME": "combo_name",
    "BITABLE_FIELD_COMBO_MEAL_TYPE": "meal_type",
    "BITABLE_FIELD_COMBO_ITEMS_JSON": "items_json",
    "BITABLE_FIELD_COMBO_USE_COUNT": "use_count",
    "BITABLE_FIELD_COMBO_CONFIDENCE": "confidence",
    "BITABLE_FIELD_COMBO_STATUS": "status",
    "BITABLE_FIELD_COMBO_CREATED_AT": "created_at",
    "BITABLE_FIELD_COMBO_UPDATED_AT": "updated_at",
    "BITABLE_FIELD_PREFERENCE_ID": "preference_id",
    "BITABLE_FIELD_PREFERENCE_USER_ID": "user_id",
    "BITABLE_FIELD_PREFERENCE_KEY": "key",
    "BITABLE_FIELD_PREFERENCE_VALUE": "value",
    "BITABLE_FIELD_PREFERENCE_CONFIDENCE": "confidence",
    "BITABLE_FIELD_PREFERENCE_STATUS": "status",
    "BITABLE_FIELD_PREFERENCE_CREATED_AT": "created_at",
    "BITABLE_FIELD_PREFERENCE_UPDATED_AT": "updated_at",
    "BITABLE_FIELD_REMINDER_RULE_ID": "reminder_rule_id",
    "BITABLE_FIELD_REMINDER_RULE_USER_ID": "user_id",
    "BITABLE_FIELD_REMINDER_RULE_TYPE": "reminder_type",
    "BITABLE_FIELD_REMINDER_RULE_ENABLED": "enabled",
    "BITABLE_FIELD_REMINDER_RULE_SCHEDULE_TIME": "schedule_time",
    "BITABLE_FIELD_REMINDER_RULE_MEAL_TYPE": "meal_type",
    "BITABLE_FIELD_REMINDER_RULE_THRESHOLD_JSON": "threshold_json",
    "BITABLE_FIELD_REMINDER_RULE_QUIET_HOURS_START": "quiet_hours_start",
    "BITABLE_FIELD_REMINDER_RULE_QUIET_HOURS_END": "quiet_hours_end",
    "BITABLE_FIELD_REMINDER_RULE_MAX_PER_DAY": "max_per_day",
    "BITABLE_FIELD_REMINDER_RULE_STATUS": "status",
    "BITABLE_FIELD_REMINDER_RULE_CHAT_ID": "chat_id",
    "BITABLE_FIELD_REMINDER_RULE_ALLOW_GROUP": "allow_group",
    "BITABLE_FIELD_REMINDER_RULE_PAUSED_UNTIL": "paused_until",
    "BITABLE_FIELD_REMINDER_RULE_CREATED_AT": "created_at",
    "BITABLE_FIELD_REMINDER_RULE_UPDATED_AT": "updated_at",
    "BITABLE_FIELD_REMINDER_EVENT_ID": "reminder_event_id",
    "BITABLE_FIELD_REMINDER_EVENT_USER_ID": "user_id",
    "BITABLE_FIELD_REMINDER_EVENT_RULE_ID": "rule_id",
    "BITABLE_FIELD_REMINDER_EVENT_TYPE": "reminder_type",
    "BITABLE_FIELD_REMINDER_EVENT_TRIGGER_REASON": "trigger_reason",
    "BITABLE_FIELD_REMINDER_EVENT_FACTS_JSON": "facts_json",
    "BITABLE_FIELD_REMINDER_EVENT_MESSAGE_TEXT": "message_text",
    "BITABLE_FIELD_REMINDER_EVENT_STATUS": "status",
    "BITABLE_FIELD_REMINDER_EVENT_SENT_AT": "sent_at",
    "BITABLE_FIELD_REMINDER_EVENT_ACKNOWLEDGED_AT": "acknowledged_at",
    "BITABLE_FIELD_REMINDER_EVENT_SOURCE": "source",
    "BITABLE_FIELD_REMINDER_EVENT_CHAT_ID": "chat_id",
    "BITABLE_FIELD_REMINDER_EVENT_ERROR": "error",
    "BITABLE_FIELD_ANOMALY_ID": "anomaly_id",
    "BITABLE_FIELD_ANOMALY_USER_ID": "user_id",
    "BITABLE_FIELD_ANOMALY_TYPE": "anomaly_type",
    "BITABLE_FIELD_ANOMALY_SEVERITY": "severity",
    "BITABLE_FIELD_ANOMALY_RELATED_RECORD_ID": "related_record_id",
    "BITABLE_FIELD_ANOMALY_RELATED_MESSAGE_ID": "related_message_id",
    "BITABLE_FIELD_ANOMALY_FACTS_JSON": "facts_json",
    "BITABLE_FIELD_ANOMALY_STATUS": "status",
    "BITABLE_FIELD_ANOMALY_CREATED_AT": "created_at",
}


def _env(key: str, default: str = "") -> str:
    return os.getenv(key, DEFAULT_SETTINGS.get(key, default))


FEISHU_API_BASE = _env("FEISHU_API_BASE").rstrip("/")
DEEPSEEK_API_BASE = _env("DEEPSEEK_API_BASE").rstrip("/")

SUPPORTED_INTENTS = {"add_food", "change_standard", "undo_last", "query_today", "unknown"}
SUPPORTED_MEAL_TYPES = {"breakfast", "lunch", "dinner", "post_workout", "unknown"}

PARSER_SYSTEM_PROMPT = """
You are a parser for a Feishu calorie tracking bot.
Return valid JSON only. Do not include markdown. Do not calculate calories or nutrition.

Supported intents:
- add_food: user wants to record one or more foods eaten.
- change_standard: user wants to change daily macro targets.
- undo_last: user wants to undo the previous valid intake record.
- query_today: user wants to query today's intake summary.
- unknown: user intent is unclear.

Return this JSON shape:
{
  "intent": "add_food|change_standard|undo_last|query_today|unknown",
  "date": "today",
  "meal_type": "breakfast|lunch|dinner|post_workout|unknown",
  "foods": [
    {"name": "food name in Chinese if possible", "grams": 200}
  ],
  "standard": {
    "carbs": null,
    "protein": null,
    "fat": null
  },
  "missing": [],
  "notes": ""
}

Rules:
- For add_food, extract all food items and grams. If grams are missing, set grams to null and add "grams" to missing.
- Convert jin/catty to grams: 1 jin = 500g. Convert kg to grams.
- If the user says breakfast/lunch/dinner/post workout or 早餐/午餐/晚餐/练后, map to breakfast/lunch/dinner/post_workout.
- For change_standard, fill standard.carbs/protein/fat when present. Do not calculate kcal.
- For undo phrases like undo previous, delete last, revoke last, 撤销上一条, 删除上一条, set intent to undo_last.
- For query phrases like today summary, what did I eat today, 今日摄入, 今日汇总, 今天吃了什么, set intent to query_today.
""".strip()

_TENANT_ACCESS_TOKEN = ""
_TENANT_ACCESS_TOKEN_EXPIRES_AT = 0.0
_MESSAGE_STATE_LOCK = threading.Lock()
_MESSAGE_STATE: dict[str, tuple[str, float]] = {}
_MESSAGE_STATE_TTL_SECONDS = 3600
_MESSAGE_PROCESSING_STALE_SECONDS = 15

FIELD_FALLBACKS = {
    "food_id": ["food_id", "食物ID", "食物 ID"],
    "food_name": ["food_name", "food", "name", "食物名称", "食物名", "食物"],
    "alias": ["alias", "aliases", "别名", "食物别名"],
    "carbs": ["carbs_per_100g", "carbs", "碳水/100g", "碳水 /100g", "碳水每100g", "碳水"],
    "protein": ["protein_per_100g", "protein", "蛋白质/100g", "蛋白质 /100g", "蛋白/100g", "蛋白质", "蛋白"],
    "fat": ["fat_per_100g", "fat", "脂肪/100g", "脂肪 /100g", "脂肪每100g", "脂肪"],
    "status": ["status", "状态"],
}

DEFAULT_DAILY_STANDARD = {
    "carbs": 250.0,
    "protein": 150.0,
    "fat": 70.0,
    "kcal": 2230.0,
}


def _settings() -> dict[str, str]:
    return {
        "app_env": _env("APP_ENV"),
        "timezone_offset_hours": _env("APP_TIMEZONE_OFFSET_HOURS"),
        "feishu_api_base": _env("FEISHU_API_BASE"),
        "feishu_app_id": _env("FEISHU_APP_ID"),
        "feishu_app_secret": _env("FEISHU_APP_SECRET"),
        "feishu_verification_token": _env("FEISHU_VERIFICATION_TOKEN"),
        "deepseek_api_key": _env("DEEPSEEK_API_KEY"),
        "deepseek_api_base": _env("DEEPSEEK_API_BASE"),
        "deepseek_model": _env("DEEPSEEK_MODEL"),
        "pending_task_ttl_hours": _env("PENDING_TASK_TTL_HOURS"),
        "rollover_task_token": _env("ROLLOVER_TASK_TOKEN"),
        "agent_runtime_enabled": _env("AGENT_RUNTIME_ENABLED"),
        "agent_reply_mode": _env("AGENT_REPLY_MODE"),
        "agent_fallback_to_legacy": _env("AGENT_FALLBACK_TO_LEGACY"),
        "agent_max_actions": _env("AGENT_MAX_ACTIONS"),
        "agent_max_rounds": _env("AGENT_MAX_ROUNDS"),
        "agent_confirm_destructive_actions": _env("AGENT_CONFIRM_DESTRUCTIVE_ACTIONS"),
        "agent_reply_validate": _env("AGENT_REPLY_VALIDATE"),
        "agent_reply_guard_v2_enabled": _env("AGENT_REPLY_GUARD_V2_ENABLED"),
        "agent_reply_guard_strict_numbers": _env("AGENT_REPLY_GUARD_STRICT_NUMBERS"),
        "agent_reply_guard_allow_rounding": _env("AGENT_REPLY_GUARD_ALLOW_ROUNDING"),
        "agent_reply_guard_block_medical_claims": _env("AGENT_REPLY_GUARD_BLOCK_MEDICAL_CLAIMS"),
        "agent_reply_guard_debug": _env("AGENT_REPLY_GUARD_DEBUG"),
        "agent_runtime_v2_enabled": _env("AGENT_RUNTIME_V2_ENABLED"),
        "agent_runtime_v2_shadow": _env("AGENT_RUNTIME_V2_SHADOW"),
        "agent_runtime_v2_read_only_tools": _env("AGENT_RUNTIME_V2_READ_ONLY_TOOLS"),
        "agent_runtime_v2_write_tools": _env("AGENT_RUNTIME_V2_WRITE_TOOLS"),
        "agent_runtime_v2_destructive_tools": _env("AGENT_RUNTIME_V2_DESTRUCTIVE_TOOLS"),
        "agent_runtime_v2_fallback_to_v1": _env("AGENT_RUNTIME_V2_FALLBACK_TO_V1"),
        "agent_runtime_v2_compare_with_v1": _env("AGENT_RUNTIME_V2_COMPARE_WITH_V1"),
        "agent_runtime_v2_sample_rate": _env("AGENT_RUNTIME_V2_SAMPLE_RATE"),
        "agent_runtime_v2_allowed_tools": _env("AGENT_RUNTIME_V2_ALLOWED_TOOLS"),
        "agent_runtime_v2_max_skills": _env("AGENT_RUNTIME_V2_MAX_SKILLS"),
        "agent_runtime_v2_context_budget_chars": _env("AGENT_RUNTIME_V2_CONTEXT_BUDGET_CHARS"),
        "agent_runtime_v2_skill_dir": _env("AGENT_RUNTIME_V2_SKILL_DIR"),
        "agent_runtime_v2_skill_context_budget_chars": _env("AGENT_RUNTIME_V2_SKILL_CONTEXT_BUDGET_CHARS"),
        "agent_runtime_v2_include_memory": _env("AGENT_RUNTIME_V2_INCLUDE_MEMORY"),
        "agent_runtime_v2_include_retrieved_facts": _env("AGENT_RUNTIME_V2_INCLUDE_RETRIEVED_FACTS"),
        "agent_runtime_v2_include_short_term": _env("AGENT_RUNTIME_V2_INCLUDE_SHORT_TERM"),
        "agent_runtime_v2_context_debug": _env("AGENT_RUNTIME_V2_CONTEXT_DEBUG"),
        "planner_profile_enabled": _env("PLANNER_PROFILE_ENABLED"),
        "planner_profile_max_aliases": _env("PLANNER_PROFILE_MAX_ALIASES"),
        "planner_profile_max_patterns": _env("PLANNER_PROFILE_MAX_PATTERNS"),
        "planner_profile_min_confidence": _env("PLANNER_PROFILE_MIN_CONFIDENCE"),
        "planner_profile_include_trace_stats": _env("PLANNER_PROFILE_INCLUDE_TRACE_STATS"),
        "planner_profile_debug": _env("PLANNER_PROFILE_DEBUG"),
        "memory_learner_enabled": _env("MEMORY_LEARNER_ENABLED"),
        "memory_learner_min_confidence": _env("MEMORY_LEARNER_MIN_CONFIDENCE"),
        "memory_learner_require_confirm_alias": _env("MEMORY_LEARNER_REQUIRE_CONFIRM_ALIAS"),
        "memory_learner_default_grams_min_samples": _env("MEMORY_LEARNER_DEFAULT_GRAMS_MIN_SAMPLES"),
        "memory_learner_auto_confirm_explicit": _env("MEMORY_LEARNER_AUTO_CONFIRM_EXPLICIT"),
        "memory_learner_debug": _env("MEMORY_LEARNER_DEBUG"),
        "daily_clear_enabled": _env("DAILY_CLEAR_ENABLED"),
        "reporting_enabled": _env("REPORTING_ENABLED"),
        "report_cache_enabled": _env("REPORT_CACHE_ENABLED"),
        "report_auto_daily_enabled": _env("REPORT_AUTO_DAILY_ENABLED"),
        "report_auto_weekly_enabled": _env("REPORT_AUTO_WEEKLY_ENABLED"),
        "report_auto_monthly_enabled": _env("REPORT_AUTO_MONTHLY_ENABLED"),
        "report_timezone_offset_hours": os.getenv(
            "REPORT_TIMEZONE_OFFSET_HOURS",
            os.getenv("APP_TIMEZONE_OFFSET_HOURS", DEFAULT_SETTINGS["REPORT_TIMEZONE_OFFSET_HOURS"]),
        ),
        "report_kcal_hit_min_ratio": _env("REPORT_KCAL_HIT_MIN_RATIO"),
        "report_kcal_hit_max_ratio": _env("REPORT_KCAL_HIT_MAX_RATIO"),
        "report_protein_hit_min_ratio": _env("REPORT_PROTEIN_HIT_MIN_RATIO"),
        "report_macro_hit_min_ratio": _env("REPORT_MACRO_HIT_MIN_RATIO"),
        "report_macro_hit_max_ratio": _env("REPORT_MACRO_HIT_MAX_RATIO"),
        "personal_memory_enabled": _env("PERSONAL_MEMORY_ENABLED"),
        "auto_alias_learning_enabled": _env("AUTO_ALIAS_LEARNING_ENABLED"),
        "auto_default_grams_enabled": _env("AUTO_DEFAULT_GRAMS_ENABLED"),
        "auto_use_default_grams": _env("AUTO_USE_DEFAULT_GRAMS"),
        "auto_combo_learning_enabled": _env("AUTO_COMBO_LEARNING_ENABLED"),
        "memory_min_sample_count": _env("MEMORY_MIN_SAMPLE_COUNT"),
        "memory_lookback_days": _env("MEMORY_LOOKBACK_DAYS"),
        "memory_confidence_threshold": _env("MEMORY_CONFIDENCE_THRESHOLD"),
        "reminder_enabled": _env("REMINDER_ENABLED"),
        "reminder_default_enabled": _env("REMINDER_DEFAULT_ENABLED"),
        "reminder_max_per_day": _env("REMINDER_MAX_PER_DAY"),
        "reminder_quiet_hours_start": _env("REMINDER_QUIET_HOURS_START"),
        "reminder_quiet_hours_end": _env("REMINDER_QUIET_HOURS_END"),
        "reminder_pending_after_hours": _env("REMINDER_PENDING_AFTER_HOURS"),
        "reminder_pending_max_times": _env("REMINDER_PENDING_MAX_TIMES"),
        "reminder_daily_summary_time": _env("REMINDER_DAILY_SUMMARY_TIME"),
        "anomaly_detection_enabled": _env("ANOMALY_DETECTION_ENABLED"),
        "anomaly_meal_kcal_ratio": _env("ANOMALY_MEAL_KCAL_RATIO"),
        "anomaly_food_grams_ratio": _env("ANOMALY_FOOD_GRAMS_RATIO"),
        "anomaly_daily_kcal_max_ratio": _env("ANOMALY_DAILY_KCAL_MAX_RATIO"),
        "target_gap_protein_min_grams": _env("TARGET_GAP_PROTEIN_MIN_GRAMS"),
        "target_gap_kcal_min_remaining": _env("TARGET_GAP_KCAL_MIN_REMAINING"),
        "bitable_app_token": _env("BITABLE_APP_TOKEN"),
        "bitable_food_table_id": _env("BITABLE_FOOD_TABLE_ID"),
        "bitable_field_food_id": _env("BITABLE_FIELD_FOOD_ID"),
        "bitable_field_food_name": _env("BITABLE_FIELD_FOOD_NAME"),
        "bitable_field_alias": _env("BITABLE_FIELD_ALIAS"),
        "bitable_field_carbs": _env("BITABLE_FIELD_CARBS"),
        "bitable_field_protein": _env("BITABLE_FIELD_PROTEIN"),
        "bitable_field_fat": _env("BITABLE_FIELD_FAT"),
        "bitable_field_status": _env("BITABLE_FIELD_STATUS"),
        "bitable_intake_table_id": _env("BITABLE_INTAKE_TABLE_ID"),
        "bitable_field_intake_date": _env("BITABLE_FIELD_INTAKE_DATE"),
        "bitable_field_intake_date_value_type": _env("BITABLE_FIELD_INTAKE_DATE_VALUE_TYPE"),
        "bitable_field_intake_user_id": _env("BITABLE_FIELD_INTAKE_USER_ID"),
        "bitable_field_intake_message_id": _env("BITABLE_FIELD_INTAKE_MESSAGE_ID"),
        "bitable_field_intake_raw_text": _env("BITABLE_FIELD_INTAKE_RAW_TEXT"),
        "bitable_field_intake_meal_type": _env("BITABLE_FIELD_INTAKE_MEAL_TYPE"),
        "bitable_field_intake_food_id": _env("BITABLE_FIELD_INTAKE_FOOD_ID"),
        "bitable_field_intake_food_name": _env("BITABLE_FIELD_INTAKE_FOOD_NAME"),
        "bitable_field_intake_grams": _env("BITABLE_FIELD_INTAKE_GRAMS"),
        "bitable_field_intake_carbs": _env("BITABLE_FIELD_INTAKE_CARBS"),
        "bitable_field_intake_protein": _env("BITABLE_FIELD_INTAKE_PROTEIN"),
        "bitable_field_intake_fat": _env("BITABLE_FIELD_INTAKE_FAT"),
        "bitable_field_intake_kcal": _env("BITABLE_FIELD_INTAKE_KCAL"),
        "bitable_field_intake_status": _env("BITABLE_FIELD_INTAKE_STATUS"),
        "bitable_field_intake_created_at": _env("BITABLE_FIELD_INTAKE_CREATED_AT"),
        "bitable_standard_table_id": _env("BITABLE_STANDARD_TABLE_ID"),
        "bitable_field_standard_user_id": _env("BITABLE_FIELD_STANDARD_USER_ID"),
        "bitable_field_standard_type": _env("BITABLE_FIELD_STANDARD_TYPE"),
        "bitable_field_standard_date": _env("BITABLE_FIELD_STANDARD_DATE"),
        "bitable_field_standard_kcal": _env("BITABLE_FIELD_STANDARD_KCAL"),
        "bitable_field_standard_carbs": _env("BITABLE_FIELD_STANDARD_CARBS"),
        "bitable_field_standard_protein": _env("BITABLE_FIELD_STANDARD_PROTEIN"),
        "bitable_field_standard_fat": _env("BITABLE_FIELD_STANDARD_FAT"),
        "bitable_pending_table_id": _env("BITABLE_PENDING_TABLE_ID"),
        "bitable_field_pending_id": _env("BITABLE_FIELD_PENDING_ID"),
        "bitable_field_pending_user_id": _env("BITABLE_FIELD_PENDING_USER_ID"),
        "bitable_field_pending_chat_id": _env("BITABLE_FIELD_PENDING_CHAT_ID"),
        "bitable_field_pending_source_message_id": _env("BITABLE_FIELD_PENDING_SOURCE_MESSAGE_ID"),
        "bitable_field_pending_intent": _env("BITABLE_FIELD_PENDING_INTENT"),
        "bitable_field_pending_food_name": _env("BITABLE_FIELD_PENDING_FOOD_NAME"),
        "bitable_field_pending_grams": _env("BITABLE_FIELD_PENDING_GRAMS"),
        "bitable_field_pending_meal_type": _env("BITABLE_FIELD_PENDING_MEAL_TYPE"),
        "bitable_field_pending_raw_text": _env("BITABLE_FIELD_PENDING_RAW_TEXT"),
        "bitable_field_pending_status": _env("BITABLE_FIELD_PENDING_STATUS"),
        "bitable_field_pending_created_at": _env("BITABLE_FIELD_PENDING_CREATED_AT"),
        "bitable_field_pending_expires_at": _env("BITABLE_FIELD_PENDING_EXPIRES_AT"),
        "bitable_agent_trace_table_id": _env("BITABLE_AGENT_TRACE_TABLE_ID"),
        "bitable_field_trace_id": _env("BITABLE_FIELD_TRACE_ID"),
        "bitable_field_trace_message_id": _env("BITABLE_FIELD_TRACE_MESSAGE_ID"),
        "bitable_field_trace_user_id": _env("BITABLE_FIELD_TRACE_USER_ID"),
        "bitable_field_trace_raw_text": _env("BITABLE_FIELD_TRACE_RAW_TEXT"),
        "bitable_field_trace_planner_prompt_version": _env("BITABLE_FIELD_TRACE_PLANNER_PROMPT_VERSION"),
        "bitable_field_trace_plan_json": _env("BITABLE_FIELD_TRACE_PLAN_JSON"),
        "bitable_field_trace_action_count": _env("BITABLE_FIELD_TRACE_ACTION_COUNT"),
        "bitable_field_trace_tool_results": _env("BITABLE_FIELD_TRACE_TOOL_RESULTS"),
        "bitable_field_trace_execution_facts": _env("BITABLE_FIELD_TRACE_EXECUTION_FACTS"),
        "bitable_field_trace_final_reply": _env("BITABLE_FIELD_TRACE_FINAL_REPLY"),
        "bitable_field_trace_fallback_used": _env("BITABLE_FIELD_TRACE_FALLBACK_USED"),
        "bitable_field_trace_error_type": _env("BITABLE_FIELD_TRACE_ERROR_TYPE"),
        "bitable_field_trace_latency_ms": _env("BITABLE_FIELD_TRACE_LATENCY_MS"),
        "bitable_field_trace_created_at": _env("BITABLE_FIELD_TRACE_CREATED_AT"),
        "bitable_report_table_id": _env("BITABLE_REPORT_TABLE_ID"),
        "bitable_field_report_id": _env("BITABLE_FIELD_REPORT_ID"),
        "bitable_field_report_user_id": _env("BITABLE_FIELD_REPORT_USER_ID"),
        "bitable_field_report_type": _env("BITABLE_FIELD_REPORT_TYPE"),
        "bitable_field_report_period_start": _env("BITABLE_FIELD_REPORT_PERIOD_START"),
        "bitable_field_report_period_end": _env("BITABLE_FIELD_REPORT_PERIOD_END"),
        "bitable_field_report_summary_text": _env("BITABLE_FIELD_REPORT_SUMMARY_TEXT"),
        "bitable_field_report_facts_json": _env("BITABLE_FIELD_REPORT_FACTS_JSON"),
        "bitable_field_report_status": _env("BITABLE_FIELD_REPORT_STATUS"),
        "bitable_field_report_generated_at": _env("BITABLE_FIELD_REPORT_GENERATED_AT"),
        "bitable_field_report_source": _env("BITABLE_FIELD_REPORT_SOURCE"),
        "bitable_field_report_version": _env("BITABLE_FIELD_REPORT_VERSION"),
        "bitable_memory_table_id": _env("BITABLE_MEMORY_TABLE_ID"),
        "bitable_field_memory_id": _env("BITABLE_FIELD_MEMORY_ID"),
        "bitable_field_memory_user_id": _env("BITABLE_FIELD_MEMORY_USER_ID"),
        "bitable_field_memory_food_id": _env("BITABLE_FIELD_MEMORY_FOOD_ID"),
        "bitable_field_memory_food_name": _env("BITABLE_FIELD_MEMORY_FOOD_NAME"),
        "bitable_field_memory_alias": _env("BITABLE_FIELD_MEMORY_ALIAS"),
        "bitable_field_memory_default_grams": _env("BITABLE_FIELD_MEMORY_DEFAULT_GRAMS"),
        "bitable_field_memory_meal_type": _env("BITABLE_FIELD_MEMORY_MEAL_TYPE"),
        "bitable_field_memory_sample_count": _env("BITABLE_FIELD_MEMORY_SAMPLE_COUNT"),
        "bitable_field_memory_confidence": _env("BITABLE_FIELD_MEMORY_CONFIDENCE"),
        "bitable_field_memory_source": _env("BITABLE_FIELD_MEMORY_SOURCE"),
        "bitable_field_memory_source_message_id": _env("BITABLE_FIELD_MEMORY_SOURCE_MESSAGE_ID"),
        "bitable_field_memory_status": _env("BITABLE_FIELD_MEMORY_STATUS"),
        "bitable_field_memory_created_at": _env("BITABLE_FIELD_MEMORY_CREATED_AT"),
        "bitable_field_memory_updated_at": _env("BITABLE_FIELD_MEMORY_UPDATED_AT"),
        "bitable_combo_table_id": _env("BITABLE_COMBO_TABLE_ID"),
        "bitable_field_combo_id": _env("BITABLE_FIELD_COMBO_ID"),
        "bitable_field_combo_user_id": _env("BITABLE_FIELD_COMBO_USER_ID"),
        "bitable_field_combo_name": _env("BITABLE_FIELD_COMBO_NAME"),
        "bitable_field_combo_meal_type": _env("BITABLE_FIELD_COMBO_MEAL_TYPE"),
        "bitable_field_combo_items_json": _env("BITABLE_FIELD_COMBO_ITEMS_JSON"),
        "bitable_field_combo_use_count": _env("BITABLE_FIELD_COMBO_USE_COUNT"),
        "bitable_field_combo_confidence": _env("BITABLE_FIELD_COMBO_CONFIDENCE"),
        "bitable_field_combo_status": _env("BITABLE_FIELD_COMBO_STATUS"),
        "bitable_field_combo_created_at": _env("BITABLE_FIELD_COMBO_CREATED_AT"),
        "bitable_field_combo_updated_at": _env("BITABLE_FIELD_COMBO_UPDATED_AT"),
        "bitable_preference_table_id": _env("BITABLE_PREFERENCE_TABLE_ID"),
        "bitable_field_preference_id": _env("BITABLE_FIELD_PREFERENCE_ID"),
        "bitable_field_preference_user_id": _env("BITABLE_FIELD_PREFERENCE_USER_ID"),
        "bitable_field_preference_key": _env("BITABLE_FIELD_PREFERENCE_KEY"),
        "bitable_field_preference_value": _env("BITABLE_FIELD_PREFERENCE_VALUE"),
        "bitable_field_preference_confidence": _env("BITABLE_FIELD_PREFERENCE_CONFIDENCE"),
        "bitable_field_preference_status": _env("BITABLE_FIELD_PREFERENCE_STATUS"),
        "bitable_field_preference_created_at": _env("BITABLE_FIELD_PREFERENCE_CREATED_AT"),
        "bitable_field_preference_updated_at": _env("BITABLE_FIELD_PREFERENCE_UPDATED_AT"),
        "bitable_reminder_rule_table_id": _env("BITABLE_REMINDER_RULE_TABLE_ID"),
        "bitable_field_reminder_rule_id": _env("BITABLE_FIELD_REMINDER_RULE_ID"),
        "bitable_field_reminder_rule_user_id": _env("BITABLE_FIELD_REMINDER_RULE_USER_ID"),
        "bitable_field_reminder_rule_type": _env("BITABLE_FIELD_REMINDER_RULE_TYPE"),
        "bitable_field_reminder_rule_enabled": _env("BITABLE_FIELD_REMINDER_RULE_ENABLED"),
        "bitable_field_reminder_rule_schedule_time": _env("BITABLE_FIELD_REMINDER_RULE_SCHEDULE_TIME"),
        "bitable_field_reminder_rule_meal_type": _env("BITABLE_FIELD_REMINDER_RULE_MEAL_TYPE"),
        "bitable_field_reminder_rule_threshold_json": _env("BITABLE_FIELD_REMINDER_RULE_THRESHOLD_JSON"),
        "bitable_field_reminder_rule_quiet_hours_start": _env("BITABLE_FIELD_REMINDER_RULE_QUIET_HOURS_START"),
        "bitable_field_reminder_rule_quiet_hours_end": _env("BITABLE_FIELD_REMINDER_RULE_QUIET_HOURS_END"),
        "bitable_field_reminder_rule_max_per_day": _env("BITABLE_FIELD_REMINDER_RULE_MAX_PER_DAY"),
        "bitable_field_reminder_rule_status": _env("BITABLE_FIELD_REMINDER_RULE_STATUS"),
        "bitable_field_reminder_rule_chat_id": _env("BITABLE_FIELD_REMINDER_RULE_CHAT_ID"),
        "bitable_field_reminder_rule_allow_group": _env("BITABLE_FIELD_REMINDER_RULE_ALLOW_GROUP"),
        "bitable_field_reminder_rule_paused_until": _env("BITABLE_FIELD_REMINDER_RULE_PAUSED_UNTIL"),
        "bitable_field_reminder_rule_created_at": _env("BITABLE_FIELD_REMINDER_RULE_CREATED_AT"),
        "bitable_field_reminder_rule_updated_at": _env("BITABLE_FIELD_REMINDER_RULE_UPDATED_AT"),
        "bitable_reminder_event_table_id": _env("BITABLE_REMINDER_EVENT_TABLE_ID"),
        "bitable_field_reminder_event_id": _env("BITABLE_FIELD_REMINDER_EVENT_ID"),
        "bitable_field_reminder_event_user_id": _env("BITABLE_FIELD_REMINDER_EVENT_USER_ID"),
        "bitable_field_reminder_event_rule_id": _env("BITABLE_FIELD_REMINDER_EVENT_RULE_ID"),
        "bitable_field_reminder_event_type": _env("BITABLE_FIELD_REMINDER_EVENT_TYPE"),
        "bitable_field_reminder_event_trigger_reason": _env("BITABLE_FIELD_REMINDER_EVENT_TRIGGER_REASON"),
        "bitable_field_reminder_event_facts_json": _env("BITABLE_FIELD_REMINDER_EVENT_FACTS_JSON"),
        "bitable_field_reminder_event_message_text": _env("BITABLE_FIELD_REMINDER_EVENT_MESSAGE_TEXT"),
        "bitable_field_reminder_event_status": _env("BITABLE_FIELD_REMINDER_EVENT_STATUS"),
        "bitable_field_reminder_event_sent_at": _env("BITABLE_FIELD_REMINDER_EVENT_SENT_AT"),
        "bitable_field_reminder_event_acknowledged_at": _env("BITABLE_FIELD_REMINDER_EVENT_ACKNOWLEDGED_AT"),
        "bitable_field_reminder_event_source": _env("BITABLE_FIELD_REMINDER_EVENT_SOURCE"),
        "bitable_field_reminder_event_chat_id": _env("BITABLE_FIELD_REMINDER_EVENT_CHAT_ID"),
        "bitable_field_reminder_event_error": _env("BITABLE_FIELD_REMINDER_EVENT_ERROR"),
        "bitable_anomaly_table_id": _env("BITABLE_ANOMALY_TABLE_ID"),
        "bitable_field_anomaly_id": _env("BITABLE_FIELD_ANOMALY_ID"),
        "bitable_field_anomaly_user_id": _env("BITABLE_FIELD_ANOMALY_USER_ID"),
        "bitable_field_anomaly_type": _env("BITABLE_FIELD_ANOMALY_TYPE"),
        "bitable_field_anomaly_severity": _env("BITABLE_FIELD_ANOMALY_SEVERITY"),
        "bitable_field_anomaly_related_record_id": _env("BITABLE_FIELD_ANOMALY_RELATED_RECORD_ID"),
        "bitable_field_anomaly_related_message_id": _env("BITABLE_FIELD_ANOMALY_RELATED_MESSAGE_ID"),
        "bitable_field_anomaly_facts_json": _env("BITABLE_FIELD_ANOMALY_FACTS_JSON"),
        "bitable_field_anomaly_status": _env("BITABLE_FIELD_ANOMALY_STATUS"),
        "bitable_field_anomaly_created_at": _env("BITABLE_FIELD_ANOMALY_CREATED_AT"),
    }


def _truthy_setting(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _agent_runtime_enabled(settings: dict[str, str]) -> bool:
    return _truthy_setting(settings.get("agent_runtime_enabled", "true"))


def _agent_fallback_to_legacy(settings: dict[str, str]) -> bool:
    return _truthy_setting(settings.get("agent_fallback_to_legacy", "true"))


def _daily_clear_enabled(settings: dict[str, str]) -> bool:
    return _truthy_setting(settings.get("daily_clear_enabled", "true"))


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def _parse_json(raw_body: bytes) -> dict[str, Any] | None:
    if not raw_body:
        return {}
    try:
        parsed = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _verify_feishu_token(payload: dict[str, Any]) -> bool:
    expected = _settings()["feishu_verification_token"]
    if not expected:
        return True

    actual = payload.get("token")
    header = payload.get("header")
    if isinstance(header, dict):
        actual = header.get("token", actual)
    return actual == expected


def _verify_rollover_task_token(path: str, headers: Any) -> bool:
    expected = _settings()["rollover_task_token"]
    if not expected:
        return False

    parsed = urllib.parse.urlparse(path)
    query = urllib.parse.parse_qs(parsed.query)
    actual = ""
    if isinstance(headers, dict):
        actual = str(headers.get("X-Task-Token") or headers.get("x-task-token") or "")
    else:
        actual = str(headers.get("X-Task-Token") or "")
    if not actual:
        actual = str((query.get("token") or [""])[0])
    return actual == expected


def _extract_challenge(payload: dict[str, Any]) -> str:
    challenge = payload.get("challenge")
    if isinstance(challenge, str):
        return challenge

    event = payload.get("event")
    if isinstance(event, dict):
        challenge = event.get("challenge")
        if isinstance(challenge, str):
            return challenge

    return ""


def _parse_text_message(payload: dict[str, Any]) -> dict[str, str] | None:
    event = payload.get("event")
    if not isinstance(event, dict):
        return None

    message = event.get("message")
    if not isinstance(message, dict) or message.get("message_type") != "text":
        return None

    raw_content = message.get("content")
    if isinstance(raw_content, str):
        try:
            content = json.loads(raw_content)
        except json.JSONDecodeError:
            content = {"text": raw_content}
    elif isinstance(raw_content, dict):
        content = raw_content
    else:
        content = {}

    text = str(content.get("text") or "").strip()
    if not text:
        return None

    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}

    return {
        "message_id": str(message.get("message_id") or ""),
        "chat_id": str(message.get("chat_id") or ""),
        "user_id": str(sender_id.get("open_id") or sender_id.get("user_id") or ""),
        "text": text,
    }


def _http_json_post(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    body = _json_bytes(payload)
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            **(headers or {}),
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw_error = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} POST {url}: {raw_error}") from exc

    parsed = json.loads(raw.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def _http_json_get(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            **(headers or {}),
        },
        method="GET",
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw_error = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} GET {url}: {raw_error}") from exc

    parsed = json.loads(raw.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def _http_json_put(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    body = _json_bytes(payload)
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            **(headers or {}),
        },
        method="PUT",
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw_error = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} PUT {url}: {raw_error}") from exc

    parsed = json.loads(raw.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def _http_json_delete(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        headers={
            "Content-Type": "application/json; charset=utf-8",
            **(headers or {}),
        },
        method="DELETE",
    )

    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw_error = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code} DELETE {url}: {raw_error}") from exc

    if not raw:
        return {"code": 0, "data": {}}
    parsed = json.loads(raw.decode("utf-8"))
    return parsed if isinstance(parsed, dict) else {}


def _get_tenant_access_token() -> str:
    global _TENANT_ACCESS_TOKEN, _TENANT_ACCESS_TOKEN_EXPIRES_AT

    if _TENANT_ACCESS_TOKEN and time.monotonic() < _TENANT_ACCESS_TOKEN_EXPIRES_AT:
        return _TENANT_ACCESS_TOKEN

    settings = _settings()
    app_id = settings["feishu_app_id"]
    app_secret = settings["feishu_app_secret"]
    if not app_id or not app_secret:
        raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET are not configured.")

    data = _http_json_post(
        f"{FEISHU_API_BASE}/auth/v3/tenant_access_token/internal",
        {"app_id": app_id, "app_secret": app_secret},
    )
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to get tenant_access_token: {data}")

    token = str(data.get("tenant_access_token") or "")
    if not token:
        raise RuntimeError("Feishu response does not include tenant_access_token.")

    expires_in = int(data.get("expire") or 7200)
    _TENANT_ACCESS_TOKEN = token
    _TENANT_ACCESS_TOKEN_EXPIRES_AT = time.monotonic() + max(expires_in - 60, 60)
    return token


def _send_feishu_text_message(chat_id: str, text: str, uuid_key: str = "") -> dict[str, Any]:
    token = _get_tenant_access_token()
    query = urllib.parse.urlencode({"receive_id_type": "chat_id"})
    payload = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    if uuid_key:
        payload["uuid"] = uuid_key
    data = _http_json_post(
        f"{FEISHU_API_BASE}/im/v1/messages?{query}",
        payload,
        {"Authorization": f"Bearer {token}"},
    )
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to send Feishu message: {data}")
    return data


def _list_bitable_records(app_token: str, table_id: str, field_names: list[str] | None = None) -> list[dict[str, Any]]:
    token = _get_tenant_access_token()
    records: list[dict[str, Any]] = []
    page_token = ""

    while True:
        query: dict[str, str] = {"page_size": "500"}
        if page_token:
            query["page_token"] = page_token
        if field_names:
            query["field_names"] = json.dumps(field_names, ensure_ascii=False)

        url = (
            f"{FEISHU_API_BASE}/bitable/v1/apps/{urllib.parse.quote(app_token, safe='')}/"
            f"tables/{urllib.parse.quote(table_id, safe='')}/records?{urllib.parse.urlencode(query)}"
        )
        data = _http_json_get(url, {"Authorization": f"Bearer {token}"})
        if data.get("code") != 0:
            raise RuntimeError(f"Failed to list Bitable records: {data}")

        payload = data.get("data")
        if not isinstance(payload, dict):
            raise RuntimeError(f"Bitable response does not include data: {data}")

        items = payload.get("items")
        if isinstance(items, list):
            records.extend(item for item in items if isinstance(item, dict))

        if not payload.get("has_more"):
            return records

        page_token = str(payload.get("page_token") or "")
        if not page_token:
            return records


def _search_bitable_records(
    app_token: str,
    table_id: str,
    field_name: str,
    value: str,
    page_size: int = 20,
) -> list[dict[str, Any]]:
    token = _get_tenant_access_token()
    query = urllib.parse.urlencode({"page_size": str(page_size)})
    url = (
        f"{FEISHU_API_BASE}/bitable/v1/apps/{urllib.parse.quote(app_token, safe='')}/"
        f"tables/{urllib.parse.quote(table_id, safe='')}/records/search?{query}"
    )
    payload = {
        "filter": {
            "conjunction": "and",
            "conditions": [
                {
                    "field_name": field_name,
                    "operator": "is",
                    "value": [value],
                }
            ],
        }
    }
    data = _http_json_post(url, payload, {"Authorization": f"Bearer {token}"})
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to search Bitable records: {data}")

    body = data.get("data")
    if not isinstance(body, dict):
        return []

    items = body.get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _batch_create_bitable_records(
    app_token: str,
    table_id: str,
    records: list[dict[str, Any]],
    client_token: str = "",
) -> dict[str, Any]:
    if not records:
        return {"code": 0, "data": {"records": []}}

    token = _get_tenant_access_token()
    query: dict[str, str] = {}
    if client_token:
        query["client_token"] = client_token
    query_string = f"?{urllib.parse.urlencode(query)}" if query else ""
    url = (
        f"{FEISHU_API_BASE}/bitable/v1/apps/{urllib.parse.quote(app_token, safe='')}/"
        f"tables/{urllib.parse.quote(table_id, safe='')}/records/batch_create{query_string}"
    )
    payload = {"records": records}
    data = _http_json_post(url, payload, {"Authorization": f"Bearer {token}"})
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to batch create Bitable records: {data}")
    return data


def _update_bitable_record(
    app_token: str,
    table_id: str,
    record_id: str,
    fields: dict[str, Any],
) -> dict[str, Any]:
    if not record_id:
        raise RuntimeError("record_id is required for Bitable update.")
    if not fields:
        return {"code": 0, "data": {"record": {"record_id": record_id, "fields": {}}}}

    token = _get_tenant_access_token()
    url = (
        f"{FEISHU_API_BASE}/bitable/v1/apps/{urllib.parse.quote(app_token, safe='')}/"
        f"tables/{urllib.parse.quote(table_id, safe='')}/records/{urllib.parse.quote(record_id, safe='')}"
    )
    data = _http_json_put(url, {"fields": fields}, {"Authorization": f"Bearer {token}"})
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to update Bitable record: {data}")
    return data


def _delete_bitable_record(app_token: str, table_id: str, record_id: str) -> dict[str, Any]:
    if not record_id:
        raise RuntimeError("record_id is required for Bitable delete.")

    token = _get_tenant_access_token()
    url = (
        f"{FEISHU_API_BASE}/bitable/v1/apps/{urllib.parse.quote(app_token, safe='')}/"
        f"tables/{urllib.parse.quote(table_id, safe='')}/records/{urllib.parse.quote(record_id, safe='')}"
    )
    data = _http_json_delete(url, {"Authorization": f"Bearer {token}"})
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to delete Bitable record: {data}")
    return data


def _configured_field_name(settings: dict[str, str], key: str) -> str:
    return settings.get(f"bitable_field_{key}", "") or FIELD_FALLBACKS[key][0]


def _candidate_field_names(settings: dict[str, str], key: str) -> list[str]:
    names = [_configured_field_name(settings, key), *FIELD_FALLBACKS[key]]
    unique: list[str] = []
    for name in names:
        if name and name not in unique:
            unique.append(name)
    return unique


def _field_value(fields: dict[str, Any], names: list[str]) -> Any:
    for name in names:
        if name in fields:
            return fields[name]
    return None


def _extract_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [_extract_text(item) for item in value]
        return " ".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("text", "name", "value", "link"):
            if key in value:
                text = _extract_text(value[key])
                if text:
                    return text
    return str(value).strip()


def _extract_number(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return _round_number(float(value))
    if isinstance(value, str):
        try:
            return _round_number(float(value.strip()))
        except ValueError:
            return None
    if isinstance(value, list) and value:
        return _extract_number(value[0])
    if isinstance(value, dict):
        for key in ("value", "text", "number"):
            if key in value:
                number = _extract_number(value[key])
                if number is not None:
                    return number
    return None


def _normalize_food_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").strip().lower()
    return re.sub(r"[\s,，、;；/|]+", "", normalized)


def _split_aliases(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,，、;；/|\n]+", value or "") if item.strip()]


def _food_record_from_bitable(record: dict[str, Any], settings: dict[str, str]) -> dict[str, Any] | None:
    fields = record.get("fields")
    if not isinstance(fields, dict):
        return None

    name = _extract_text(_field_value(fields, _candidate_field_names(settings, "food_name")))
    if not name:
        return None

    status = _extract_text(_field_value(fields, _candidate_field_names(settings, "status"))).lower()
    if status and status not in {"active", "启用", "正常", "可用"}:
        return None

    carbs = _extract_number(_field_value(fields, _candidate_field_names(settings, "carbs")))
    protein = _extract_number(_field_value(fields, _candidate_field_names(settings, "protein")))
    fat = _extract_number(_field_value(fields, _candidate_field_names(settings, "fat")))
    if carbs is None or protein is None or fat is None:
        return None

    alias_text = _extract_text(_field_value(fields, _candidate_field_names(settings, "alias")))
    food_id = _extract_text(_field_value(fields, _candidate_field_names(settings, "food_id")))
    aliases = _split_aliases(alias_text)

    return {
        "record_id": str(record.get("record_id") or ""),
        "food_id": food_id,
        "name": name,
        "aliases": aliases,
        "carbs_per_100g": carbs,
        "protein_per_100g": protein,
        "fat_per_100g": fat,
    }


def _load_food_database() -> list[dict[str, Any]]:
    settings = _settings()
    app_token = settings["bitable_app_token"]
    table_id = settings["bitable_food_table_id"]
    if not app_token or not table_id:
        raise RuntimeError("BITABLE_APP_TOKEN and BITABLE_FOOD_TABLE_ID are not configured.")

    records = _list_bitable_records(app_token, table_id)
    foods: list[dict[str, Any]] = []
    for record in records:
        food = _food_record_from_bitable(record, settings)
        if food:
            foods.append(food)
    return foods


def _match_food(food_name: str, food_database: list[dict[str, Any]]) -> dict[str, Any] | None:
    query = _normalize_food_key(food_name)
    if not query:
        return None

    for food in food_database:
        keys = [_normalize_food_key(food["name"])]
        keys.extend(_normalize_food_key(alias) for alias in food.get("aliases", []))
        if query in keys:
            return food

    if len(query) >= 2:
        for food in food_database:
            keys = [_normalize_food_key(food["name"])]
            keys.extend(_normalize_food_key(alias) for alias in food.get("aliases", []))
            if any(query in key or key in query for key in keys if key):
                return food

    return None


def _legacy_food_match_diagnostics(
    food_name: str,
    grams: Any,
    matched_food: dict[str, Any] | None,
    food_database: list[dict[str, Any]],
) -> dict[str, Any]:
    query = _normalize_food_key(food_name)
    candidates: list[dict[str, Any]] = []
    if query:
        for food in food_database:
            for key, key_type in _food_match_keys(food):
                if query == key:
                    candidates.append(_food_candidate_debug(food, "exact" if key_type == "name" else "alias_exact", key, key_type))
                    break
        if not candidates and len(query) >= 2:
            for food in food_database:
                for key, key_type in _food_match_keys(food):
                    if query in key or key in query:
                        reason = "contains" if query in key else "query_contains"
                        candidates.append(_food_candidate_debug(food, reason, key, key_type))
                        break
    return {
        "input_name": str(food_name or ""),
        "normalized_query": query,
        "grams": grams,
        "status": "matched" if matched_food else ("empty_query" if not query else "not_found"),
        "matched_name": str((matched_food or {}).get("name") or ""),
        "matched_food_id": str((matched_food or {}).get("food_id") or ""),
        "food_database_count": len(food_database),
        "candidate_count": len(candidates),
        "candidates": candidates[:5],
    }


def _food_match_keys(food: dict[str, Any]) -> list[tuple[str, str]]:
    keys = [(_normalize_food_key(food.get("name")), "name")]
    keys.extend((_normalize_food_key(alias), "alias") for alias in food.get("aliases", []) if alias)
    return [(key, key_type) for key, key_type in keys if key]


def _food_candidate_debug(food: dict[str, Any], reason: str, matched_key: str, key_type: str) -> dict[str, Any]:
    return {
        "food_name": str(food.get("name") or ""),
        "food_id": str(food.get("food_id") or ""),
        "record_id": str(food.get("record_id") or ""),
        "reason": reason,
        "matched_key": matched_key,
        "matched_key_type": key_type,
    }


def _calculate_food_intake(food: dict[str, Any], grams: float) -> dict[str, float]:
    multiplier = grams / 100
    carbs = _round_number(food["carbs_per_100g"] * multiplier)
    protein = _round_number(food["protein_per_100g"] * multiplier)
    fat = _round_number(food["fat_per_100g"] * multiplier)
    kcal = _round_number(carbs * 4 + protein * 4 + fat * 9)
    return {"carbs": carbs, "protein": protein, "fat": fat, "kcal": kcal}


def _enrich_add_food_with_food_database(parse_result: dict[str, Any]) -> dict[str, Any]:
    food_database = _load_food_database()
    matched_items: list[dict[str, Any]] = []
    missing_items: list[dict[str, Any]] = []
    match_diagnostics: list[dict[str, Any]] = []
    totals = {"carbs": 0.0, "protein": 0.0, "fat": 0.0, "kcal": 0.0}

    for parsed_food in parse_result.get("foods", []):
        name = str(parsed_food.get("name") or "").strip()
        grams = parsed_food.get("grams")
        matched_food = _match_food(name, food_database)
        match_diagnostics.append(_legacy_food_match_diagnostics(name, grams, matched_food, food_database))
        if not matched_food:
            missing_items.append({"name": name, "grams": grams, "reason": "food_not_found"})
            continue
        if grams is None:
            missing_items.append({"name": name, "grams": grams, "reason": "grams_missing", "matched_food": matched_food})
            continue

        nutrition = _calculate_food_intake(matched_food, float(grams))
        for key in totals:
            totals[key] = _round_number(totals[key] + nutrition[key])

        matched_items.append(
            {
                "input_name": name,
                "matched_name": matched_food["name"],
                "food_id": matched_food["food_id"],
                "record_id": matched_food["record_id"],
                "grams": _round_number(float(grams)),
                "per_100g": {
                    "carbs": matched_food["carbs_per_100g"],
                    "protein": matched_food["protein_per_100g"],
                    "fat": matched_food["fat_per_100g"],
                },
                "nutrition": nutrition,
            }
        )

    return {
        "items": matched_items,
        "missing": missing_items,
        "totals": totals,
        "food_database_count": len(food_database),
        "match_diagnostics": match_diagnostics,
    }


def _lookup_result_from_food(food: dict[str, Any], input_name: str, grams: float) -> dict[str, Any]:
    nutrition = _calculate_food_intake(food, float(grams))
    return {
        "items": [
            {
                "input_name": input_name,
                "matched_name": food["name"],
                "food_id": food.get("food_id", ""),
                "record_id": food.get("record_id", ""),
                "grams": _round_number(float(grams)),
                "per_100g": {
                    "carbs": food["carbs_per_100g"],
                    "protein": food["protein_per_100g"],
                    "fat": food["fat_per_100g"],
                },
                "nutrition": nutrition,
            }
        ],
        "missing": [],
        "totals": {
            "carbs": nutrition["carbs"],
            "protein": nutrition["protein"],
            "fat": nutrition["fat"],
            "kcal": nutrition["kcal"],
        },
        "food_database_count": 1,
    }


def _food_id_for_name(food_name: str) -> str:
    key = _normalize_food_key(food_name)
    return _stable_uuid4_from_text(f"calorie-agent:food:{key or food_name}")


def _food_client_token(food_name: str) -> str:
    key = _normalize_food_key(food_name)
    return _stable_uuid4_from_text(f"calorie-agent:food-create:{key or food_name}")


def _create_food_database_record(food_name: str, per_100g: dict[str, float]) -> dict[str, Any]:
    settings = _settings()
    app_token = settings["bitable_app_token"]
    table_id = settings["bitable_food_table_id"]
    if not app_token or not table_id:
        raise RuntimeError("BITABLE_APP_TOKEN and BITABLE_FOOD_TABLE_ID are not configured.")

    fields = {
        _configured_field_name(settings, "food_id"): _food_id_for_name(food_name),
        _configured_field_name(settings, "food_name"): food_name,
        _configured_field_name(settings, "alias"): "",
        _configured_field_name(settings, "carbs"): per_100g["carbs"],
        _configured_field_name(settings, "protein"): per_100g["protein"],
        _configured_field_name(settings, "fat"): per_100g["fat"],
        _configured_field_name(settings, "status"): "active",
    }
    data = _batch_create_bitable_records(
        app_token,
        table_id,
        [{"fields": fields}],
        client_token=_food_client_token(food_name),
    )
    body = data.get("data") if isinstance(data.get("data"), dict) else {}
    records = body.get("records") if isinstance(body.get("records"), list) else []
    record_id = str(records[0].get("record_id") or "") if records and isinstance(records[0], dict) else ""
    return {
        "record_id": record_id,
        "food_id": fields[_configured_field_name(settings, "food_id")],
        "name": food_name,
        "aliases": [],
        "carbs_per_100g": per_100g["carbs"],
        "protein_per_100g": per_100g["protein"],
        "fat_per_100g": per_100g["fat"],
    }


def _timezone_offset_seconds() -> int:
    try:
        return int(float(_settings()["timezone_offset_hours"]) * 3600)
    except ValueError:
        return 8 * 3600


def _today_start_millis() -> int:
    offset = _timezone_offset_seconds()
    now = time.time()
    shifted = now + offset
    start_shifted = math.floor(shifted / 86400) * 86400
    return int((start_shifted - offset) * 1000)


def _today_string() -> str:
    offset = _timezone_offset_seconds()
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + offset))


def _now_millis() -> int:
    return int(time.time() * 1000)


def _intake_field(settings: dict[str, str], key: str) -> str:
    return settings[f"bitable_field_intake_{key}"]


def _standard_field(settings: dict[str, str], key: str) -> str:
    return settings[f"bitable_field_standard_{key}"]


def _pending_field(settings: dict[str, str], key: str) -> str:
    return settings[f"bitable_field_pending_{key}"]


def _unique_non_empty(values: list[str]) -> list[str]:
    unique: list[str] = []
    for value in values:
        if value and value not in unique:
            unique.append(value)
    return unique


def _is_today_date_value(value: Any, settings: dict[str, str]) -> bool:
    if settings.get("bitable_field_intake_date_value_type") == "text":
        return _extract_text(value) == _today_string()

    number = _extract_number(value)
    if number is None:
        return False

    start = _today_start_millis()
    return start <= int(number) < start + 86400000


def _is_valid_intake_status(value: Any) -> bool:
    status = _extract_text(value).lower()
    return not status or status in {"valid", "active", "normal", "启用", "正常", "有效", "已记录"}


def _pending_ttl_millis() -> int:
    try:
        hours = float(_settings()["pending_task_ttl_hours"])
    except ValueError:
        hours = 24
    return int(max(hours, 1) * 3600 * 1000)


def _pending_field_names(settings: dict[str, str]) -> list[str]:
    return _unique_non_empty(
        [
            _pending_field(settings, "id"),
            _pending_field(settings, "user_id"),
            _pending_field(settings, "chat_id"),
            _pending_field(settings, "source_message_id"),
            _pending_field(settings, "intent"),
            _pending_field(settings, "food_name"),
            _pending_field(settings, "grams"),
            _pending_field(settings, "meal_type"),
            _pending_field(settings, "raw_text"),
            _pending_field(settings, "status"),
            _pending_field(settings, "created_at"),
            _pending_field(settings, "expires_at"),
        ]
    )


def _pending_task_from_record(record: dict[str, Any], settings: dict[str, str]) -> dict[str, Any] | None:
    fields = record.get("fields")
    if not isinstance(fields, dict):
        return None
    return {
        "record_id": str(record.get("record_id") or ""),
        "pending_id": _extract_text(fields.get(_pending_field(settings, "id"))),
        "user_id": _extract_text(fields.get(_pending_field(settings, "user_id"))),
        "chat_id": _extract_text(fields.get(_pending_field(settings, "chat_id"))),
        "source_message_id": _extract_text(fields.get(_pending_field(settings, "source_message_id"))),
        "intent": _extract_text(fields.get(_pending_field(settings, "intent"))),
        "food_name": _extract_text(fields.get(_pending_field(settings, "food_name"))),
        "grams": _extract_number(fields.get(_pending_field(settings, "grams"))),
        "meal_type": _extract_text(fields.get(_pending_field(settings, "meal_type"))) or "unknown",
        "raw_text": _extract_text(fields.get(_pending_field(settings, "raw_text"))),
        "status": _extract_text(fields.get(_pending_field(settings, "status"))).lower(),
        "created_at": _extract_number(fields.get(_pending_field(settings, "created_at"))) or 0.0,
        "expires_at": _extract_number(fields.get(_pending_field(settings, "expires_at"))) or 0.0,
    }


def _load_active_pending_task(message: dict[str, str]) -> dict[str, Any] | None:
    settings = _settings()
    app_token = settings["bitable_app_token"]
    table_id = settings["bitable_pending_table_id"]
    if not app_token or not table_id:
        return None

    records = _list_bitable_records(app_token, table_id, field_names=_pending_field_names(settings))
    user_id = str(message.get("user_id") or "").strip()
    chat_id = str(message.get("chat_id") or "").strip()
    now = _now_millis()
    best: dict[str, Any] | None = None
    for record in records:
        task = _pending_task_from_record(record, settings)
        if not task:
            continue
        if task["status"] != "pending":
            continue
        if user_id and task["user_id"] and task["user_id"] != user_id:
            continue
        if chat_id and task["chat_id"] and task["chat_id"] != chat_id:
            continue
        if task["expires_at"] and task["expires_at"] < now:
            continue
        if best is None or float(task.get("created_at") or 0) > float(best.get("created_at") or 0):
            best = task
    return best


def _pending_id_for_missing(message: dict[str, str], intent: str, food_name: str) -> str:
    return _stable_uuid4_from_text(f"calorie-agent:pending:{_message_key(message)}:{intent}:{food_name}")


def _pending_client_token(message: dict[str, str], intent: str, food_name: str) -> str:
    return _stable_uuid4_from_text(f"calorie-agent:pending-create:{_message_key(message)}:{intent}:{food_name}")


def _create_pending_task_for_missing(
    message: dict[str, str],
    parse_result: dict[str, Any],
    missing_item: dict[str, Any],
) -> dict[str, Any]:
    reason = str(missing_item.get("reason") or "")
    if reason == "confirm_default_grams":
        intent = "confirm_default_grams"
    else:
        intent = "missing_food" if reason == "food_not_found" else "missing_grams"
    food_name = str(missing_item.get("name") or "").strip()
    grams = missing_item.get("grams")
    settings = _settings()
    app_token = settings["bitable_app_token"]
    table_id = settings["bitable_pending_table_id"]
    if not app_token or not table_id:
        return {
            "status": "not_configured",
            "intent": intent,
            "food_name": food_name,
            "grams": grams,
            "item": missing_item,
        }

    pending_id = _pending_id_for_missing(message, intent, food_name)
    now = _now_millis()
    fields = {
        _pending_field(settings, "id"): pending_id,
        _pending_field(settings, "user_id"): message.get("user_id", ""),
        _pending_field(settings, "chat_id"): message.get("chat_id", ""),
        _pending_field(settings, "source_message_id"): message.get("message_id", ""),
        _pending_field(settings, "intent"): intent,
        _pending_field(settings, "food_name"): food_name,
        _pending_field(settings, "meal_type"): parse_result.get("meal_type", "unknown"),
        _pending_field(settings, "raw_text"): message.get("text", ""),
        _pending_field(settings, "status"): "pending",
        _pending_field(settings, "created_at"): now,
        _pending_field(settings, "expires_at"): now + _pending_ttl_millis(),
    }
    if grams is not None:
        fields[_pending_field(settings, "grams")] = grams

    data = _batch_create_bitable_records(
        app_token,
        table_id,
        [{"fields": fields}],
        client_token=_pending_client_token(message, intent, food_name),
    )
    return {
        "status": "created",
        "pending_id": pending_id,
        "intent": intent,
        "food_name": food_name,
        "grams": grams,
        "create_result": data,
    }


def _update_pending_task(task: dict[str, Any], fields: dict[str, Any]) -> dict[str, Any]:
    settings = _settings()
    app_token = settings["bitable_app_token"]
    table_id = settings["bitable_pending_table_id"]
    if not app_token or not table_id:
        raise RuntimeError("BITABLE_APP_TOKEN and BITABLE_PENDING_TABLE_ID are not configured.")
    return _update_bitable_record(app_token, table_id, str(task.get("record_id") or ""), fields)


def _mark_pending_task_status(task: dict[str, Any], status: str) -> dict[str, Any]:
    settings = _settings()
    return _update_pending_task(task, {_pending_field(settings, "status"): status})


def _load_today_intake_summary(message: dict[str, str]) -> dict[str, Any]:
    settings = _settings()
    app_token = settings["bitable_app_token"]
    table_id = settings["bitable_intake_table_id"]
    if not app_token or not table_id:
        raise RuntimeError("BITABLE_APP_TOKEN and BITABLE_INTAKE_TABLE_ID are not configured.")

    date_field = _intake_field(settings, "date")
    user_id_field = _intake_field(settings, "user_id")
    status_field = _intake_field(settings, "status")
    meal_type_field = _intake_field(settings, "meal_type")
    food_name_field = _intake_field(settings, "food_name")
    grams_field = _intake_field(settings, "grams")
    carbs_field = _intake_field(settings, "carbs")
    protein_field = _intake_field(settings, "protein")
    fat_field = _intake_field(settings, "fat")
    kcal_field = _intake_field(settings, "kcal")
    message_id_field = _intake_field(settings, "message_id")
    created_at_field = _intake_field(settings, "created_at")

    field_names = _unique_non_empty(
        [
            date_field,
            user_id_field,
            status_field,
            meal_type_field,
            food_name_field,
            grams_field,
            carbs_field,
            protein_field,
            fat_field,
            kcal_field,
            message_id_field,
            created_at_field,
        ]
    )
    records = _list_bitable_records(app_token, table_id, field_names=field_names)

    user_id = str(message.get("user_id") or "").strip()
    items: list[dict[str, Any]] = []
    totals = {"carbs": 0.0, "protein": 0.0, "fat": 0.0, "kcal": 0.0}

    for record in records:
        fields = record.get("fields")
        if not isinstance(fields, dict):
            continue

        row_user_id = _extract_text(fields.get(user_id_field))
        if user_id and row_user_id and row_user_id != user_id:
            continue
        if not _is_today_date_value(fields.get(date_field), settings):
            continue
        if not _is_valid_intake_status(fields.get(status_field)):
            continue

        nutrition = {
            "carbs": _extract_number(fields.get(carbs_field)) or 0.0,
            "protein": _extract_number(fields.get(protein_field)) or 0.0,
            "fat": _extract_number(fields.get(fat_field)) or 0.0,
            "kcal": _extract_number(fields.get(kcal_field)) or 0.0,
        }
        for key in totals:
            totals[key] = _round_number(totals[key] + nutrition[key])

        items.append(
            {
                "record_id": str(record.get("record_id") or ""),
                "message_id": _extract_text(fields.get(message_id_field)),
                "meal_type": _extract_text(fields.get(meal_type_field)),
                "food_name": _extract_text(fields.get(food_name_field)),
                "grams": _extract_number(fields.get(grams_field)) or 0.0,
                "created_at": _extract_number(fields.get(created_at_field)) or 0.0,
                "nutrition": nutrition,
            }
        )

    items.sort(key=lambda item: (float(item.get("created_at") or 0), str(item.get("record_id") or "")))

    return {
        "date": _today_string(),
        "count": len(items),
        "items": items,
        "totals": totals,
        "source": "bitable",
    }


def _intake_record_to_summary_item(
    record: dict[str, Any],
    fields: dict[str, Any],
    settings: dict[str, str],
) -> dict[str, Any]:
    nutrition = {
        "carbs": _extract_number(fields.get(_intake_field(settings, "carbs"))) or 0.0,
        "protein": _extract_number(fields.get(_intake_field(settings, "protein"))) or 0.0,
        "fat": _extract_number(fields.get(_intake_field(settings, "fat"))) or 0.0,
        "kcal": _extract_number(fields.get(_intake_field(settings, "kcal"))) or 0.0,
    }
    return {
        "record_id": str(record.get("record_id") or ""),
        "message_id": _extract_text(fields.get(_intake_field(settings, "message_id"))),
        "meal_type": _extract_text(fields.get(_intake_field(settings, "meal_type"))),
        "food_name": _extract_text(fields.get(_intake_field(settings, "food_name"))),
        "grams": _extract_number(fields.get(_intake_field(settings, "grams"))) or 0.0,
        "created_at": _extract_number(fields.get(_intake_field(settings, "created_at"))) or 0.0,
        "nutrition": nutrition,
    }


def _intake_record_sort_value(fields: dict[str, Any], settings: dict[str, str]) -> float:
    created_at_field = _intake_field(settings, "created_at")
    created_at = _extract_number(fields.get(created_at_field)) if created_at_field else None
    if created_at is not None:
        return created_at
    return _extract_number(fields.get(_intake_field(settings, "date"))) or 0.0


def _find_last_valid_intake_record(message: dict[str, str]) -> dict[str, Any] | None:
    settings = _settings()
    app_token = settings["bitable_app_token"]
    table_id = settings["bitable_intake_table_id"]
    if not app_token or not table_id:
        raise RuntimeError("BITABLE_APP_TOKEN and BITABLE_INTAKE_TABLE_ID are not configured.")

    field_names = _unique_non_empty(
        [
            _intake_field(settings, "date"),
            _intake_field(settings, "user_id"),
            _intake_field(settings, "status"),
            _intake_field(settings, "meal_type"),
            _intake_field(settings, "food_name"),
            _intake_field(settings, "grams"),
            _intake_field(settings, "carbs"),
            _intake_field(settings, "protein"),
            _intake_field(settings, "fat"),
            _intake_field(settings, "kcal"),
            _intake_field(settings, "message_id"),
            _intake_field(settings, "created_at"),
        ]
    )
    records = _list_bitable_records(app_token, table_id, field_names=field_names)
    user_id = str(message.get("user_id") or "").strip()
    user_id_field = _intake_field(settings, "user_id")
    status_field = _intake_field(settings, "status")

    best_key: tuple[float, int] | None = None
    best_item: dict[str, Any] | None = None
    for index, record in enumerate(records):
        fields = record.get("fields")
        if not isinstance(fields, dict):
            continue

        row_user_id = _extract_text(fields.get(user_id_field))
        if user_id and row_user_id and row_user_id != user_id:
            continue
        if not _is_valid_intake_status(fields.get(status_field)):
            continue

        sort_key = (_intake_record_sort_value(fields, settings), index)
        if best_key is None or sort_key > best_key:
            best_key = sort_key
            best_item = _intake_record_to_summary_item(record, fields, settings)

    return best_item


def _undo_last_intake_record(message: dict[str, str]) -> dict[str, Any]:
    settings = _settings()
    app_token = settings["bitable_app_token"]
    table_id = settings["bitable_intake_table_id"]
    if not app_token or not table_id:
        raise RuntimeError("BITABLE_APP_TOKEN and BITABLE_INTAKE_TABLE_ID are not configured.")

    item = _find_last_valid_intake_record(message)
    if not item:
        return {"status": "nothing_to_undo", "item": None}

    status_field = _intake_field(settings, "status")
    data = _update_bitable_record(app_token, table_id, item["record_id"], {status_field: "deleted"})
    return {
        "status": "deleted",
        "item": item,
        "update_result": data,
    }


def _default_daily_standard(source: str = "default") -> dict[str, Any]:
    return {
        "carbs": DEFAULT_DAILY_STANDARD["carbs"],
        "protein": DEFAULT_DAILY_STANDARD["protein"],
        "fat": DEFAULT_DAILY_STANDARD["fat"],
        "kcal": DEFAULT_DAILY_STANDARD["kcal"],
        "source": source,
    }


def _standard_date_matches_today(value: Any) -> bool:
    text = _extract_text(value)
    if not text:
        return True

    number = _extract_number(value)
    if number is not None:
        start = _today_start_millis()
        return start <= int(number) < start + 86400000

    return text in {_today_string(), "today", "今日"}


def _daily_standard_from_fields(fields: dict[str, Any], settings: dict[str, str]) -> dict[str, Any]:
    standard = _default_daily_standard(source="bitable")
    carbs = _extract_number(fields.get(_standard_field(settings, "carbs")))
    protein = _extract_number(fields.get(_standard_field(settings, "protein")))
    fat = _extract_number(fields.get(_standard_field(settings, "fat")))
    kcal = _extract_number(fields.get(_standard_field(settings, "kcal")))

    if carbs is not None:
        standard["carbs"] = carbs
    if protein is not None:
        standard["protein"] = protein
    if fat is not None:
        standard["fat"] = fat
    if kcal is None:
        kcal = _round_number(standard["carbs"] * 4 + standard["protein"] * 4 + standard["fat"] * 9)
    standard["kcal"] = kcal
    return standard


def _load_daily_standard(message: dict[str, str]) -> dict[str, Any]:
    settings = _settings()
    app_token = settings["bitable_app_token"]
    table_id = settings["bitable_standard_table_id"]
    if not table_id:
        return _default_daily_standard()
    if not app_token:
        raise RuntimeError("BITABLE_APP_TOKEN is not configured.")

    user_id_field = _standard_field(settings, "user_id")
    type_field = _standard_field(settings, "type")
    date_field = _standard_field(settings, "date")
    field_names = _unique_non_empty(
        [
            user_id_field,
            type_field,
            date_field,
            _standard_field(settings, "kcal"),
            _standard_field(settings, "carbs"),
            _standard_field(settings, "protein"),
            _standard_field(settings, "fat"),
        ]
    )
    records = _list_bitable_records(app_token, table_id, field_names=field_names)
    user_id = str(message.get("user_id") or "").strip()

    best_score = -1
    best_fields: dict[str, Any] | None = None
    for record in records:
        fields = record.get("fields")
        if not isinstance(fields, dict):
            continue

        row_user_id = _extract_text(fields.get(user_id_field))
        if user_id and row_user_id and row_user_id != user_id:
            continue
        if not _standard_date_matches_today(fields.get(date_field)):
            continue

        standard_type = _extract_text(fields.get(type_field)).lower()
        score = 0
        if user_id and row_user_id == user_id:
            score += 100
        elif not row_user_id:
            score += 10
        if fields.get(date_field) not in (None, ""):
            score += 20
        if standard_type in {"daily", "default", "默认", "每日", "每日标准"}:
            score += 5

        if score > best_score:
            best_score = score
            best_fields = fields

    if best_fields is None:
        return _default_daily_standard()
    return _daily_standard_from_fields(best_fields, settings)


def _parse_standard_values_from_text(text: str) -> dict[str, float | None]:
    patterns = {
        "carbs": r"(?:碳水化合物|碳水|carbs?)\s*[:：]?\s*(\d+(?:\.\d+)?)",
        "protein": r"(?:蛋白质|蛋白|protein)\s*[:：]?\s*(\d+(?:\.\d+)?)",
        "fat": r"(?:脂肪|fat)\s*[:：]?\s*(\d+(?:\.\d+)?)",
    }
    values: dict[str, float | None] = {"carbs": None, "protein": None, "fat": None}
    for key, pattern in patterns.items():
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if match:
            values[key] = _number_or_none(match.group(1))
    return values


def _standard_values_from_parse_result(
    message: dict[str, str],
    parse_result: dict[str, Any],
) -> dict[str, float]:
    current = _load_daily_standard(message)
    parsed_standard = parse_result.get("standard")
    if not isinstance(parsed_standard, dict):
        parsed_standard = {}
    fallback = _parse_standard_values_from_text(message.get("text", ""))

    values: dict[str, float] = {}
    for key in ("carbs", "protein", "fat"):
        value = parsed_standard.get(key)
        if value is None:
            value = fallback.get(key)
        if value is None:
            value = current.get(key)
        values[key] = _round_number(float(value or 0))

    values["kcal"] = _round_number(values["carbs"] * 4 + values["protein"] * 4 + values["fat"] * 9)
    return values


def _find_user_standard_record(message: dict[str, str]) -> dict[str, Any] | None:
    settings = _settings()
    app_token = settings["bitable_app_token"]
    table_id = settings["bitable_standard_table_id"]
    if not table_id:
        raise RuntimeError("BITABLE_STANDARD_TABLE_ID is not configured.")
    if not app_token:
        raise RuntimeError("BITABLE_APP_TOKEN is not configured.")

    user_id_field = _standard_field(settings, "user_id")
    type_field = _standard_field(settings, "type")
    date_field = _standard_field(settings, "date")
    field_names = _unique_non_empty(
        [
            user_id_field,
            type_field,
            date_field,
            _standard_field(settings, "kcal"),
            _standard_field(settings, "carbs"),
            _standard_field(settings, "protein"),
            _standard_field(settings, "fat"),
        ]
    )
    records = _list_bitable_records(app_token, table_id, field_names=field_names)
    user_id = str(message.get("user_id") or "").strip()

    best_score = -1
    best_record: dict[str, Any] | None = None
    for record in records:
        fields = record.get("fields")
        if not isinstance(fields, dict):
            continue

        row_user_id = _extract_text(fields.get(user_id_field))
        if user_id and row_user_id != user_id:
            continue
        if not user_id and row_user_id:
            continue
        if not _standard_date_matches_today(fields.get(date_field)):
            continue

        standard_type = _extract_text(fields.get(type_field)).lower()
        score = 0
        if standard_type in {"daily", "default", "默认", "每日", "每日标准"}:
            score += 10
        if fields.get(date_field) not in (None, ""):
            score += 5

        if score > best_score:
            best_score = score
            best_record = record

    return best_record


def _standard_client_token_for_message(message: dict[str, str]) -> str:
    key = _message_key(message)
    return _stable_uuid4_from_text(f"calorie-agent:{key}:standard")


def _upsert_daily_standard(message: dict[str, str], parse_result: dict[str, Any]) -> dict[str, Any]:
    settings = _settings()
    app_token = settings["bitable_app_token"]
    table_id = settings["bitable_standard_table_id"]
    if not table_id:
        raise RuntimeError("BITABLE_STANDARD_TABLE_ID is not configured.")
    if not app_token:
        raise RuntimeError("BITABLE_APP_TOKEN is not configured.")

    standard = _standard_values_from_parse_result(message, parse_result)
    user_id = str(message.get("user_id") or "").strip()
    fields = {
        _standard_field(settings, "type"): "default",
        _standard_field(settings, "carbs"): standard["carbs"],
        _standard_field(settings, "protein"): standard["protein"],
        _standard_field(settings, "fat"): standard["fat"],
        _standard_field(settings, "kcal"): standard["kcal"],
    }
    user_id_field = _standard_field(settings, "user_id")
    if user_id_field:
        fields[user_id_field] = user_id

    existing = _find_user_standard_record(message)
    if existing:
        data = _update_bitable_record(app_token, table_id, str(existing.get("record_id") or ""), fields)
        return {
            "status": "updated",
            "record_id": str(existing.get("record_id") or ""),
            "standard": standard,
            "update_result": data,
        }

    data = _batch_create_bitable_records(
        app_token,
        table_id,
        [{"fields": fields}],
        client_token=_standard_client_token_for_message(message),
    )
    body = data.get("data") if isinstance(data.get("data"), dict) else {}
    records = body.get("records") if isinstance(body.get("records"), list) else []
    record_id = str(records[0].get("record_id") or "") if records and isinstance(records[0], dict) else ""
    return {
        "status": "created",
        "record_id": record_id,
        "standard": standard,
        "create_result": data,
    }


def _intake_record_exists(message_id: str) -> bool:
    if not message_id:
        return False

    settings = _settings()
    app_token = settings["bitable_app_token"]
    table_id = settings["bitable_intake_table_id"]
    if not app_token or not table_id:
        raise RuntimeError("BITABLE_APP_TOKEN and BITABLE_INTAKE_TABLE_ID are not configured.")

    message_id_field = _intake_field(settings, "message_id")
    records = _list_bitable_records(app_token, table_id, field_names=[message_id_field])
    for record in records:
        fields = record.get("fields")
        if not isinstance(fields, dict):
            continue
        if _extract_text(fields.get(message_id_field)) == message_id:
            return True
    return False


def _build_intake_bitable_record(
    message: dict[str, str],
    parse_result: dict[str, Any],
    item: dict[str, Any],
    status: str = "valid",
) -> dict[str, Any]:
    settings = _settings()
    nutrition = item["nutrition"]
    date_value: int | str = _today_start_millis()
    if settings.get("bitable_field_intake_date_value_type") == "text":
        date_value = _today_string()

    fields = {
        _intake_field(settings, "date"): date_value,
        _intake_field(settings, "user_id"): message.get("user_id", ""),
        _intake_field(settings, "message_id"): message.get("message_id", ""),
        _intake_field(settings, "raw_text"): message.get("text", ""),
        _intake_field(settings, "meal_type"): parse_result.get("meal_type", "unknown"),
        _intake_field(settings, "food_id"): item.get("food_id", ""),
        _intake_field(settings, "food_name"): item.get("matched_name", ""),
        _intake_field(settings, "grams"): item.get("grams", 0),
        _intake_field(settings, "carbs"): nutrition["carbs"],
        _intake_field(settings, "protein"): nutrition["protein"],
        _intake_field(settings, "fat"): nutrition["fat"],
        _intake_field(settings, "kcal"): nutrition["kcal"],
        _intake_field(settings, "status"): status,
    }
    created_at_field = _intake_field(settings, "created_at")
    if created_at_field:
        fields[created_at_field] = _now_millis()
    return {"fields": fields}


def _write_intake_records(
    message: dict[str, str],
    parse_result: dict[str, Any],
    lookup_result: dict[str, Any],
) -> dict[str, Any]:
    settings = _settings()
    app_token = settings["bitable_app_token"]
    table_id = settings["bitable_intake_table_id"]
    if not app_token or not table_id:
        raise RuntimeError("BITABLE_APP_TOKEN and BITABLE_INTAKE_TABLE_ID are not configured.")

    if _intake_record_exists(message.get("message_id", "")):
        return {
            "status": "duplicate_skipped",
            "created_count": 0,
            "records": [],
        }

    records = [
        _build_intake_bitable_record(message, parse_result, item)
        for item in lookup_result.get("items", [])
    ]
    if not records:
        return {
            "status": "nothing_to_write",
            "created_count": 0,
            "records": [],
        }

    client_token = _bitable_client_token_for_message(message)
    data = _batch_create_bitable_records(app_token, table_id, records, client_token=client_token)
    body = data.get("data") if isinstance(data.get("data"), dict) else {}
    created_records = body.get("records") if isinstance(body.get("records"), list) else []
    return {
        "status": "created",
        "created_count": len(created_records) or len(records),
        "records": created_records,
    }


def _parse_grams_from_text(text: str) -> float | None:
    normalized = unicodedata.normalize("NFKC", text or "").strip().lower()
    unit_patterns = [
        (r"(\d+(?:\.\d+)?)\s*(?:kg|公斤|千克)", 1000),
        (r"(\d+(?:\.\d+)?)\s*(?:斤)", 500),
        (r"(\d+(?:\.\d+)?)\s*(?:g|克|公克)", 1),
    ]
    for pattern, multiplier in unit_patterns:
        match = re.search(pattern, normalized)
        if match:
            return _round_number(float(match.group(1)) * multiplier)

    if re.fullmatch(r"\d+(?:\.\d+)?", normalized):
        return _round_number(float(normalized))
    return None


def _parse_per_100g_macros_from_text(text: str) -> dict[str, float] | None:
    values = _parse_standard_values_from_text(text)
    if values["carbs"] is None or values["protein"] is None or values["fat"] is None:
        return None
    return {
        "carbs": float(values["carbs"]),
        "protein": float(values["protein"]),
        "fat": float(values["fat"]),
    }


def _pending_parse_result(task: dict[str, Any], grams: float) -> dict[str, Any]:
    return {
        "intent": "add_food",
        "date": "today",
        "meal_type": task.get("meal_type") or "unknown",
        "foods": [{"name": task.get("food_name", ""), "grams": grams}],
        "standard": {"carbs": None, "protein": None, "fat": None},
        "missing": [],
        "notes": "",
    }


def _pending_write_message(message: dict[str, str], task: dict[str, Any]) -> dict[str, str]:
    raw_text = str(task.get("raw_text") or "").strip()
    supplement = str(message.get("text") or "").strip()
    combined = f"{raw_text} | 补充：{supplement}" if raw_text else supplement
    return {
        "message_id": message.get("message_id", ""),
        "chat_id": message.get("chat_id", ""),
        "user_id": message.get("user_id", ""),
        "text": combined,
    }


def _record_pending_food_intake(
    message: dict[str, str],
    task: dict[str, Any],
    food: dict[str, Any],
    grams: float,
) -> dict[str, Any]:
    parse_result = _pending_parse_result(task, grams)
    lookup_result = _lookup_result_from_food(food, str(task.get("food_name") or food["name"]), grams)
    write_message = _pending_write_message(message, task)
    intake_write_result = _write_intake_records(write_message, parse_result, lookup_result)
    _mark_pending_task_status(task, "completed")
    today_summary = _load_today_intake_summary(message)
    daily_standard = _load_daily_standard(message)
    return {
        "parse_result": parse_result,
        "lookup_result": lookup_result,
        "intake_write_result": intake_write_result,
        "today_summary": today_summary,
        "daily_standard": daily_standard,
    }


def _complete_missing_grams_task(message: dict[str, str], task: dict[str, Any]) -> dict[str, Any]:
    grams = _parse_grams_from_text(message.get("text", ""))
    food_name = str(task.get("food_name") or "").strip()
    if grams is None:
        return {
            "status": "still_pending",
            "reply": f"请补充{food_name}的克重，例如：200g",
        }

    food = _match_food(food_name, _load_food_database())
    if not food:
        return {
            "status": "still_pending",
            "reply": f"食物库没有{food_name}，请提供每100g碳水/蛋白质/脂肪，例如：碳水10 蛋白1 脂肪20",
        }

    result = _record_pending_food_intake(message, task, food, grams)
    result["status"] = "completed"
    result["reply"] = _format_pending_recorded_reply([], result)
    return result


def _complete_default_grams_task(message: dict[str, str], task: dict[str, Any]) -> dict[str, Any]:
    grams = _parse_grams_from_text(message.get("text", ""))
    if grams is None:
        grams = _extract_number(task.get("grams"))
    food_name = str(task.get("food_name") or "").strip()
    if grams is None:
        return {
            "status": "still_pending",
            "reply": f"璇风‘璁ゆ槸鍚︽寜寤鸿鍏嬮噸璁板綍{food_name}锛屾垨鐩存帴鍙戦€佸厠閲嶏紝渚嬪 200g",
        }

    food = _match_food(food_name, _load_food_database())
    if not food:
        return {
            "status": "still_pending",
            "reply": f"食物库没有{food_name}，请提供每100g碳水/蛋白质/脂肪，例如：碳水10 蛋白1 脂肪20",
        }

    result = _record_pending_food_intake(message, task, food, float(grams))
    result["status"] = "completed"
    result["reply"] = _format_pending_recorded_reply([], result)
    return result


def _complete_missing_food_task(message: dict[str, str], task: dict[str, Any]) -> dict[str, Any]:
    food_name = str(task.get("food_name") or "").strip()
    per_100g = _parse_per_100g_macros_from_text(message.get("text", ""))
    if per_100g is None:
        return {
            "status": "still_pending",
            "reply": f"食物库没有{food_name}，请提供每100g碳水/蛋白质/脂肪，例如：碳水10 蛋白1 脂肪20",
        }

    food = _match_food(food_name, _load_food_database())
    created_food = False
    if not food:
        food = _create_food_database_record(food_name, per_100g)
        created_food = True

    grams = task.get("grams")
    if grams is None:
        settings = _settings()
        _update_pending_task(task, {_pending_field(settings, "intent"): "missing_grams"})
        prefix = f"已新增食物：{food_name}\n" if created_food else ""
        return {
            "status": "still_pending",
            "reply": prefix + f"请补充{food_name}的克重，例如：200g",
        }

    result = _record_pending_food_intake(message, task, food, float(grams))
    result["status"] = "completed"
    headers = [f"已新增食物：{food_name}"] if created_food else []
    result["reply"] = _format_pending_recorded_reply(headers, result)
    return result


def _complete_pending_task(message: dict[str, str], task: dict[str, Any]) -> dict[str, Any]:
    intent = str(task.get("intent") or "")
    if intent == "missing_grams":
        return _complete_missing_grams_task(message, task)
    if intent == "confirm_default_grams":
        return _complete_default_grams_task(message, task)
    if intent == "missing_food":
        return _complete_missing_food_task(message, task)
    _mark_pending_task_status(task, "cancelled")
    return {
        "status": "cancelled",
        "reply": "待确认任务类型不支持，已取消。请重新发送食物信息。",
    }


def _extract_deepseek_content(data: dict[str, Any]) -> str:
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise RuntimeError(f"DeepSeek response does not include choices: {data}")

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise RuntimeError(f"DeepSeek choice is invalid: {data}")

    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise RuntimeError(f"DeepSeek choice does not include message: {data}")

    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError(f"DeepSeek message content is empty: {data}")

    return content.strip()


def _parse_json_object(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise RuntimeError(f"DeepSeek did not return a JSON object: {text}") from None
        try:
            parsed = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"DeepSeek JSON output is invalid: {text}") from exc

    if not isinstance(parsed, dict):
        raise RuntimeError(f"DeepSeek JSON output is not an object: {text}")

    return parsed


def _number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return _round_number(number)


def _round_number(value: float) -> float:
    return math.floor(float(value) * 10 + 0.5 + 1e-9) / 10


def _normalize_parse_result(parsed: dict[str, Any]) -> dict[str, Any]:
    intent = str(parsed.get("intent") or "unknown").strip()
    if intent not in SUPPORTED_INTENTS:
        intent = "unknown"

    meal_type = str(parsed.get("meal_type") or "unknown").strip()
    if meal_type not in SUPPORTED_MEAL_TYPES:
        meal_type = "unknown"

    foods: list[dict[str, Any]] = []
    raw_foods = parsed.get("foods")
    if isinstance(raw_foods, list):
        for raw_food in raw_foods:
            if not isinstance(raw_food, dict):
                continue
            name = str(raw_food.get("name") or "").strip()
            if not name:
                continue
            foods.append({"name": name, "grams": _number_or_none(raw_food.get("grams"))})

    raw_standard = parsed.get("standard")
    if not isinstance(raw_standard, dict):
        raw_standard = {}

    standard = {
        "carbs": _number_or_none(raw_standard.get("carbs")),
        "protein": _number_or_none(raw_standard.get("protein")),
        "fat": _number_or_none(raw_standard.get("fat")),
    }

    raw_missing = parsed.get("missing")
    missing = [str(item).strip() for item in raw_missing if str(item).strip()] if isinstance(raw_missing, list) else []

    return {
        "intent": intent,
        "date": str(parsed.get("date") or "today").strip() or "today",
        "meal_type": meal_type,
        "foods": foods,
        "standard": standard,
        "missing": missing,
        "notes": str(parsed.get("notes") or "").strip(),
    }


def _parse_user_text_with_deepseek(text: str) -> dict[str, Any]:
    settings = _settings()
    api_key = settings["deepseek_api_key"]
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is not configured.")

    model = settings["deepseek_model"] or DEFAULT_SETTINGS["DEEPSEEK_MODEL"]
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": PARSER_SYSTEM_PROMPT},
            {"role": "user", "content": text},
        ],
        "temperature": 0,
        "max_tokens": 800,
        "stream": False,
        "response_format": {"type": "json_object"},
        "thinking": {"type": "disabled"},
    }
    data = _http_json_post(
        f"{DEEPSEEK_API_BASE}/chat/completions",
        payload,
        {"Authorization": f"Bearer {api_key}"},
    )
    if "error" in data:
        raise RuntimeError(f"DeepSeek API error: {data['error']}")

    content = _extract_deepseek_content(data)
    return _normalize_parse_result(_parse_json_object(content))


def _format_parse_result_reply(result: dict[str, Any]) -> str:
    intent_labels = {
        "add_food": "添加摄入",
        "change_standard": "修改每日标准",
        "undo_last": "撤销上一条",
        "query_today": "查询今日",
        "unknown": "未识别",
    }
    meal_labels = {
        "breakfast": "早餐",
        "lunch": "午餐",
        "dinner": "晚餐",
        "post_workout": "练后餐",
        "unknown": "未知",
    }

    intent = result["intent"]
    lines = [
        "识别结果：",
        f"意图：{intent_labels.get(intent, intent)}",
    ]

    if intent == "add_food":
        lines.append(f"餐次：{meal_labels.get(result['meal_type'], '未知')}")
        lines.append("食物：")
        if result["foods"]:
            for food in result["foods"]:
                grams = food.get("grams")
                grams_text = "克重缺失" if grams is None else f"{grams:g}g"
                lines.append(f"- {food['name']} {grams_text}")
        else:
            lines.append("- 未识别到食物")
    elif intent == "change_standard":
        standard = result["standard"]
        lines.append("每日标准：")
        lines.append(f"- 碳水：{_format_optional_grams(standard['carbs'])}")
        lines.append(f"- 蛋白质：{_format_optional_grams(standard['protein'])}")
        lines.append(f"- 脂肪：{_format_optional_grams(standard['fat'])}")
    elif intent in {"undo_last", "query_today"}:
        lines.append("下一步将接入多维表格后执行该操作。")
    else:
        lines.append("暂时无法判断你的操作，请换一种说法。")

    if result["missing"]:
        lines.append("缺失信息：" + "、".join(result["missing"]))

    if result["notes"]:
        lines.append("备注：" + result["notes"])

    lines.append("")
    lines.append("当前阶段：仅完成 DeepSeek 结构化解析，尚未写入多维表格。")
    return "\n".join(lines)


def _format_progress_bar(value: float, target: float, width: int = 10) -> str:
    if target <= 0:
        return "[----------] n/a"
    percent = max(value / target * 100, 0)
    filled = min(width, int(round(percent / 100 * width)))
    return f"[{'#' * filled}{'-' * (width - filled)}] {percent:.1f}%"


def _format_standard_source(source: str) -> str:
    return "默认值" if source == "default" else "多维表格"


def _meal_label(meal_type: str) -> str:
    return {
        "breakfast": "早餐",
        "lunch": "午餐",
        "dinner": "晚餐",
        "post_workout": "练后餐",
        "unknown": "未知餐次",
    }.get(meal_type, meal_type or "未知餐次")


def _format_today_items_lines(today_summary: dict[str, Any]) -> list[str]:
    items = today_summary.get("items")
    lines = ["", "今日清单："]
    if not isinstance(items, list) or not items:
        lines.append("- 暂无有效记录")
        return lines

    for item in items:
        nutrition = item.get("nutrition") if isinstance(item.get("nutrition"), dict) else {}
        food_name = str(item.get("food_name") or "未命名食物")
        grams = float(item.get("grams") or 0)
        meal_type = str(item.get("meal_type") or "unknown")
        lines.append(
            f"- {_meal_label(meal_type)} {food_name} {grams:g}g："
            f"{float(nutrition.get('kcal') or 0):g} kcal，"
            f"碳水 {float(nutrition.get('carbs') or 0):g}g，"
            f"蛋白质 {float(nutrition.get('protein') or 0):g}g，"
            f"脂肪 {float(nutrition.get('fat') or 0):g}g"
        )
    return lines


def _format_today_summary_lines(today_summary: dict[str, Any], daily_standard: dict[str, Any]) -> list[str]:
    totals = today_summary["totals"]
    date_text = today_summary.get("date") or _today_string()
    count = int(today_summary.get("count") or 0)
    lines = [
        "",
        "今日累计：",
        f"日期：{date_text}，记录 {count} 条",
        f"标准来源：{_format_standard_source(str(daily_standard.get('source') or 'default'))}",
    ]

    metrics = [
        ("kcal", "热量", "kcal"),
        ("carbs", "碳水", "g"),
        ("protein", "蛋白质", "g"),
        ("fat", "脂肪", "g"),
    ]
    for key, label, unit in metrics:
        value = float(totals.get(key) or 0)
        target = float(daily_standard.get(key) or 0)
        lines.append(f"{label}：{value:g}/{target:g} {unit} {_format_progress_bar(value, target)}")

    remaining = {
        key: max(_round_number(float(daily_standard.get(key) or 0) - float(totals.get(key) or 0)), 0)
        for key, _, _ in metrics
    }
    lines.append(
        "剩余："
        f"热量 {remaining['kcal']:g} kcal，"
        f"碳水 {remaining['carbs']:g}g，"
        f"蛋白质 {remaining['protein']:g}g，"
        f"脂肪 {remaining['fat']:g}g"
    )
    return lines


def _format_today_query_reply(today_summary: dict[str, Any], daily_standard: dict[str, Any]) -> str:
    lines = ["今日摄入汇总："]
    lines.extend(_format_today_items_lines(today_summary))
    lines.extend(_format_today_summary_lines(today_summary, daily_standard)[1:])
    return "\n".join(lines)


def _format_intake_item_inline(item: dict[str, Any]) -> str:
    nutrition = item.get("nutrition") if isinstance(item.get("nutrition"), dict) else {}
    return (
        f"{_meal_label(str(item.get('meal_type') or 'unknown'))} "
        f"{str(item.get('food_name') or '未命名食物')} "
        f"{float(item.get('grams') or 0):g}g："
        f"{float(nutrition.get('kcal') or 0):g} kcal，"
        f"碳水 {float(nutrition.get('carbs') or 0):g}g，"
        f"蛋白质 {float(nutrition.get('protein') or 0):g}g，"
        f"脂肪 {float(nutrition.get('fat') or 0):g}g"
    )


def _format_undo_last_reply(
    undo_result: dict[str, Any],
    today_summary: dict[str, Any],
    daily_standard: dict[str, Any],
) -> str:
    status = undo_result.get("status")
    lines = ["撤销结果："]
    if status == "deleted" and isinstance(undo_result.get("item"), dict):
        lines.append("已撤销：" + _format_intake_item_inline(undo_result["item"]))
    elif status == "nothing_to_undo":
        lines.append("没有可撤销的有效摄入记录")
    else:
        lines.append(str(status or "未知"))

    lines.extend(_format_today_items_lines(today_summary))
    lines.extend(_format_today_summary_lines(today_summary, daily_standard)[1:])
    return "\n".join(lines)


def _format_change_standard_reply(standard_write_result: dict[str, Any]) -> str:
    status = standard_write_result.get("status")
    status_label = "已更新" if status == "updated" else "已创建" if status == "created" else str(status or "未知")
    standard = standard_write_result.get("standard")
    if not isinstance(standard, dict):
        standard = _default_daily_standard()

    return "\n".join(
        [
            "每日标准已修改：",
            f"写入状态：{status_label}",
            f"碳水：{float(standard.get('carbs') or 0):g}g",
            f"蛋白质：{float(standard.get('protein') or 0):g}g",
            f"脂肪：{float(standard.get('fat') or 0):g}g",
            f"每日总热量：{float(standard.get('kcal') or 0):g} kcal",
        ]
    )


def _format_control_error_reply(title: str, error: Exception) -> str:
    return "\n".join(
        [
            f"{title}失败：",
            str(error),
            "",
            "请检查多维表格字段映射、应用编辑权限，以及多维表格是否已授权给该飞书应用。",
        ]
    )


def _format_today_summary_error_reply(error: Exception) -> str:
    return "\n".join(
        [
            "今日摄入汇总失败：",
            str(error),
            "",
            "请检查摄入表、标准表字段映射，以及多维表格是否已授权给该飞书应用。",
        ]
    )


def _format_food_lookup_reply(
    parse_result: dict[str, Any],
    lookup_result: dict[str, Any],
    intake_write_result: dict[str, Any] | None = None,
    today_summary: dict[str, Any] | None = None,
    daily_standard: dict[str, Any] | None = None,
    today_summary_error: Exception | None = None,
) -> str:
    meal_labels = {
        "breakfast": "早餐",
        "lunch": "午餐",
        "dinner": "晚餐",
        "post_workout": "练后餐",
        "unknown": "未知",
    }
    lines = [
        "识别结果：",
        "意图：添加摄入",
        f"餐次：{meal_labels.get(parse_result['meal_type'], '未知')}",
        "",
    ]
    if intake_write_result:
        status = intake_write_result.get("status")
        if status == "created":
            lines.append(f"写入状态：已记录 {intake_write_result.get('created_count', 0)} 条")
        elif status == "duplicate_skipped":
            lines.append("写入状态：该消息已处理过，本次跳过重复写入")
        elif status == "nothing_to_write":
            lines.append("写入状态：没有可写入的有效食物")
        else:
            lines.append(f"写入状态：{status or '未知'}")
    else:
        lines.append("写入状态：尚未写入")

    lines.extend(["", "食物库查询："])

    if lookup_result["items"]:
        for item in lookup_result["items"]:
            nutrition = item["nutrition"]
            per_100g = item["per_100g"]
            lines.append(
                f"- {item['matched_name']} {item['grams']:g}g："
                f"碳水 {nutrition['carbs']:g}g，"
                f"蛋白质 {nutrition['protein']:g}g，"
                f"脂肪 {nutrition['fat']:g}g，"
                f"热量 {nutrition['kcal']:g} kcal"
            )
            lines.append(
                f"  每100g：碳水 {per_100g['carbs']:g}g，"
                f"蛋白质 {per_100g['protein']:g}g，"
                f"脂肪 {per_100g['fat']:g}g"
            )
    else:
        lines.append("- 未匹配到可计算食物")

    if lookup_result["missing"]:
        lines.append("")
        lines.append("待补充：")
        for item in lookup_result["missing"]:
            reason = item.get("reason")
            if reason == "grams_missing":
                lines.append(f"- {item['name']}：缺少克重")
            else:
                grams = item.get("grams")
                grams_text = "" if grams is None else f" {grams:g}g"
                lines.append(f"- {item['name']}{grams_text}：食物库未找到")

    totals = lookup_result["totals"]
    lines.append("")
    lines.append("本次合计：")
    lines.append(f"热量：{totals['kcal']:g} kcal")
    lines.append(
        f"碳水：{totals['carbs']:g}g，"
        f"蛋白质：{totals['protein']:g}g，"
        f"脂肪：{totals['fat']:g}g"
    )
    if today_summary and daily_standard:
        lines.extend(_format_today_summary_lines(today_summary, daily_standard))
    elif today_summary_error:
        lines.extend(["", "今日累计读取失败：", str(today_summary_error)])

    lines.append("")
    write_status = intake_write_result.get("status") if intake_write_result else ""
    if write_status == "created":
        lines.append("当前阶段：已完成每日摄入记录写入，并返回今日累计进度。")
    elif write_status == "duplicate_skipped":
        lines.append("当前阶段：已跳过重复写入，并返回今日累计进度。")
    elif write_status == "nothing_to_write":
        lines.append("当前阶段：未写入记录，请补充缺失信息后再试。")
    else:
        lines.append("当前阶段：已接入每日摄入记录写入流程。")
    return "\n".join(lines)


def _format_pending_prompt_reply(
    pending_result: dict[str, Any],
    intake_write_result: dict[str, Any] | None = None,
    today_summary: dict[str, Any] | None = None,
    daily_standard: dict[str, Any] | None = None,
) -> str:
    intent = str(pending_result.get("intent") or "")
    food_name = str(pending_result.get("food_name") or "该食物")
    lines: list[str] = []
    write_status = intake_write_result.get("status") if intake_write_result else ""
    if write_status == "created":
        lines.append(f"已记录本次可计算食物 {intake_write_result.get('created_count', 0)} 条。")
    elif write_status == "duplicate_skipped":
        lines.append("该消息中可计算的食物已处理过，本次跳过重复写入。")

    if pending_result.get("status") == "not_configured":
        lines.append("待确认任务表未配置，暂时无法保存补全上下文。")

    if intent == "missing_food":
        lines.append(f"食物库没有{food_name}，请提供每100g碳水/蛋白质/脂肪，例如：碳水10 蛋白1 脂肪20")
    elif intent == "missing_grams":
        lines.append(f"请补充{food_name}的克重，例如：200g")
    else:
        lines.append("请补充缺失信息。")

    if today_summary and daily_standard:
        lines.extend(_format_today_summary_lines(today_summary, daily_standard))
    return "\n".join(lines)


def _format_pending_recorded_reply(headers: list[str], result: dict[str, Any]) -> str:
    lookup_result = result.get("lookup_result") if isinstance(result.get("lookup_result"), dict) else {}
    items = lookup_result.get("items") if isinstance(lookup_result.get("items"), list) else []
    lines = [line for line in headers if line]
    if items:
        item = items[0]
        lines.append(f"已记录{item['matched_name']}{float(item['grams']):g}g")
    else:
        lines.append("已记录补全食物")

    intake_write_result = result.get("intake_write_result") if isinstance(result.get("intake_write_result"), dict) else {}
    if intake_write_result.get("status") == "duplicate_skipped":
        lines.append("该补全消息已处理过，本次跳过重复写入。")

    today_summary = result.get("today_summary")
    daily_standard = result.get("daily_standard")
    if isinstance(today_summary, dict) and isinstance(daily_standard, dict):
        lines.extend(_format_today_summary_lines(today_summary, daily_standard))
    return "\n".join(lines)


def _format_intake_write_error_reply(
    parse_result: dict[str, Any],
    lookup_result: dict[str, Any],
    error: Exception,
) -> str:
    return "\n".join(
        [
            _format_food_lookup_reply(parse_result, lookup_result),
            "",
            "每日摄入记录写入失败：",
            str(error),
            "",
            "请检查 BITABLE_INTAKE_TABLE_ID、摄入表字段映射、应用编辑权限，以及多维表格是否已授权给该飞书应用。",
        ]
    )


def _format_food_lookup_error_reply(parse_result: dict[str, Any], error: Exception) -> str:
    return "\n".join(
        [
            _format_parse_result_reply(parse_result),
            "",
            "食物库读取失败：",
            str(error),
            "",
            "请检查 BITABLE_APP_TOKEN、BITABLE_FOOD_TABLE_ID、应用权限，以及多维表格是否已授权给该飞书应用。",
        ]
    )


def _format_optional_grams(value: float | None) -> str:
    return "未提供" if value is None else f"{value:g}g"


def _format_parse_error_reply(error: Exception) -> str:
    return "\n".join(
        [
            "解析失败：",
            str(error),
            "",
            "请检查 DEEPSEEK_API_KEY、DEEPSEEK_MODEL 和云函数外网访问配置。",
        ]
    )


def _handle_add_food_command(message: dict[str, str], parse_result: dict[str, Any]) -> dict[str, Any]:
    lookup_result = _enrich_add_food_with_food_database(parse_result)
    try:
        intake_write_result = _write_intake_records(message, parse_result, lookup_result)
    except (RuntimeError, urllib.error.URLError, TimeoutError) as write_exc:
        return {
            "status": "intake_write_failed",
            "reply": _format_intake_write_error_reply(parse_result, lookup_result, write_exc),
            "lookup_result": lookup_result,
            "intake_write_result": {},
            "today_summary": {},
            "daily_standard": {},
            "pending_result": {},
        }
    today_summary: dict[str, Any] = {}
    daily_standard: dict[str, Any] = {}
    pending_result: dict[str, Any] = {}

    missing = lookup_result.get("missing") if isinstance(lookup_result.get("missing"), list) else []
    if missing:
        pending_result = _create_pending_task_for_missing(message, parse_result, missing[0])
        if lookup_result.get("items"):
            today_summary = _load_today_intake_summary(message)
            daily_standard = _load_daily_standard(message)
        return {
            "status": "pending_created" if pending_result.get("status") == "created" else "pending_not_configured",
            "reply": _format_pending_prompt_reply(
                pending_result,
                intake_write_result,
                today_summary=today_summary or None,
                daily_standard=daily_standard or None,
            ),
            "lookup_result": lookup_result,
            "intake_write_result": intake_write_result,
            "today_summary": today_summary,
            "daily_standard": daily_standard,
            "pending_result": pending_result,
        }

    today_summary = _load_today_intake_summary(message)
    daily_standard = _load_daily_standard(message)
    return {
        "status": "today_summary_replied",
        "reply": _format_food_lookup_reply(
            parse_result,
            lookup_result,
            intake_write_result,
            today_summary=today_summary,
            daily_standard=daily_standard,
        ),
        "lookup_result": lookup_result,
        "intake_write_result": intake_write_result,
        "today_summary": today_summary,
        "daily_standard": daily_standard,
        "pending_result": pending_result,
    }


def _build_event_metrics(
    started_at: float,
    parse_status: str,
    parse_result: dict[str, Any],
    lookup_result: dict[str, Any],
    intake_write_result: dict[str, Any],
    pending_result: dict[str, Any],
) -> dict[str, Any]:
    items = lookup_result.get("items") if isinstance(lookup_result.get("items"), list) else []
    missing = lookup_result.get("missing") if isinstance(lookup_result.get("missing"), list) else []
    intake_status = str(intake_write_result.get("status") or "")
    pending_status = str(pending_result.get("status") or "")
    return {
        "event_status": parse_status,
        "intent": parse_result.get("intent", "pending_completion" if pending_result else "unknown"),
        "parse_success": parse_status != "parse_failed",
        "food_match_count": len(items),
        "food_missing_count": len(missing),
        "food_match_success": bool(items) if items or missing else None,
        "pending_status": pending_status or None,
        "pending_completed": pending_status == "completed",
        "intake_write_status": intake_status or None,
        "intake_write_success": (intake_status in {"created", "duplicate_skipped"}) if intake_status else None,
        "duplicate_skipped": intake_status == "duplicate_skipped",
        "bitable_error": parse_status in {
            "food_lookup_failed",
            "intake_write_failed",
            "today_summary_failed",
            "undo_last_failed",
            "standard_update_failed",
            "pending_failed",
        },
        "deepseek_error": parse_status == "parse_failed",
        "reply_latency_ms": int((time.monotonic() - started_at) * 1000),
    }


def _build_processing_diagnostics(
    message: dict[str, str],
    parse_status: str,
    parse_result: dict[str, Any],
    lookup_result: dict[str, Any],
    intake_write_result: dict[str, Any],
    today_summary: dict[str, Any],
    daily_standard: dict[str, Any],
    pending_result: dict[str, Any],
    extra: dict[str, Any] | None,
) -> dict[str, Any]:
    settings = _settings()
    tool_results = extra.get("tool_results") if isinstance(extra, dict) and isinstance(extra.get("tool_results"), list) else []
    execution_facts = extra.get("execution_facts") if isinstance(extra, dict) and isinstance(extra.get("execution_facts"), dict) else {}
    reply_guard = extra.get("reply_guard") if isinstance(extra, dict) and isinstance(extra.get("reply_guard"), dict) else {}
    parse_error = extra.get("parse_error") if isinstance(extra, dict) and isinstance(extra.get("parse_error"), dict) else {}
    steps = [
        {
            "step": "message_received",
            "status": "ok",
            "message_id": message.get("message_id", ""),
            "chat_id": message.get("chat_id", ""),
            "user_id_present": bool(message.get("user_id")),
            "text_length": len(str(message.get("text") or "")),
        },
        {
            "step": "config_loaded",
            "status": _diagnostic_config_status(settings),
            "tables": {
                "food_table_configured": bool(settings.get("bitable_food_table_id")),
                "intake_table_configured": bool(settings.get("bitable_intake_table_id")),
                "pending_table_configured": bool(settings.get("bitable_pending_table_id")),
                "standard_table_configured": bool(settings.get("bitable_standard_table_id")),
                "trace_table_configured": bool(settings.get("bitable_agent_trace_table_id")),
            },
            "deepseek": _diagnostic_deepseek_config(settings),
            "field_map": _diagnostic_field_map(settings),
        },
        {
            "step": "intent_or_plan",
            "status": parse_status,
            "route": _diagnostic_route(parse_result, tool_results),
            "intent": str(parse_result.get("intent") or ""),
            "goal": str(parse_result.get("goal") or ""),
            "action_count": parse_result.get("action_count"),
            "foods": _diagnostic_parse_foods(parse_result),
            "actions": _diagnostic_actions(parse_result),
        },
    ]
    if parse_error:
        steps.append({"step": "parse_error", "status": "failed", **parse_error})
    if lookup_result:
        steps.append({"step": "legacy_food_lookup", **_diagnostic_lookup_result(lookup_result)})
    if tool_results:
        steps.append(
            {
                "step": "agent_tool_results",
                "status": "ok",
                "tool_count": len(tool_results),
                "tools": [_diagnostic_tool_result(item) for item in tool_results if isinstance(item, dict)],
            }
        )
    if pending_result:
        steps.append(
            {
                "step": "pending",
                "status": str(pending_result.get("status") or ""),
                "intent": str(pending_result.get("intent") or ""),
                "food_name": str(pending_result.get("food_name") or ""),
                "grams": pending_result.get("grams"),
                "reason": str(pending_result.get("reason") or pending_result.get("message") or ""),
            }
        )
    if intake_write_result:
        steps.append(
            {
                "step": "intake_write",
                "status": str(intake_write_result.get("status") or ""),
                "created_count": intake_write_result.get("created_count"),
                "record_ids": [
                    str(record.get("record_id") or "")
                    for record in intake_write_result.get("records", [])
                    if isinstance(record, dict)
                ],
            }
        )
    if today_summary or daily_standard:
        steps.append(
            {
                "step": "summary_loaded",
                "status": "ok",
                "today_count": today_summary.get("count") if isinstance(today_summary, dict) else None,
                "today_totals": today_summary.get("totals") if isinstance(today_summary.get("totals"), dict) else {},
                "standard_source": daily_standard.get("source") if isinstance(daily_standard, dict) else "",
            }
        )
    if reply_guard:
        steps.append(
            {
                "step": "reply_guard",
                "status": "passed" if reply_guard.get("passed", True) else "blocked",
                "fallback_used": bool(reply_guard.get("fallback_used")),
                "violation_count": len(reply_guard.get("violations") if isinstance(reply_guard.get("violations"), list) else []),
                "reasons": reply_guard.get("reasons") if isinstance(reply_guard.get("reasons"), list) else [],
            }
        )
    diagnostics = {
        "version": "processing_diagnostics.v1",
        "route": _diagnostic_route(parse_result, tool_results),
        "parse_status": parse_status,
        "food_lookup": _diagnostic_food_lookup(lookup_result, tool_results, execution_facts),
        "steps": steps,
    }
    if parse_error:
        diagnostics["parse_error"] = parse_error
    return diagnostics


def _diagnostic_config_status(settings: dict[str, str]) -> str:
    required = ("bitable_app_token", "bitable_food_table_id", "bitable_intake_table_id")
    missing = [key for key in required if not settings.get(key)]
    return "missing_required_config" if missing else "ok"


def _diagnostic_field_map(settings: dict[str, str]) -> dict[str, str]:
    return {
        "food_id": settings.get("bitable_field_food_id", ""),
        "food_name": settings.get("bitable_field_food_name", ""),
        "food_alias": settings.get("bitable_field_alias", ""),
        "food_carbs": settings.get("bitable_field_carbs", ""),
        "food_protein": settings.get("bitable_field_protein", ""),
        "food_fat": settings.get("bitable_field_fat", ""),
        "food_status": settings.get("bitable_field_status", ""),
        "intake_food_name": settings.get("bitable_field_intake_food_name", ""),
        "intake_grams": settings.get("bitable_field_intake_grams", ""),
        "intake_status": settings.get("bitable_field_intake_status", ""),
    }


def _diagnostic_deepseek_config(settings: dict[str, str]) -> dict[str, Any]:
    api_base = settings.get("deepseek_api_base") or DEFAULT_SETTINGS["DEEPSEEK_API_BASE"]
    model = settings.get("deepseek_model") or DEFAULT_SETTINGS["DEEPSEEK_MODEL"]
    return {
        "api_key_configured": bool(settings.get("deepseek_api_key")),
        "api_base": api_base.rstrip("/"),
        "api_base_source": "env" if os.getenv("DEEPSEEK_API_BASE") else "default",
        "model": model,
        "model_source": "env" if os.getenv("DEEPSEEK_MODEL") else "default",
    }


def _diagnostic_exception(error: Exception, stage: str) -> dict[str, Any]:
    message = _redact_diagnostic_text(str(error))
    return {
        "stage": stage,
        "type": type(error).__name__,
        "message": message,
        "http_status": _diagnostic_http_status(message),
        "category": _diagnostic_error_category(error, message),
    }


def _redact_diagnostic_text(text: str) -> str:
    redacted = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer ***", text)
    redacted = re.sub(r"sk-[A-Za-z0-9._\-]+", "sk-***", redacted)
    redacted = re.sub(r'("api[_-]?key"\s*:\s*")[^"]+(")', r"\1***\2", redacted, flags=re.IGNORECASE)
    return redacted[:1000]


def _diagnostic_http_status(message: str) -> int | None:
    match = re.search(r"\bHTTP\s+(\d{3})\b", message)
    return int(match.group(1)) if match else None


def _diagnostic_error_category(error: Exception, message: str) -> str:
    lowered = message.lower()
    http_status = _diagnostic_http_status(message)
    if "deepseek_api_key is not configured" in lowered:
        return "missing_api_key"
    if isinstance(error, TimeoutError) or "timed out" in lowered:
        return "timeout"
    if isinstance(error, urllib.error.URLError):
        return "network_error"
    if http_status in {401, 403}:
        return "auth_error"
    if http_status == 400:
        return "invalid_request"
    if http_status == 429:
        return "rate_limited"
    if http_status and http_status >= 500:
        return "deepseek_server_error"
    if "model" in lowered:
        return "model_error"
    if "json" in lowered:
        return "invalid_model_response"
    return "unknown"


def _diagnostic_route(parse_result: dict[str, Any], tool_results: list[Any]) -> str:
    if str(parse_result.get("intent") or "") == "agent_runtime" or tool_results:
        return "agent_runtime"
    if pending_intent := str(parse_result.get("intent") or ""):
        return "pending" if pending_intent == "pending" else "legacy_parser"
    return "legacy_parser"


def _diagnostic_parse_foods(parse_result: dict[str, Any]) -> list[dict[str, Any]]:
    foods = parse_result.get("foods") if isinstance(parse_result.get("foods"), list) else []
    return [{"name": str(food.get("name") or ""), "grams": food.get("grams")} for food in foods if isinstance(food, dict)]


def _diagnostic_actions(parse_result: dict[str, Any]) -> list[dict[str, Any]]:
    actions = parse_result.get("actions") if isinstance(parse_result.get("actions"), list) else []
    return [_diagnostic_action(action) for action in actions if isinstance(action, dict)]


def _diagnostic_action(action: dict[str, Any]) -> dict[str, Any]:
    params = action.get("params") if isinstance(action.get("params"), dict) else {}
    target = action.get("target") if isinstance(action.get("target"), dict) else {}
    return {
        "action_id": str(action.get("action_id") or ""),
        "tool": str(action.get("tool") or ""),
        "food_query": str(params.get("food_query") or params.get("food_name") or params.get("name") or target.get("food_query") or target.get("food_name") or ""),
        "grams": params.get("grams") if "grams" in params else params.get("amount_grams"),
        "meal_type": str(params.get("meal_type") or target.get("meal_type") or ""),
        "reason": str(action.get("reason") or ""),
    }


def _diagnostic_lookup_result(lookup_result: dict[str, Any]) -> dict[str, Any]:
    items = lookup_result.get("items") if isinstance(lookup_result.get("items"), list) else []
    missing = lookup_result.get("missing") if isinstance(lookup_result.get("missing"), list) else []
    match_diagnostics = lookup_result.get("match_diagnostics") if isinstance(lookup_result.get("match_diagnostics"), list) else []
    return {
        "status": "ok" if items and not missing else "partial" if items and missing else "missing" if missing else "empty",
        "food_database_count": lookup_result.get("food_database_count"),
        "matched_count": len(items),
        "missing_count": len(missing),
        "items": [_diagnostic_lookup_item(item) for item in items if isinstance(item, dict)],
        "missing": [_diagnostic_missing_item(item) for item in missing if isinstance(item, dict)],
        "match_diagnostics": match_diagnostics,
        "totals": lookup_result.get("totals") if isinstance(lookup_result.get("totals"), dict) else {},
    }


def _diagnostic_lookup_item(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "input_name": str(item.get("input_name") or ""),
        "matched_name": str(item.get("matched_name") or ""),
        "food_id": str(item.get("food_id") or ""),
        "record_id": str(item.get("record_id") or ""),
        "grams": item.get("grams"),
        "nutrition": item.get("nutrition") if isinstance(item.get("nutrition"), dict) else {},
        "per_100g": item.get("per_100g") if isinstance(item.get("per_100g"), dict) else {},
    }


def _diagnostic_missing_item(item: dict[str, Any]) -> dict[str, Any]:
    matched_food = item.get("matched_food") if isinstance(item.get("matched_food"), dict) else {}
    return {
        "name": str(item.get("name") or ""),
        "grams": item.get("grams"),
        "reason": str(item.get("reason") or ""),
        "matched_food_name": str(matched_food.get("name") or ""),
        "matched_food_id": str(matched_food.get("food_id") or ""),
    }


def _diagnostic_tool_result(item: dict[str, Any]) -> dict[str, Any]:
    data = item.get("data") if isinstance(item.get("data"), dict) else {}
    nested = data.get("result") if isinstance(data.get("result"), dict) else {}
    lookup = nested.get("lookup_result") if isinstance(nested.get("lookup_result"), dict) else data.get("lookup_result") if isinstance(data.get("lookup_result"), dict) else {}
    write = nested.get("intake_write_result") if isinstance(nested.get("intake_write_result"), dict) else data.get("intake_write_result") if isinstance(data.get("intake_write_result"), dict) else {}
    pending = data.get("pending_result") if isinstance(data.get("pending_result"), dict) else nested.get("pending_result") if isinstance(nested.get("pending_result"), dict) else {}
    food_match = data.get("food_match") if isinstance(data.get("food_match"), dict) else {}
    return {
        "tool": str(item.get("tool") or ""),
        "action_id": str(item.get("action_id") or ""),
        "status": str(item.get("status") or ""),
        "error": str(item.get("error") or ""),
        "food_match": _diagnostic_food_match(food_match),
        "lookup_result": _diagnostic_lookup_result(lookup) if lookup else {},
        "pending_status": str(pending.get("status") or ""),
        "pending_reason": str(pending.get("reason") or pending.get("message") or ""),
        "intake_write_status": str(write.get("status") or ""),
        "intake_created_count": write.get("created_count"),
    }


def _diagnostic_food_match(food_match: dict[str, Any]) -> dict[str, Any]:
    if not food_match:
        return {}
    candidate = food_match.get("candidate") if isinstance(food_match.get("candidate"), dict) else {}
    candidates = food_match.get("candidates") if isinstance(food_match.get("candidates"), list) else []
    return {
        "status": str(food_match.get("status") or ""),
        "query": str(food_match.get("query") or ""),
        "candidate": _diagnostic_food_candidate(candidate),
        "candidate_count": len(candidates),
        "candidates": [_diagnostic_food_candidate(item) for item in candidates[:5] if isinstance(item, dict)],
    }


def _diagnostic_food_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    food = candidate.get("food") if isinstance(candidate.get("food"), dict) else {}
    return {
        "food_name": str(food.get("name") or ""),
        "food_id": str(food.get("food_id") or ""),
        "record_id": str(food.get("record_id") or ""),
        "score": candidate.get("score"),
        "reason": str(candidate.get("reason") or ""),
        "matched_key": str(candidate.get("matched_key") or ""),
    }


def _diagnostic_food_lookup(
    lookup_result: dict[str, Any],
    tool_results: list[Any],
    execution_facts: dict[str, Any],
) -> dict[str, Any]:
    return {
        "legacy_lookup": _diagnostic_lookup_result(lookup_result) if lookup_result else {},
        "agent_tools": [
            _diagnostic_tool_result(item)
            for item in tool_results
            if isinstance(item, dict) and str(item.get("tool") or "") in {"record_food", "search_food", "suggest_default_grams", "query_intake_food_nutrition"}
        ],
        "recorded_count": len(execution_facts.get("recorded") if isinstance(execution_facts.get("recorded"), list) else []),
        "pending_count": len(execution_facts.get("pending") if isinstance(execution_facts.get("pending"), list) else []),
        "error_count": len(execution_facts.get("errors") if isinstance(execution_facts.get("errors"), list) else []),
    }


def _message_key(message: dict[str, str]) -> str:
    message_id = str(message.get("message_id") or "").strip()
    if message_id:
        return message_id
    return f"{message.get('chat_id', '')}:{message.get('text', '')}"


def _stable_uuid4_from_text(value: str) -> str:
    token_bytes = bytearray(hashlib.sha256(value.encode("utf-8")).digest()[:16])
    token_bytes[6] = (token_bytes[6] & 0x0F) | 0x40
    token_bytes[8] = (token_bytes[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(token_bytes)))


def _reply_uuid_for_message(message: dict[str, str]) -> str:
    key = _message_key(message)
    return _stable_uuid4_from_text(f"calorie-agent:{key}:reply")


def _bitable_client_token_for_message(message: dict[str, str]) -> str:
    key = _message_key(message)
    return _stable_uuid4_from_text(f"calorie-agent:{key}:intake")


def _prune_message_state(now: float) -> None:
    expired = [
        key
        for key, (_, timestamp) in _MESSAGE_STATE.items()
        if now - timestamp > _MESSAGE_STATE_TTL_SECONDS
    ]
    for key in expired:
        _MESSAGE_STATE.pop(key, None)


def _try_mark_message_processing(message: dict[str, str]) -> bool:
    key = _message_key(message)
    if not key:
        return True

    now = time.monotonic()
    with _MESSAGE_STATE_LOCK:
        _prune_message_state(now)
        existing = _MESSAGE_STATE.get(key)
        if existing:
            status, timestamp = existing
            if status == "processing" and now - timestamp > _MESSAGE_PROCESSING_STALE_SECONDS:
                _MESSAGE_STATE[key] = ("processing", now)
                return True
            return False
        _MESSAGE_STATE[key] = ("processing", now)
        return True


def _mark_message_done(message: dict[str, str], status: str) -> None:
    key = _message_key(message)
    if not key:
        return

    with _MESSAGE_STATE_LOCK:
        _MESSAGE_STATE[key] = (status, time.monotonic())


def _finish_text_message_processing(
    message: dict[str, str],
    started_at: float,
    parse_status: str,
    reply: str,
    parse_result: dict[str, Any],
    lookup_result: dict[str, Any] | None = None,
    intake_write_result: dict[str, Any] | None = None,
    today_summary: dict[str, Any] | None = None,
    daily_standard: dict[str, Any] | None = None,
    undo_result: dict[str, Any] | None = None,
    standard_write_result: dict[str, Any] | None = None,
    pending_task: dict[str, Any] | None = None,
    pending_result: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    lookup_result = lookup_result or {}
    intake_write_result = intake_write_result or {}
    today_summary = today_summary or {}
    daily_standard = daily_standard or {}
    undo_result = undo_result or {}
    standard_write_result = standard_write_result or {}
    pending_task = pending_task or {}
    pending_result = pending_result or {}
    settings = _settings()
    metrics = _build_event_metrics(
        started_at,
        parse_status,
        parse_result,
        lookup_result,
        intake_write_result,
        pending_result,
    )
    if extra and isinstance(extra.get("agent_metrics"), dict):
        metrics.update(extra["agent_metrics"])
    diagnostics = _build_processing_diagnostics(
        message,
        parse_status,
        parse_result,
        lookup_result,
        intake_write_result,
        today_summary,
        daily_standard,
        pending_result,
        extra,
    )
    payload = {
        "status": parse_status,
        "message_id": message["message_id"],
        "chat_id": message["chat_id"],
        "parse_result": parse_result,
        "lookup_result": lookup_result,
        "intake_write_result": intake_write_result,
        "today_summary": today_summary,
        "daily_standard": daily_standard,
        "undo_result": undo_result,
        "standard_write_result": standard_write_result,
        "pending_task": pending_task,
        "pending_result": pending_result,
        "metrics": metrics,
        "diagnostics": diagnostics,
    }
    if extra:
        payload.update(extra)

    if not settings["feishu_app_id"] or not settings["feishu_app_secret"]:
        payload["reason"] = "Feishu credentials are not configured."
        payload["reply_preview"] = reply
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        _mark_message_done(message, parse_status)
        return

    payload["send_result"] = _send_feishu_text_message(
        message["chat_id"],
        reply,
        _reply_uuid_for_message(message),
    )
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    _mark_message_done(message, parse_status)


def _process_text_message(message: dict[str, str]) -> None:
    started_at = time.monotonic()
    parse_result: dict[str, Any] = {}
    lookup_result: dict[str, Any] = {}
    intake_write_result: dict[str, Any] = {}
    today_summary: dict[str, Any] = {}
    daily_standard: dict[str, Any] = {}
    undo_result: dict[str, Any] = {}
    standard_write_result: dict[str, Any] = {}
    pending_task: dict[str, Any] = {}
    pending_result: dict[str, Any] = {}
    parse_status = "unknown"
    agent_fallback_metrics: dict[str, Any] = {}
    parse_error: dict[str, Any] = {}

    try:
        settings = _settings()
        if _agent_runtime_enabled(settings):
            try:
                from calorie_agent.agent.runtime import handle_message as _handle_agent_message

                agent_result = _handle_agent_message(message, legacy=sys.modules[__name__], settings=settings)
                _finish_text_message_processing(
                    message,
                    started_at,
                    str(agent_result.get("status") or "agent_runtime_replied"),
                    str(agent_result.get("reply") or ""),
                    agent_result.get("parse_result", {}) if isinstance(agent_result.get("parse_result"), dict) else {},
                    agent_result.get("lookup_result", {}) if isinstance(agent_result.get("lookup_result"), dict) else {},
                    agent_result.get("intake_write_result", {})
                    if isinstance(agent_result.get("intake_write_result"), dict)
                    else {},
                    agent_result.get("today_summary", {}) if isinstance(agent_result.get("today_summary"), dict) else {},
                    agent_result.get("daily_standard", {}) if isinstance(agent_result.get("daily_standard"), dict) else {},
                    agent_result.get("undo_result", {}) if isinstance(agent_result.get("undo_result"), dict) else {},
                    agent_result.get("standard_write_result", {})
                    if isinstance(agent_result.get("standard_write_result"), dict)
                    else {},
                    agent_result.get("pending_task", {}) if isinstance(agent_result.get("pending_task"), dict) else {},
                    agent_result.get("pending_result", {}) if isinstance(agent_result.get("pending_result"), dict) else {},
                    extra={
                        "tool_results": agent_result.get("tool_results", []),
                        "execution_facts": agent_result.get("execution_facts", {}),
                        "reply_guard": agent_result.get("reply_guard", {}),
                        "agent_metrics": agent_result.get("agent_metrics", {}),
                    },
                )
                return
            except (RuntimeError, ValueError, urllib.error.URLError, TimeoutError) as agent_exc:
                if not _agent_fallback_to_legacy(settings):
                    parse_error = _diagnostic_exception(agent_exc, "agent_runtime")
                    _finish_text_message_processing(
                        message,
                        started_at,
                        "agent_runtime_failed",
                        _format_parse_error_reply(agent_exc),
                        {
                            "intent": "agent_runtime",
                            "error": parse_error["message"],
                            "fallback_to_legacy": False,
                        },
                        extra={
                            "agent_metrics": {
                                "agent_runtime_used": True,
                                "planner_success": False,
                                "planner_fallback_used": False,
                                "action_count": 0,
                                "tool_success_count": 0,
                                "tool_failed_count": 0,
                                "reply_guard_passed": False,
                                "reply_guard_fallback_used": False,
                                "agent_eval_version": "agent_eval.v1",
                            },
                            "parse_error": parse_error,
                        },
                    )
                    return
                agent_fallback_metrics = {
                    "agent_runtime_used": True,
                    "planner_success": False,
                    "planner_fallback_used": True,
                    "action_count": 0,
                    "tool_success_count": 0,
                    "tool_failed_count": 0,
                    "reply_guard_passed": None,
                    "reply_guard_fallback_used": False,
                    "agent_eval_version": "agent_eval.v1",
                }
                print(
                    json.dumps(
                        {
                            "status": "agent_runtime_fallback_to_legacy",
                            "message_id": message.get("message_id", ""),
                            "chat_id": message.get("chat_id", ""),
                            "error": str(agent_exc),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

        try:
            pending_should_parse = True
            active_pending = _load_active_pending_task(message)
            if active_pending:
                pending_task = active_pending
                from calorie_agent.domain.pending import classify_pending_message, handle_pending_decision

                pending_decision = classify_pending_message(message, pending_task, legacy=sys.modules[__name__])
                pending_should_parse = pending_decision.get("route") == "pass_through"
                if not pending_should_parse:
                    try:
                        handled_pending = handle_pending_decision(message, pending_decision, legacy=sys.modules[__name__])
                        pending_result = handled_pending.get("result", {}) if isinstance(handled_pending.get("result"), dict) else {}
                        lookup_result = pending_result.get("lookup_result", {}) if isinstance(pending_result.get("lookup_result"), dict) else {}
                        intake_write_result = (
                            pending_result.get("intake_write_result", {})
                            if isinstance(pending_result.get("intake_write_result"), dict)
                            else {}
                        )
                        today_summary = pending_result.get("today_summary", {}) if isinstance(pending_result.get("today_summary"), dict) else {}
                        daily_standard = pending_result.get("daily_standard", {}) if isinstance(pending_result.get("daily_standard"), dict) else {}
                        reply = str(pending_result.get("reply") or "")
                        parse_status = "pending_completed" if pending_result.get("status") == "completed" else f"pending_{handled_pending.get('status', 'handled')}"
                    except (RuntimeError, urllib.error.URLError, TimeoutError) as pending_exc:
                        reply = _format_control_error_reply("补全待确认任务", pending_exc)
                        parse_status = "pending_failed"
            if pending_should_parse:
                parse_result = _parse_user_text_with_deepseek(message["text"])
                if parse_result["intent"] == "add_food":
                    try:
                        add_food_result = _handle_add_food_command(message, parse_result)
                        lookup_result = add_food_result.get("lookup_result", {})
                        intake_write_result = add_food_result.get("intake_write_result", {})
                        today_summary = add_food_result.get("today_summary", {})
                        daily_standard = add_food_result.get("daily_standard", {})
                        pending_result = add_food_result.get("pending_result", {})
                        reply = str(add_food_result.get("reply") or "")
                        parse_status = str(add_food_result.get("status") or "add_food_processed")
                    except (RuntimeError, urllib.error.URLError, TimeoutError) as add_food_exc:
                        if lookup_result:
                            reply = _format_intake_write_error_reply(parse_result, lookup_result, add_food_exc)
                            parse_status = "intake_write_failed"
                        else:
                            reply = _format_food_lookup_error_reply(parse_result, add_food_exc)
                            parse_status = "food_lookup_failed"
                elif parse_result["intent"] == "query_today":
                    try:
                        today_summary = _load_today_intake_summary(message)
                        daily_standard = _load_daily_standard(message)
                        reply = _format_today_query_reply(today_summary, daily_standard)
                        parse_status = "today_summary_replied"
                    except (RuntimeError, urllib.error.URLError, TimeoutError) as summary_exc:
                        reply = _format_today_summary_error_reply(summary_exc)
                        parse_status = "today_summary_failed"
                elif parse_result["intent"] == "undo_last":
                    try:
                        undo_result = _undo_last_intake_record(message)
                        today_summary = _load_today_intake_summary(message)
                        daily_standard = _load_daily_standard(message)
                        reply = _format_undo_last_reply(undo_result, today_summary, daily_standard)
                        parse_status = "undo_last_replied"
                    except (RuntimeError, urllib.error.URLError, TimeoutError) as undo_exc:
                        reply = _format_control_error_reply("撤销上一条", undo_exc)
                        parse_status = "undo_last_failed"
                elif parse_result["intent"] == "change_standard":
                    try:
                        standard_write_result = _upsert_daily_standard(message, parse_result)
                        reply = _format_change_standard_reply(standard_write_result)
                        parse_status = "standard_updated"
                    except (RuntimeError, urllib.error.URLError, TimeoutError) as standard_exc:
                        reply = _format_control_error_reply("修改每日标准", standard_exc)
                        parse_status = "standard_update_failed"
                else:
                    reply = _format_parse_result_reply(parse_result)
                    parse_status = "parsed"
        except (RuntimeError, urllib.error.URLError, TimeoutError) as exc:
            reply = _format_parse_error_reply(exc)
            parse_status = "parse_failed"
            parse_error = _diagnostic_exception(exc, "legacy_parser")

        processing_extra: dict[str, Any] = {}
        if agent_fallback_metrics:
            processing_extra["agent_metrics"] = agent_fallback_metrics
        if parse_error:
            processing_extra["parse_error"] = parse_error
        _finish_text_message_processing(
            message,
            started_at,
            parse_status,
            reply,
            parse_result,
            lookup_result,
            intake_write_result,
            today_summary,
            daily_standard,
            undo_result,
            standard_write_result,
            pending_task,
            pending_result,
            extra=processing_extra or None,
        )
    except Exception as exc:
        _mark_message_done(message, "failed")
        print(
            json.dumps(
                {
                    "status": "message_processing_failed",
                    "message_id": message.get("message_id", ""),
                    "chat_id": message.get("chat_id", ""),
                    "error": str(exc),
                    "metrics": {
                        "event_status": "message_processing_failed",
                        "reply_latency_ms": int((time.monotonic() - started_at) * 1000),
                        "bitable_error": False,
                        "deepseek_error": False,
                    },
                },
                ensure_ascii=False,
            ),
            flush=True,
        )


def _process_message_synchronously(message: dict[str, str]) -> bool:
    if not _try_mark_message_processing(message):
        return False

    _process_text_message(message)
    return True


def _log_post_ack_processing_event(
    status: str,
    message: dict[str, str],
    started_at: float,
    extra: dict[str, Any] | None = None,
) -> None:
    payload: dict[str, Any] = {
        "status": status,
        "message_id": message.get("message_id", ""),
        "chat_id": message.get("chat_id", ""),
        "metrics": {
            "event_status": status,
            "post_ack_latency_ms": int((time.monotonic() - started_at) * 1000),
        },
    }
    if extra:
        payload.update(extra)
    print(json.dumps(payload, ensure_ascii=False), flush=True)


def _process_message_after_ack(message: dict[str, str]) -> bool:
    started_at = time.monotonic()
    _log_post_ack_processing_event("post_ack_processing_started", message, started_at)
    try:
        processed = _process_message_synchronously(message)
    except Exception as exc:
        _log_post_ack_processing_event(
            "post_ack_processing_failed",
            message,
            started_at,
            {"error": str(exc)},
        )
        return False

    _log_post_ack_processing_event(
        "post_ack_processing_completed" if processed else "post_ack_processing_skipped_duplicate",
        message,
        started_at,
        {"processed": processed},
    )
    return processed


def _schedule_message_after_ack(message: dict[str, str]) -> None:
    started_at = time.monotonic()

    thread = threading.Thread(
        target=_process_message_after_ack,
        args=(message,),
        name=f"post-ack-{message.get('message_id', '')[:16]}",
        daemon=True,
    )
    thread.start()
    _log_post_ack_processing_event(
        "post_ack_processing_scheduled",
        message,
        started_at,
        {"thread_name": thread.name},
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "CalorieAgentHTTP/1.0"

    def do_GET(self) -> None:
        if self.path in {"/", "/health"}:
            self._send_json(
                200,
                {
                    "status": "ok",
                    "service": SERVICE_NAME,
                    "env": _settings()["app_env"],
                },
            )
            return
        self._send_json(404, {"error": "not_found"})

    def do_POST(self) -> None:
        request_path = self.path.split("?", 1)[0]
        if request_path == "/tasks/reminders":
            if not _verify_rollover_task_token(self.path, self.headers):
                self._send_json(
                    403,
                    {
                        "error": "invalid_rollover_task_token",
                        "hint": "Set ROLLOVER_TASK_TOKEN and pass it as X-Task-Token or ?token=...",
                    },
                )
                return
            try:
                from calorie_agent.domain.reminders import evaluate_reminders

                self._send_json(200, evaluate_reminders(legacy=sys.modules[__name__], send=True))
            except Exception as exc:
                self._send_json(500, {"error": "reminder_task_failed", "detail": str(exc)})
            return

        if request_path == "/tasks/daily-cleanup":
            if not _daily_clear_enabled(_settings()):
                self._send_json(403, {"error": "daily_cleanup_disabled"})
                return
            if not _verify_rollover_task_token(self.path, self.headers):
                self._send_json(
                    403,
                    {
                        "error": "invalid_rollover_task_token",
                        "hint": "Set ROLLOVER_TASK_TOKEN and pass it as X-Task-Token or ?token=...",
                    },
                )
                return
            try:
                from calorie_agent.domain.rollover import clear_daily_intake

                self._send_json(200, clear_daily_intake(legacy=sys.modules[__name__]))
            except Exception as exc:
                self._send_json(500, {"error": "daily_cleanup_failed", "detail": str(exc)})
            return

        if request_path != "/webhook/feishu/events":
            self._send_json(404, {"error": "not_found"})
            return

        content_length = int(self.headers.get("Content-Length") or 0)
        payload = _parse_json(self.rfile.read(content_length))
        if payload is None:
            self._send_json(400, {"error": "invalid_json"})
            return

        if "encrypt" in payload:
            self._send_json(
                400,
                {
                    "error": "encrypted_events_not_supported",
                    "hint": "Disable Feishu event encryption for block 2 testing.",
                },
            )
            return

        if not _verify_feishu_token(payload):
            self._send_json(403, {"error": "invalid_feishu_token"})
            return

        challenge = _extract_challenge(payload)
        if challenge:
            self._send_json(200, {"challenge": challenge})
            return

        message = _parse_text_message(payload)
        if not message:
            self._send_json(200, {"status": "ignored", "reason": "unsupported_or_non_text_event"})
            return

        self._send_json(
            200,
            {
                "status": "accepted",
                "message_id": message["message_id"],
                "chat_id": message["chat_id"],
            },
        )
        _schedule_message_after_ack(message)

    def log_message(self, format: str, *args: Any) -> None:
        print("%s - %s" % (self.address_string(), format % args), flush=True)

    def _send_json(self, status_code: int, payload: dict[str, Any]) -> None:
        body = _json_bytes(payload)
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        self.wfile.flush()


def main() -> None:
    port = int(os.getenv("PORT") or os.getenv("SCF_CUSTOM_CONTAINER_PORT") or "9000")
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"{SERVICE_NAME} listening on 0.0.0.0:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
