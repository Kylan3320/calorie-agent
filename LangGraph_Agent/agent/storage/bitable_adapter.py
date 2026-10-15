"""Bitable-backed store: the only real persistence layer (ported from V2 legacy_app).

Idempotency formulas and field defaults come from contracts/bitable_schema.md.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import unicodedata
import urllib.parse
import uuid
from typing import Any

from . import feishu_client, timeutil
from .food_match import normalize_food_name
from .timeutil import extract_number, extract_text, round_number

# --- field name defaults (env-overridable, V2 compatible) ---------------------

FOOD_FIELD_FALLBACKS = {
    "food_id": ["food_id", "食物ID", "食物 ID"],
    "food_name": ["food_name", "food", "name", "食物名称", "食物名", "食物"],
    "alias": ["alias", "aliases", "别名", "食物别名"],
    "carbs": ["carbs_per_100g", "carbs", "碳水/100g", "碳水 /100g", "碳水每100g", "碳水"],
    "protein": ["protein_per_100g", "protein", "蛋白质/100g", "蛋白质 /100g", "蛋白/100g", "蛋白质", "蛋白"],
    "fat": ["fat_per_100g", "fat", "脂肪/100g", "脂肪 /100g", "脂肪每100g", "脂肪"],
    "status": ["status", "状态"],
}

INTAKE_FIELD_DEFAULTS = {
    "date": ("BITABLE_FIELD_INTAKE_DATE", "date"),
    "user_id": ("BITABLE_FIELD_INTAKE_USER_ID", "user_id"),
    "message_id": ("BITABLE_FIELD_INTAKE_MESSAGE_ID", "message_id"),
    "raw_text": ("BITABLE_FIELD_INTAKE_RAW_TEXT", "raw_text"),
    "meal_type": ("BITABLE_FIELD_INTAKE_MEAL_TYPE", "meal_type"),
    "food_id": ("BITABLE_FIELD_INTAKE_FOOD_ID", "food_id"),
    "food_name": ("BITABLE_FIELD_INTAKE_FOOD_NAME", "food_name_snapshot"),
    "grams": ("BITABLE_FIELD_INTAKE_GRAMS", "grams"),
    "carbs": ("BITABLE_FIELD_INTAKE_CARBS", "carbs"),
    "protein": ("BITABLE_FIELD_INTAKE_PROTEIN", "protein"),
    "fat": ("BITABLE_FIELD_INTAKE_FAT", "fat"),
    "kcal": ("BITABLE_FIELD_INTAKE_KCAL", "kcal"),
    "status": ("BITABLE_FIELD_INTAKE_STATUS", "status"),
    "created_at": ("BITABLE_FIELD_INTAKE_CREATED_AT", "created_at"),
}

STANDARD_FIELDS = {
    "user_id": ("BITABLE_FIELD_STANDARD_USER_ID", "user_id"),
    "type": ("BITABLE_FIELD_STANDARD_TYPE", "standard_type"),
    "date": ("BITABLE_FIELD_STANDARD_DATE", "date"),
    "kcal": ("BITABLE_FIELD_STANDARD_KCAL", "kstandard"),
    "carbs": ("BITABLE_FIELD_STANDARD_CARBS", "cstandard"),
    "protein": ("BITABLE_FIELD_STANDARD_PROTEIN", "pstandard"),
    "fat": ("BITABLE_FIELD_STANDARD_FAT", "fstandard"),
}

PENDING_FIELDS = {
    "id": ("BITABLE_FIELD_PENDING_ID", "pending_id"),
    "user_id": ("BITABLE_FIELD_PENDING_USER_ID", "user_id"),
    "chat_id": ("BITABLE_FIELD_PENDING_CHAT_ID", "chat_id"),
    "source_message_id": ("BITABLE_FIELD_PENDING_SOURCE_MESSAGE_ID", "source_message_id"),
    "intent": ("BITABLE_FIELD_PENDING_INTENT", "intent"),
    "food_name": ("BITABLE_FIELD_PENDING_FOOD_NAME", "food_name"),
    "grams": ("BITABLE_FIELD_PENDING_GRAMS", "grams"),
    "meal_type": ("BITABLE_FIELD_PENDING_MEAL_TYPE", "meal_type"),
    "raw_text": ("BITABLE_FIELD_PENDING_RAW_TEXT", "raw_text"),
    "status": ("BITABLE_FIELD_PENDING_STATUS", "status"),
    "created_at": ("BITABLE_FIELD_PENDING_CREATED_AT", "created_at"),
    "expires_at": ("BITABLE_FIELD_PENDING_EXPIRES_AT", "expires_at"),
}

USAGE_FIELDS = {
    "event_id": ("BITABLE_FIELD_USAGE_EVENT_ID", "event_id"),
    "event_type": ("BITABLE_FIELD_USAGE_EVENT_TYPE", "event_type"),
    "message_id": ("BITABLE_FIELD_USAGE_MESSAGE_ID", "message_id"),
    "user_id": ("BITABLE_FIELD_USAGE_USER_ID", "user_id"),
    "preset": ("BITABLE_FIELD_USAGE_PRESET", "preset"),
    "planner": ("BITABLE_FIELD_USAGE_PLANNER", "planner"),
    "status": ("BITABLE_FIELD_USAGE_STATUS", "status"),
    "error_type": ("BITABLE_FIELD_USAGE_ERROR_TYPE", "error_type"),
    "action_count": ("BITABLE_FIELD_USAGE_ACTION_COUNT", "action_count"),
    "reply_sent": ("BITABLE_FIELD_USAGE_REPLY_SENT", "reply_sent"),
    "latency_ms": ("BITABLE_FIELD_USAGE_LATENCY_MS", "latency_ms"),
    "created_at": ("BITABLE_FIELD_USAGE_CREATED_AT", "created_at"),
}

DEFAULT_DAILY_STANDARD = {"carbs": 250.0, "protein": 150.0, "fat": 70.0, "kcal": 2230.0}

VALID_FOOD_STATUS = {"active", "启用", "正常", "可用"}
VALID_INTAKE_STATUS = {"valid", "active", "normal", "启用", "正常", "有效", "已记录"}

_FOODS_CACHE: tuple[float, list[dict[str, Any]]] | None = None
_FOODS_CACHE_LOCK = threading.Lock()
_FOODS_CACHE_TTL_SECONDS = 300.0
_KNOWLEDGE_CACHE: tuple[float, list[dict[str, Any]]] | None = None
_KNOWLEDGE_CACHE_LOCK = threading.Lock()
_KNOWLEDGE_CACHE_TTL_SECONDS = 300.0
_ALIASES_CACHE: tuple[float, dict[str, str]] | None = None
_ALIASES_CACHE_LOCK = threading.Lock()
_ALIASES_CACHE_TTL_SECONDS = 300.0


# --- idempotency helpers (formulas in contracts/bitable_schema.md) ------------

def message_key(message: dict[str, str]) -> str:
    key = str(message.get("message_id") or "").strip()
    if key:
        return key
    return f"{message.get('chat_id', '')}:{message.get('text', '')}"


def stable_uuid(value: str) -> str:
    token_bytes = bytearray(hashlib.sha256(value.encode("utf-8")).digest()[:16])
    token_bytes[6] = (token_bytes[6] & 0x0F) | 0x40
    token_bytes[8] = (token_bytes[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(token_bytes)))


def intake_client_token(message: dict[str, str]) -> str:
    return stable_uuid(f"calorie-agent:{message_key(message)}:intake")


def reply_uuid(message: dict[str, str]) -> str:
    return stable_uuid(f"calorie-agent:{message_key(message)}:reply")


def normalize_food_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").strip().lower()
    return re.sub(r"[\s,，、;；/|]+", "", normalized)


# --- helpers ------------------------------------------------------------------

def _env_field(table_key: str, key: str, spec: dict[str, tuple[str, str]]) -> str:
    env_key, default = spec[key]
    return os.getenv(env_key, default)


def _intake_field(key: str) -> str:
    return _env_field("intake", key, INTAKE_FIELD_DEFAULTS)


def _std_field(key: str) -> str:
    return _env_field("standard", key, STANDARD_FIELDS)


def _pending_field(key: str) -> str:
    return _env_field("pending", key, PENDING_FIELDS)


def _usage_field(key: str) -> str:
    return _env_field("usage", key, USAGE_FIELDS)


def _food_candidate_names(key: str) -> list[str]:
    configured = os.getenv(f"BITABLE_FIELD_{key.upper()}", "")
    names = [configured, *FOOD_FIELD_FALLBACKS[key]]
    unique: list[str] = []
    for name in names:
        if name and name not in unique:
            unique.append(name)
    return unique


def _cell(fields: dict[str, Any], names: list[str]) -> Any:
    for name in names:
        if name in fields:
            return fields[name]
    return None


def _unique_non_empty(values: list[str]) -> list[str]:
    unique: list[str] = []
    for value in values:
        if value and value not in unique:
            unique.append(value)
    return unique


def _split_aliases(value: str) -> list[str]:
    return [item.strip() for item in re.split(r"[,，、;；/|\n]+", value or "") if item.strip()]


def parse_alias_env() -> dict[str, str]:
    """env USER_ID_ALIASES 兜底映射，格式 "别名:主账号,别名2:主账号2"（contracts/tool_protocol.md）。"""
    raw = os.getenv("USER_ID_ALIASES", "")
    out: dict[str, str] = {}
    for pair in raw.split(","):
        alias, _, canonical = pair.strip().partition(":")
        alias, canonical = alias.strip(), canonical.strip()
        if alias and canonical:
            out[alias] = canonical
    return out


def _number_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _is_valid_intake_status(value: Any) -> bool:
    status = extract_text(value).lower()
    return not status or status in VALID_INTAKE_STATUS


def _record_url(app_token: str, table: str) -> str:
    return (
        f"{feishu_client.feishu_api_base()}/bitable/v1/apps/{urllib.parse.quote(app_token, safe='')}/"
        f"tables/{urllib.parse.quote(table, safe='')}/records"
    )


def _list_records(app_token: str, table: str, field_names: list[str] | None = None) -> list[tuple[str, dict[str, Any]]]:
    """List all records as (record_id, fields) tuples, paging through 500 at a time."""
    token = feishu_client.get_tenant_access_token()
    rows: list[tuple[str, dict[str, Any]]] = []
    page_token = ""
    while True:
        query: dict[str, str] = {"page_size": "500"}
        if page_token:
            query["page_token"] = page_token
        if field_names:
            query["field_names"] = json.dumps(field_names, ensure_ascii=False)
        data = feishu_client.http_json_get(
            f"{_record_url(app_token, table)}?{urllib.parse.urlencode(query)}",
            {"Authorization": f"Bearer {token}"},
        )
        if data.get("code") != 0:
            raise RuntimeError(f"Failed to list Bitable records: {data}")
        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        items = payload.get("items")
        for record in items if isinstance(items, list) else []:
            if isinstance(record, dict) and isinstance(record.get("fields"), dict):
                rows.append((str(record.get("record_id") or ""), record["fields"]))
        if not payload.get("has_more"):
            return rows
        page_token = str(payload.get("page_token") or "")
        if not page_token:
            return rows


# --- knowledge_base (contracts/knowledge_schema.md) ---------------------------

_KNOWLEDGE_STATUS_DEFAULTS = {"", "active", "启用", "正常", "有效"}


def _knowledge_entry_from_bitable(record: dict[str, Any]) -> dict[str, Any] | None:
    fields = record.get("fields")
    if not isinstance(fields, dict):
        return None
    question = extract_text(fields.get("question"))
    answer = extract_text(fields.get("answer"))
    if not question or not answer:
        return None
    status = extract_text(fields.get("status")).lower()
    if status not in _KNOWLEDGE_STATUS_DEFAULTS:
        return None
    return {
        "entry_id": extract_text(fields.get("entry_id")),
        "question": question,
        "answer": answer,
        "aliases": _split_aliases(extract_text(fields.get("aliases"))),
        "tags": extract_text(fields.get("tags")),
        "source": extract_text(fields.get("source")),
    }


class BitableStore:
    """Real store. All Bitable/Feishu HTTP goes through feishu_client."""

    def __init__(self, offset_hours: float | None = None) -> None:
        if offset_hours is None:
            try:
                offset_hours = float(os.getenv("APP_TIMEZONE_OFFSET_HOURS", "") or timeutil.DEFAULT_TIMEZONE_OFFSET_HOURS)
            except ValueError:
                offset_hours = timeutil.DEFAULT_TIMEZONE_OFFSET_HOURS
        self.offset_hours = offset_hours
        self.app_token = os.getenv("BITABLE_APP_TOKEN", "")
        self.food_table = os.getenv("BITABLE_FOOD_TABLE_ID", "")
        self.intake_table = os.getenv("BITABLE_INTAKE_TABLE_ID", "")
        self.standard_table = os.getenv("BITABLE_STANDARD_TABLE_ID", "")
        self.pending_table = os.getenv("BITABLE_PENDING_TABLE_ID", "")
        self.usage_table = os.getenv("BITABLE_USAGE_TABLE_ID", "")
        self.knowledge_table = os.getenv("BITABLE_KNOWLEDGE_TABLE_ID", "")
        self.alias_table = os.getenv("BITABLE_ALIASES_TABLE_ID", "")

    def kind(self) -> str:
        return "bitable"

    def _require(self, table_id: str, env_name: str, table_label: str) -> str:
        if not self.app_token or not table_id:
            raise RuntimeError(f"BITABLE_APP_TOKEN and {env_name} are not configured ({table_label}).")
        return table_id

    def today_string(self) -> str:
        """Store-canonical "today" (contracts/tool_protocol.md); query_history anchors here."""
        return self._today_string()

    def load_user_aliases(self) -> dict[str, str]:
        """账号别名映射（方案 B）：env 兜底 + user_aliases 表（表行优先，TTL 缓存）。"""
        global _ALIASES_CACHE
        now = timeutil.now_millis() / 1000
        with _ALIASES_CACHE_LOCK:
            if _ALIASES_CACHE and now - _ALIASES_CACHE[0] < _ALIASES_CACHE_TTL_SECONDS:
                return dict(_ALIASES_CACHE[1])
        merged = parse_alias_env()
        if self.app_token and self.alias_table:
            for _record_id, fields in _list_records(self.app_token, self.alias_table):
                alias = extract_text(fields.get("alias_open_id")).strip()
                canonical = extract_text(fields.get("canonical_user_id")).strip()
                status = extract_text(fields.get("status")).strip().lower()
                if alias and canonical and status in _KNOWLEDGE_STATUS_DEFAULTS:
                    merged[alias] = canonical
        with _ALIASES_CACHE_LOCK:
            _ALIASES_CACHE = (now, dict(merged))
        return dict(merged)

    def _today_string(self) -> str:
        return timeutil.today_string(self.offset_hours)

    def _today_start_millis(self) -> int:
        return timeutil.today_start_millis(self.offset_hours)

    def _batch_create(self, table: str, records: list[dict[str, Any]], client_token: str = "") -> dict[str, Any]:
        if not records:
            return {"code": 0, "data": {"records": []}}
        query = urllib.parse.urlencode({"client_token": client_token}) if client_token else ""
        url = f"{_record_url(self.app_token, table)}/batch_create{'?' + query if query else ''}"
        data = feishu_client.http_json_post(
            url,
            {"records": records},
            {"Authorization": f"Bearer {feishu_client.get_tenant_access_token()}"},
        )
        if data.get("code") != 0:
            raise RuntimeError(f"Failed to batch create Bitable records: {data}")
        return data

    def _update_record(self, table: str, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        if not record_id:
            raise RuntimeError("record_id is required for Bitable update.")
        if not fields:
            return {"code": 0}
        url = f"{_record_url(self.app_token, table)}/{urllib.parse.quote(record_id, safe='')}"
        data = feishu_client.http_json_put(
            url,
            {"fields": fields},
            {"Authorization": f"Bearer {feishu_client.get_tenant_access_token()}"},
        )
        if data.get("code") != 0:
            raise RuntimeError(f"Failed to update Bitable record: {data}")
        return data

    # --- foods ------------------------------------------------------------

    def _food_record_from_bitable(self, record: dict[str, Any]) -> dict[str, Any] | None:
        fields = record.get("fields")
        if not isinstance(fields, dict):
            return None
        name = extract_text(_cell(fields, _food_candidate_names("food_name")))
        if not name:
            return None
        status = extract_text(_cell(fields, _food_candidate_names("status"))).lower()
        if status and status not in VALID_FOOD_STATUS:
            return None
        carbs = extract_number(_cell(fields, _food_candidate_names("carbs")))
        protein = extract_number(_cell(fields, _food_candidate_names("protein")))
        fat = extract_number(_cell(fields, _food_candidate_names("fat")))
        if carbs is None or protein is None or fat is None:
            return None
        return {
            "record_id": str(record.get("record_id") or ""),
            "food_id": extract_text(_cell(fields, _food_candidate_names("food_id"))),
            "name": name,
            "aliases": _split_aliases(extract_text(_cell(fields, _food_candidate_names("alias")))),
            "carbs_per_100g": carbs,
            "protein_per_100g": protein,
            "fat_per_100g": fat,
        }

    def load_foods(self) -> list[dict[str, Any]]:
        global _FOODS_CACHE
        table = self._require(self.food_table, "BITABLE_FOOD_TABLE_ID", "foods")
        now = timeutil.now_millis() / 1000
        with _FOODS_CACHE_LOCK:
            if _FOODS_CACHE and now - _FOODS_CACHE[0] < _FOODS_CACHE_TTL_SECONDS:
                return list(_FOODS_CACHE[1])
        foods: list[dict[str, Any]] = []
        for _record_id, fields in _list_records(self.app_token, table):
            food = self._food_record_from_bitable({"fields": fields})
            if food:
                foods.append(food)
        with _FOODS_CACHE_LOCK:
            _FOODS_CACHE = (now, list(foods))
        return list(foods)

    def load_knowledge(self) -> list[dict[str, Any]]:
        """knowledge_base entries (contracts/knowledge_schema.md); graceful [] when unset."""
        global _KNOWLEDGE_CACHE
        if not self.app_token or not self.knowledge_table:
            return []
        now = timeutil.now_millis() / 1000
        with _KNOWLEDGE_CACHE_LOCK:
            if _KNOWLEDGE_CACHE and now - _KNOWLEDGE_CACHE[0] < _KNOWLEDGE_CACHE_TTL_SECONDS:
                return list(_KNOWLEDGE_CACHE[1])
        entries: list[dict[str, Any]] = []
        for _record_id, fields in _list_records(self.app_token, self.knowledge_table):
            entry = _knowledge_entry_from_bitable({"fields": fields})
            if entry:
                entries.append(entry)
        with _KNOWLEDGE_CACHE_LOCK:
            _KNOWLEDGE_CACHE = (now, list(entries))
        return list(entries)

    def write_food(self, food: dict[str, Any]) -> dict[str, Any]:
        """Add one food-library row (Boohee M1 confirm-import; tool_protocol.md).

        Idempotent by the contract "create-food" formulas; invalidates the
        process foods cache so the new row is immediately matchable.
        """
        table = self._require(self.food_table, "BITABLE_FOOD_TABLE_ID", "foods")
        name = str(food.get("name") or "").strip()
        carbs = _number_or_none(food.get("carbs_per_100g"))
        protein = _number_or_none(food.get("protein_per_100g"))
        fat = _number_or_none(food.get("fat_per_100g"))
        if not name or carbs is None or protein is None or fat is None:
            return {"status": "invalid", "food_id": "", "name": name}
        normalized = normalize_food_key(name)
        food_id = stable_uuid(f"calorie-agent:food:{normalized}")
        existing = {str(item.get("food_id") or "") for item in self.load_foods()}
        if food_id in existing:
            return {"status": "duplicate", "food_id": food_id, "name": name}
        client_token = stable_uuid(f"calorie-agent:food-create:{normalized}")
        fields = {
            _food_candidate_names("food_id")[0]: food_id,
            _food_candidate_names("food_name")[0]: name,
            _food_candidate_names("carbs")[0]: carbs,
            _food_candidate_names("protein")[0]: protein,
            _food_candidate_names("fat")[0]: fat,
            _food_candidate_names("status")[0]: "active",
            "source": str(food.get("source") or ""),
            "boohee_id": str(food.get("boohee_id") or ""),
        }
        aliases = food.get("aliases")
        if aliases:
            fields[_food_candidate_names("alias")[0]] = "、".join(aliases) if isinstance(aliases, list) else str(aliases)
        result = self._batch_create(table, [{"fields": fields}], client_token=client_token)
        if result.get("code") != 0:
            raise RuntimeError(f"write_food batch_create failed: {str(result)[:200]}")
        global _FOODS_CACHE
        with _FOODS_CACHE_LOCK:
            _FOODS_CACHE = None
        return {"status": "created", "food_id": food_id, "name": name}

    # --- intake -----------------------------------------------------------

    def _intake_summary_item(self, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return {
            "record_id": record_id,
            "message_id": extract_text(fields.get(_intake_field("message_id"))),
            "meal_type": extract_text(fields.get(_intake_field("meal_type"))),
            "food_name": extract_text(fields.get(_intake_field("food_name"))),
            "grams": extract_number(fields.get(_intake_field("grams"))) or 0.0,
            "created_at": extract_number(fields.get(_intake_field("created_at"))) or 0.0,
            "nutrition": {
                "carbs": extract_number(fields.get(_intake_field("carbs"))) or 0.0,
                "protein": extract_number(fields.get(_intake_field("protein"))) or 0.0,
                "fat": extract_number(fields.get(_intake_field("fat"))) or 0.0,
                "kcal": extract_number(fields.get(_intake_field("kcal"))) or 0.0,
            },
        }

    def _iter_intake_rows(self) -> list[tuple[str, dict[str, Any]]]:
        table = self._require(self.intake_table, "BITABLE_INTAKE_TABLE_ID", "intake")
        return _list_records(self.app_token, table, _unique_non_empty([_intake_field(key) for key in INTAKE_FIELD_DEFAULTS]))

    def _is_today_date_value(self, value: Any) -> bool:
        if os.getenv("BITABLE_FIELD_INTAKE_DATE_VALUE_TYPE", "timestamp") == "text":
            return extract_text(value) == self._today_string()
        number = extract_number(value)
        if number is None:
            return False
        start = self._today_start_millis()
        return start <= int(number) < start + 86400000

    def today_summary(self, user_id: str) -> dict[str, Any]:
        user_id = str(user_id or "").strip()
        items: list[dict[str, Any]] = []
        totals = {"carbs": 0.0, "protein": 0.0, "fat": 0.0, "kcal": 0.0}
        for record_id, fields in self._iter_intake_rows():
            row_user_id = extract_text(fields.get(_intake_field("user_id")))
            if user_id and row_user_id and row_user_id != user_id:
                continue
            if not self._is_today_date_value(fields.get(_intake_field("date"))):
                continue
            if not _is_valid_intake_status(fields.get(_intake_field("status"))):
                continue
            item = self._intake_summary_item(record_id, fields)
            for key in totals:
                totals[key] = round_number(totals[key] + float(item["nutrition"][key]))
            items.append(item)
        items.sort(key=lambda item: (float(item.get("created_at") or 0), str(item.get("record_id") or "")))
        return {
            "date": self._today_string(),
            "count": len(items),
            "items": items,
            "totals": totals,
            "source": "bitable",
        }

    def summarize_history(self, user_id: str, start_date: str, end_date: str, food_query: str = "") -> dict[str, Any]:
        """Aggregate intake over [start_date, end_date); RAG M2 query_history (tool_protocol.md)."""
        table = self._require(self.intake_table, "BITABLE_INTAKE_TABLE_ID", "intake")
        _ = table
        start_millis = self._date_to_start_millis(start_date)
        end_millis = self._date_to_start_millis(end_date)
        normalized_food = normalize_food_name(food_query)
        days: dict[str, dict[str, Any]] = {}
        total = {"count": 0, "grams": 0.0, "kcal": 0.0, "carbs": 0.0, "protein": 0.0, "fat": 0.0}
        for record_id, fields in self._iter_intake_rows():
            row_user_id = extract_text(fields.get(_intake_field("user_id")))
            if user_id and row_user_id and row_user_id != user_id:
                continue
            row_date, in_range = self._date_value_in_range(fields.get(_intake_field("date")), start_millis, end_millis, start_date, end_date)
            if not in_range:
                continue
            if not _is_valid_intake_status(fields.get(_intake_field("status"))):
                continue
            food_name = extract_text(fields.get(_intake_field("food_name")))
            if normalized_food:
                name = normalize_food_name(food_name)
                if not name or (name != normalized_food and name not in normalized_food and normalized_food not in name):
                    continue
            item = self._intake_summary_item(record_id, fields)
            day = days.setdefault(item.get("date") or row_date, {"date": row_date, "count": 0, "grams": 0.0, "kcal": 0.0, "carbs": 0.0, "protein": 0.0, "fat": 0.0})
            day["count"] += 1
            total["count"] += 1
            day["grams"] = round_number(day["grams"] + float(item.get("grams") or 0))
            total["grams"] = round_number(total["grams"] + float(item.get("grams") or 0))
            for key in ("kcal", "carbs", "protein", "fat"):
                day[key] = round_number(day[key] + float(item["nutrition"][key]))
                total[key] = round_number(total[key] + float(item["nutrition"][key]))
        day_list = [days[key] for key in sorted(days)]
        over_budget_days: list[str] = []
        if not normalized_food:
            standard = self.get_standard(user_id)
            budget = float(standard.get("kcal") or 0)
            if budget > 0:
                over_budget_days = [day["date"] for day in day_list if float(day["kcal"]) > budget]
        return {
            "start_date": start_date,
            "end_date": end_date,
            "food_query": str(food_query or ""),
            "total": total,
            "days": day_list,
            "over_budget_days": over_budget_days,
            "source": "bitable",
        }

    def _date_to_start_millis(self, date_str: str) -> int:
        import calendar as _calendar
        import datetime as _dt
        parsed = _dt.datetime.strptime(str(date_str), "%Y-%m-%d")
        epoch = _calendar.timegm(parsed.timetuple())
        return int((epoch - self.offset_hours * 3600) * 1000)

    def _date_value_in_range(self, value: Any, start_millis: int, end_millis: int, start_date: str, end_date: str) -> tuple[str, bool]:
        if os.getenv("BITABLE_FIELD_INTAKE_DATE_VALUE_TYPE", "timestamp") == "text":
            text = extract_text(value)
            return text, start_date <= text < end_date
        number = extract_number(value)
        if number is None:
            return "", False
        number = int(number)
        import datetime as _dt
        shifted = number / 1000 + self.offset_hours * 3600
        row_date = _dt.datetime.fromtimestamp(shifted, _dt.timezone.utc).strftime("%Y-%m-%d")
        return row_date, start_millis <= number < end_millis

    def intake_exists(self, message_id: str) -> bool:
        if not message_id:
            return False
        table = self._require(self.intake_table, "BITABLE_INTAKE_TABLE_ID", "intake")
        message_field = _intake_field("message_id")
        for _record_id, fields in _list_records(self.app_token, table, [message_field]):
            if extract_text(fields.get(message_field)) == message_id:
                return True
        return False

    def write_intake(
        self,
        message: dict[str, str],
        items: list[dict[str, Any]],
        meal_type: str = "unknown",
    ) -> dict[str, Any]:
        table = self._require(self.intake_table, "BITABLE_INTAKE_TABLE_ID", "intake")
        if self.intake_exists(str(message.get("message_id") or "")):
            return {"status": "duplicate_skipped", "created_count": 0, "records": []}
        if not items:
            return {"status": "nothing_to_write", "created_count": 0, "records": []}

        date_value: int | str = self._today_start_millis()
        if os.getenv("BITABLE_FIELD_INTAKE_DATE_VALUE_TYPE", "timestamp") == "text":
            date_value = self._today_string()
        records = []
        for item in items:
            nutrition = item["nutrition"]
            records.append(
                {
                    "fields": {
                        _intake_field("date"): date_value,
                        _intake_field("user_id"): message.get("user_id", ""),
                        _intake_field("message_id"): message.get("message_id", ""),
                        _intake_field("raw_text"): message.get("text", ""),
                        _intake_field("meal_type"): meal_type or "unknown",
                        _intake_field("food_id"): item.get("food_id", ""),
                        _intake_field("food_name"): item.get("matched_name", ""),
                        _intake_field("grams"): item.get("grams", 0),
                        _intake_field("carbs"): nutrition["carbs"],
                        _intake_field("protein"): nutrition["protein"],
                        _intake_field("fat"): nutrition["fat"],
                        _intake_field("kcal"): nutrition["kcal"],
                        _intake_field("status"): "valid",
                        _intake_field("created_at"): timeutil.now_millis(),
                    }
                }
            )
        data = self._batch_create(table, records, client_token=intake_client_token(message))
        body = data.get("data") if isinstance(data.get("data"), dict) else {}
        created = body.get("records") if isinstance(body.get("records"), list) else []
        return {"status": "created", "created_count": len(created) or len(records), "records": created}

    def undo_last(self, user_id: str) -> dict[str, Any]:
        user_id = str(user_id or "").strip()
        best_sort: tuple[float, int] | None = None
        best_item: dict[str, Any] | None = None
        for index, (record_id, fields) in enumerate(self._iter_intake_rows()):
            row_user_id = extract_text(fields.get(_intake_field("user_id")))
            if user_id and row_user_id and row_user_id != user_id:
                continue
            if not _is_valid_intake_status(fields.get(_intake_field("status"))):
                continue
            item = self._intake_summary_item(record_id, fields)
            sort_value = float(item.get("created_at") or 0) or float(extract_number(fields.get(_intake_field("date"))) or 0)
            sort_key = (sort_value, index)
            if best_sort is None or sort_key > best_sort:
                best_sort = sort_key
                best_item = item
        if not best_item:
            return {"status": "nothing_to_undo", "item": None}
        self._update_record(self.intake_table, best_item["record_id"], {_intake_field("status"): "deleted"})
        return {"status": "deleted", "item": best_item}

    # --- standard ---------------------------------------------------------

    def get_standard(self, user_id: str) -> dict[str, Any]:
        if not self.standard_table:
            return {**DEFAULT_DAILY_STANDARD, "source": "default"}
        if not self.app_token:
            raise RuntimeError("BITABLE_APP_TOKEN is not configured.")

        user_id = str(user_id or "").strip()
        names = _unique_non_empty([_std_field(key) for key in STANDARD_FIELDS])
        start = self._today_start_millis()
        today_text = self._today_string()
        best_score = -1
        best_fields: dict[str, Any] | None = None
        for _record_id, fields in _list_records(self.app_token, self.standard_table, names):
            row_user_id = extract_text(fields.get(_std_field("user_id")))
            if user_id and row_user_id and row_user_id != user_id:
                continue
            date_value = fields.get(_std_field("date"))
            date_text = extract_text(date_value)
            if date_text:
                number = extract_number(date_value)
                if number is not None:
                    if not (start <= int(number) < start + 86400000):
                        continue
                elif date_text not in {today_text, "today", "今日"}:
                    continue

            standard_type = extract_text(fields.get(_std_field("type"))).lower()
            score = 0
            if user_id and row_user_id == user_id:
                score += 100
            elif not row_user_id:
                score += 10
            if date_value not in (None, ""):
                score += 20
            if standard_type in {"daily", "default", "默认", "每日", "每日标准"}:
                score += 5
            if score > best_score:
                best_score = score
                best_fields = fields

        if best_fields is None:
            return {**DEFAULT_DAILY_STANDARD, "source": "default"}
        standard = {**DEFAULT_DAILY_STANDARD, "source": "bitable"}
        for key in ("carbs", "protein", "fat"):
            value = extract_number(best_fields.get(_std_field(key)))
            if value is not None:
                standard[key] = value
        kcal = extract_number(best_fields.get(_std_field("kcal")))
        if kcal is None:
            kcal = round_number(standard["carbs"] * 4 + standard["protein"] * 4 + standard["fat"] * 9)
        standard["kcal"] = kcal
        return standard

    # --- pending ----------------------------------------------------------

    def create_pending(
        self,
        message: dict[str, str],
        intent: str,
        food_name: str,
        grams: float | None,
        meal_type: str = "unknown",
    ) -> dict[str, Any]:
        if not self.app_token or not self.pending_table:
            return {"status": "not_configured", "intent": intent, "food_name": food_name, "grams": grams}
        msg_key = message_key(message)
        pending_id = stable_uuid(f"calorie-agent:pending:{msg_key}:{intent}:{food_name}")
        now = timeutil.now_millis()
        try:
            ttl_hours = max(float(os.getenv("PENDING_TASK_TTL_HOURS", "24")), 1)
        except ValueError:
            ttl_hours = 24.0
        fields = {
            _pending_field("id"): pending_id,
            _pending_field("user_id"): message.get("user_id", ""),
            _pending_field("chat_id"): message.get("chat_id", ""),
            _pending_field("source_message_id"): message.get("message_id", ""),
            _pending_field("intent"): intent,
            _pending_field("food_name"): food_name,
            _pending_field("meal_type"): meal_type or "unknown",
            _pending_field("raw_text"): message.get("text", ""),
            _pending_field("status"): "pending",
            _pending_field("created_at"): now,
            _pending_field("expires_at"): now + int(ttl_hours * 3600 * 1000),
        }
        if grams is not None:
            fields[_pending_field("grams")] = grams
        client_token = stable_uuid(f"calorie-agent:pending-create:{msg_key}:{intent}:{food_name}")
        self._batch_create(self.pending_table, [{"fields": fields}], client_token=client_token)
        return {"status": "created", "pending_id": pending_id, "intent": intent, "food_name": food_name, "grams": grams}

    def _pending_task(self, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        return {
            "record_id": record_id,
            "pending_id": extract_text(fields.get(_pending_field("id"))),
            "user_id": extract_text(fields.get(_pending_field("user_id"))),
            "chat_id": extract_text(fields.get(_pending_field("chat_id"))),
            "source_message_id": extract_text(fields.get(_pending_field("source_message_id"))),
            "intent": extract_text(fields.get(_pending_field("intent"))),
            "food_name": extract_text(fields.get(_pending_field("food_name"))),
            "grams": extract_number(fields.get(_pending_field("grams"))),
            "meal_type": extract_text(fields.get(_pending_field("meal_type"))) or "unknown",
            "raw_text": extract_text(fields.get(_pending_field("raw_text"))),
            "status": extract_text(fields.get(_pending_field("status"))).lower(),
            "created_at": extract_number(fields.get(_pending_field("created_at"))) or 0.0,
            "expires_at": extract_number(fields.get(_pending_field("expires_at"))) or 0.0,
        }

    def load_active_pending(self, user_id: str, chat_id: str) -> dict[str, Any] | None:
        if not self.app_token or not self.pending_table:
            return None
        user_id = str(user_id or "").strip()
        chat_id = str(chat_id or "").strip()
        names = _unique_non_empty([_pending_field(key) for key in PENDING_FIELDS])
        now = timeutil.now_millis()
        best: dict[str, Any] | None = None
        for record_id, fields in _list_records(self.app_token, self.pending_table, names):
            if extract_text(fields.get(_pending_field("status"))).lower() != "pending":
                continue
            row_user_id = extract_text(fields.get(_pending_field("user_id")))
            if user_id and row_user_id and row_user_id != user_id:
                continue
            row_chat_id = extract_text(fields.get(_pending_field("chat_id")))
            if chat_id and row_chat_id and row_chat_id != chat_id:
                continue
            expires_at = extract_number(fields.get(_pending_field("expires_at"))) or 0.0
            if expires_at and expires_at < now:
                continue
            task = self._pending_task(record_id, fields)
            if best is None or float(task.get("created_at") or 0) > float(best.get("created_at") or 0):
                best = task
        return best

    def mark_pending_status(self, task: dict[str, Any], status: str) -> dict[str, Any]:
        return self._update_record(self.pending_table, str(task.get("record_id") or ""), {_pending_field("status"): status})

    # --- usage events -------------------------------------------------------

    def append_usage_event(self, event: dict[str, Any]) -> None:
        if not self.app_token or not self.usage_table:
            return
        try:
            fields = {
                _usage_field("event_id"): event.get("event_id", ""),
                _usage_field("event_type"): event.get("event_type", "message_processed"),
                _usage_field("message_id"): event.get("message_id", ""),
                _usage_field("user_id"): event.get("user_id", ""),
                _usage_field("preset"): event.get("preset", ""),
                _usage_field("planner"): event.get("planner", ""),
                _usage_field("status"): event.get("status", "ok"),
                _usage_field("error_type"): event.get("error_type", ""),
                _usage_field("action_count"): event.get("action_count", 0),
                _usage_field("reply_sent"): bool(event.get("reply_sent", False)),
                _usage_field("latency_ms"): event.get("latency_ms", 0),
                _usage_field("created_at"): timeutil.now_millis(),
            }
            from .bitable_adapter import usage_event_token

            self._batch_create(
                self.usage_table,
                [{"fields": fields}],
                client_token=usage_event_token(event.get("message_id", "") or event.get("event_id", "")),
            )
        except Exception as exc:  # noqa: BLE001 - metrics must never break the chain
            print(f"[usage_event_error] {type(exc).__name__}: {exc}")


def usage_event_token(message_id: str) -> str:
    return stable_uuid(f"calorie-agent:usage:{message_id}")
