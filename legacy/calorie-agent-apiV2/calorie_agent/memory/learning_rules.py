from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import re
from typing import Any, Callable

from ..domain.food_match import choose_food_match


MEMORY_TYPES = {"alias", "default_grams", "combo", "preference", "query_pattern"}
DANGEROUS_WORDS = ("删除", "删", "撤销", "清理", "delete", "undo", "clear")


@dataclass(frozen=True)
class LearningEvent:
    user_id: str
    message_id: str = ""
    trace_id: str = ""
    raw_text: str = ""
    plan: dict[str, Any] = field(default_factory=dict)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    final_reply: str = ""
    user_feedback: str = ""
    timestamp: int = 0
    recent_records: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class MemoryCandidate:
    candidate_id: str
    user_id: str
    memory_type: str
    key: str
    value: dict[str, Any]
    confidence: float
    source: str
    source_trace_id: str = ""
    source_message_id: str = ""
    status: str = "candidate"
    created_at: int = 0
    updated_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RuleOptions:
    min_confidence: float = 0.8
    require_confirm_alias: bool = True
    default_grams_min_samples: int = 3
    auto_confirm_explicit: bool = True


FoodLookup = Callable[[str], dict[str, Any] | None]


def generate_memory_candidates(
    event: LearningEvent,
    *,
    options: RuleOptions | None = None,
    food_lookup: FoodLookup | None = None,
    include_trace_based: bool = True,
) -> list[MemoryCandidate]:
    opts = options or RuleOptions()
    candidates: list[MemoryCandidate] = []

    candidates.extend(_explicit_alias_candidates(event, opts, food_lookup))
    candidates.extend(_explicit_combo_candidates(event, opts))
    candidates.extend(_explicit_preference_candidates(event, opts))

    if include_trace_based and _event_success(event):
        candidates.extend(_successful_alias_candidates(event, opts))
        candidates.extend(_default_grams_candidates(event, opts))
        candidates.extend(_query_pattern_candidates(event, opts))

    return _dedupe(candidates)


def food_lookup_from_legacy(legacy: Any) -> FoodLookup:
    def lookup(food_query: str) -> dict[str, Any] | None:
        try:
            foods = legacy._load_food_database()
        except Exception:
            return None
        match = choose_food_match(food_query, foods)
        if match.get("status") not in {"auto_matched", "needs_confirmation"}:
            return None
        candidate = match.get("candidate") if isinstance(match.get("candidate"), dict) else {}
        food = candidate.get("food") if isinstance(candidate.get("food"), dict) else {}
        return food or None

    return lookup


def _explicit_alias_candidates(
    event: LearningEvent,
    opts: RuleOptions,
    food_lookup: FoodLookup | None,
) -> list[MemoryCandidate]:
    text = _clean_text(event.raw_text)
    pairs = _alias_pairs(text)
    result: list[MemoryCandidate] = []
    for alias, food_query in pairs:
        food = food_lookup(food_query) if food_lookup is not None else None
        status = "confirmed" if food and opts.auto_confirm_explicit else "candidate"
        confidence = 1.0 if status == "confirmed" else opts.min_confidence
        value = {
            "alias": alias,
            "food_name": str((food or {}).get("name") or food_query).strip(),
            "food_id": str((food or {}).get("food_id") or ""),
        }
        result.append(_candidate(event, "alias", alias, value, confidence, "explicit_user", status))
    return result


def _alias_pairs(text: str) -> list[tuple[str, str]]:
    patterns = (
        r"^以后\s*(?P<alias>.+?)\s*(?:就是|就代表|代表)\s*(?P<food>.+?)$",
        r"^我说\s*(?P<alias>.+?)\s*(?:就是|就代表|代表)\s*(?P<food>.+?)$",
        r"^(?P<alias>.+?)\s*是\s*(?P<food>.+?)\s*的别名$",
        r"^from now on\s+(?P<alias>.+?)\s+means\s+(?P<food>.+?)$",
    )
    pairs: list[tuple[str, str]] = []
    for pattern in patterns:
        match = re.search(pattern, text, flags=re.IGNORECASE)
        if not match:
            continue
        alias = _strip_phrase(match.group("alias"))
        food = _strip_phrase(match.group("food"))
        if alias and food and alias != food:
            pairs.append((alias, food))
    return pairs


def _successful_alias_candidates(event: LearningEvent, opts: RuleOptions) -> list[MemoryCandidate]:
    result: list[MemoryCandidate] = []
    for tool_result in event.tool_results:
        payload = _payload(tool_result)
        food_match = payload.get("food_match") if isinstance(payload.get("food_match"), dict) else {}
        if not food_match:
            continue
        status = str(payload.get("status") or tool_result.get("status") or "")
        if status not in {"today_summary_replied", "success", "ok", "created"}:
            continue
        candidate = food_match.get("candidate") if isinstance(food_match.get("candidate"), dict) else {}
        food = candidate.get("food") if isinstance(candidate.get("food"), dict) else {}
        reason = str(candidate.get("reason") or "")
        score = _float(candidate.get("score"), _float(food_match.get("score"), 0.0))
        alias = _strip_phrase(food_match.get("query"))
        food_name = _strip_phrase(food.get("name") or payload.get("food_name"))
        if reason in {"exact", "alias_exact"}:
            continue
        if score < opts.min_confidence or not alias or not food_name or alias == food_name:
            continue
        value = {"alias": alias, "food_name": food_name, "food_id": str(food.get("food_id") or "")}
        result.append(_candidate(event, "alias", alias, value, score, "successful_trace", "candidate"))
    return result


def _default_grams_candidates(event: LearningEvent, opts: RuleOptions) -> list[MemoryCandidate]:
    records = list(event.recent_records)
    for tool_result in event.tool_results:
        payload = _payload(tool_result)
        for item in payload.get("items", []) if isinstance(payload.get("items"), list) else []:
            if isinstance(item, dict):
                records.append(item)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        food_name = _strip_phrase(record.get("food_name") or record.get("name"))
        grams = _float(record.get("grams"), 0.0)
        if not food_name or grams <= 0:
            continue
        grouped.setdefault(food_name, []).append(record)

    candidates: list[MemoryCandidate] = []
    for food_name, items in grouped.items():
        grams_values = [_float(item.get("grams"), 0.0) for item in items]
        grams_values = [value for value in grams_values if value > 0]
        if len(grams_values) < opts.default_grams_min_samples or not _stable_grams(grams_values):
            continue
        grams = round(sum(grams_values) / len(grams_values), 1)
        confidence = min(0.99, 0.65 + len(grams_values) / 20)
        value = {
            "food_name": food_name,
            "food_id": str(items[-1].get("food_id") or ""),
            "grams": grams,
            "sample_count": len(grams_values),
            "method": "mean_stable",
        }
        candidates.append(_candidate(event, "default_grams", food_name, value, confidence, "repeated_pattern", "candidate"))
    return candidates


def _explicit_combo_candidates(event: LearningEvent, opts: RuleOptions) -> list[MemoryCandidate]:
    text = _clean_text(event.raw_text)
    match = re.search(r"^以后把(?P<items>.+?)叫(?P<name>.+?)$", text)
    if not match:
        return []
    combo_name = _strip_phrase(match.group("name"))
    items = _parse_combo_items(match.group("items"))
    if not combo_name or not items:
        return []
    value = {"combo_name": combo_name, "meal_type": _infer_meal_type(combo_name), "items": items}
    return [_candidate(event, "combo", combo_name, value, 1.0, "explicit_user", "confirmed")]


def _explicit_preference_candidates(event: LearningEvent, opts: RuleOptions) -> list[MemoryCandidate]:
    text = _clean_text(event.raw_text)
    if _dangerous_text(text):
        return []
    compact_terms = ("回复简单点", "回复简洁", "别解释太多", "不要每次说那么多", "简单点")
    detailed_terms = ("详细解释", "说详细点", "以后详细")
    if any(term in text for term in compact_terms):
        value = {"key": "reply_style", "value": "compact"}
        return [_candidate(event, "preference", "reply_style", value, 1.0, "explicit_user", "confirmed")]
    if any(term in text for term in detailed_terms):
        value = {"key": "reply_style", "value": "detailed"}
        return [_candidate(event, "preference", "reply_style", value, 1.0, "explicit_user", "confirmed")]
    return []


def _query_pattern_candidates(event: LearningEvent, opts: RuleOptions) -> list[MemoryCandidate]:
    text = _clean_text(event.raw_text)
    if "还剩多少" not in text:
        return []
    tools = {str(item.get("tool") or "") for item in event.tool_results if isinstance(item, dict)}
    if "query_today" not in tools:
        return []
    value = {"pattern": "还剩多少", "tool": "query_today"}
    return [_candidate(event, "query_pattern", "还剩多少", value, 0.85, "successful_trace", "candidate")]


def _candidate(
    event: LearningEvent,
    memory_type: str,
    key: str,
    value: dict[str, Any],
    confidence: float,
    source: str,
    status: str,
) -> MemoryCandidate:
    now = int(event.timestamp or 0)
    candidate_id = _candidate_id(event.user_id, memory_type, key, value)
    return MemoryCandidate(
        candidate_id=candidate_id,
        user_id=str(event.user_id or ""),
        memory_type=memory_type,
        key=str(key or ""),
        value=value,
        confidence=round(max(0.0, min(1.0, float(confidence))), 3),
        source=source,
        source_trace_id=str(event.trace_id or ""),
        source_message_id=str(event.message_id or ""),
        status=status,
        created_at=now,
        updated_at=now,
    )


def _dedupe(candidates: list[MemoryCandidate]) -> list[MemoryCandidate]:
    by_key: dict[tuple[str, str, str], MemoryCandidate] = {}
    for candidate in candidates:
        key = (candidate.memory_type, candidate.key, json.dumps(candidate.value, ensure_ascii=False, sort_keys=True))
        existing = by_key.get(key)
        if existing is None or candidate.confidence > existing.confidence:
            by_key[key] = candidate
    return list(by_key.values())


def _candidate_id(user_id: str, memory_type: str, key: str, value: dict[str, Any]) -> str:
    raw = json.dumps(
        {"user_id": user_id, "memory_type": memory_type, "key": key, "value": value},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _parse_combo_items(text: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for match in re.finditer(r"(?P<food>[^\d\s,，、]+)\s*(?P<grams>\d+(?:\.\d+)?)\s*g", text, flags=re.IGNORECASE):
        food = _strip_phrase(match.group("food"))
        grams = _float(match.group("grams"), 0.0)
        if food and grams > 0:
            items.append({"food_query": food, "grams": round(grams, 1), "meal_type": "unknown"})
    return items


def _infer_meal_type(text: str) -> str:
    if "早餐" in text or "早饭" in text:
        return "breakfast"
    if "午餐" in text or "午饭" in text:
        return "lunch"
    if "晚餐" in text or "晚饭" in text:
        return "dinner"
    return "unknown"


def _stable_grams(values: list[float]) -> bool:
    if not values:
        return False
    mean = sum(values) / len(values)
    if mean <= 0:
        return False
    return max(abs(value - mean) for value in values) <= max(5.0, mean * 0.1)


def _event_success(event: LearningEvent) -> bool:
    if event.plan and str(event.plan.get("needs_confirmation")).lower() == "true":
        return False
    if not event.tool_results:
        return False
    failed_statuses = {"error", "failed", "validation_failed", "invalid", "denied", "ambiguous"}
    for result in event.tool_results:
        if not isinstance(result, dict):
            continue
        if str(result.get("tool") or "") in {"undo_intake", "clear_daily_intake", "delete_memory"}:
            return False
        status = str(result.get("status") or "").lower()
        if status in failed_statuses:
            return False
    return True


def _payload(tool_result: dict[str, Any]) -> dict[str, Any]:
    data = tool_result.get("data") if isinstance(tool_result.get("data"), dict) else {}
    return {**tool_result, **data}


def _clean_text(text: Any) -> str:
    return _strip_phrase(text).replace("，", ",")


def _strip_phrase(value: Any) -> str:
    return str(value or "").strip().strip("。.!！?？,，；;：:\"'“”‘’")


def _dangerous_text(text: str) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in DANGEROUS_WORDS)


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
