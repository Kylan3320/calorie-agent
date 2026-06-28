from __future__ import annotations

import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from calorie_agent.legacy_app import DEFAULT_SETTINGS

from .fallback_reply import build_deterministic_fallback


SUCCESS_WORDS = (
    "已记录",
    "记录成功",
    "已撤销",
    "已删除",
    "已更新",
    "已创建",
    "已关闭",
    "已发送提醒",
    "已完成",
    "recorded",
    "successfully recorded",
)
FAILURE_WORDS = ("失败", "未完成", "没有完成", "需要补充", "请补充", "没有权限", "无权", "failed", "error")

NUMERIC_FIELD_WORDS = ("kcal", "卡", "千卡", "热量", "碳水", "蛋白", "蛋白质", "脂肪", "g", "克", "%", "％")
FOOD_CLAIM_WORDS = (
    "已记录",
    "记录成功",
    "已查询",
    "查询到",
    "查到",
    "已撤销",
    "已删除",
    "记录了",
    "撤销了",
)
MEDICAL_CLAIM_PATTERNS = (
    re.compile(r"一定有.{0,12}(疾病|病)"),
    re.compile(r"必须吃药"),
    re.compile(r"医学上判断"),
    re.compile(r"(诊断为|确诊|患有)"),
)
SKILL_EXECUTION_RE = re.compile(
    r"((执行|运行|调用|用了|使用).{0,24}\bskill\b|\bskill\b.{0,16}(执行|运行|查询|已))",
    re.IGNORECASE,
)

KNOWN_FOOD_TERMS = (
    "西瓜",
    "苹果",
    "香蕉",
    "米饭",
    "鸡蛋",
    "鸡胸",
    "鸡胸肉",
    "沙拉汁",
    "牛奶",
    "酸奶",
    "面包",
    "燕麦",
    "watermelon",
    "apple",
    "banana",
    "rice",
    "egg",
    "chicken",
    "sauce",
    "milk",
    "yogurt",
    "oat",
)

SUCCESS_STATUSES = {
    "success",
    "ok",
    "created",
    "updated",
    "deleted",
    "generated",
    "sent",
    "paused",
    "resumed",
    "snoozed",
    "found",
    "combo_used",
    "today_summary_replied",
    "cache_hit",
    "duplicate_skipped",
}
BLOCKED_STATUSES = {
    "pending",
    "failed",
    "denied",
    "permission_denied",
    "needs_confirmation",
    "no_match",
    "multiple_candidates",
    "error",
}


@dataclass(frozen=True)
class NumberMention:
    value: float
    raw: str
    unit: str | None = None
    context: str = ""


@dataclass(frozen=True)
class GuardViolation:
    type: str
    value: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {"type": self.type, "value": self.value, "reason": self.reason}


@dataclass(frozen=True)
class GuardInput:
    reply: str
    execution_facts: dict[str, Any]
    plan: dict[str, Any] = field(default_factory=dict)
    tool_results: list[dict[str, Any]] = field(default_factory=list)
    context: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class GuardResult:
    passed: bool
    reasons: list[str] = field(default_factory=list)
    safe_reply: str = ""
    fallback_used: bool = False
    violations: list[GuardViolation] = field(default_factory=list)
    facts_used: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reasons": self.reasons,
            "safe_reply": self.safe_reply,
            "fallback_used": self.fallback_used,
            "violations": [violation.to_dict() for violation in self.violations],
            "facts_used": self.facts_used,
            "warnings": self.warnings,
        }


def validate_reply(
    reply: str,
    execution_facts: dict[str, Any],
    tool_results: list[dict[str, Any]] | None = None,
    plan: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    fallback_reply: str | None = None,
    *,
    strict_numbers: bool | None = None,
    allow_rounding: bool | None = None,
    block_medical_claims: bool | None = None,
) -> GuardResult:
    """Validate that a natural-language reply stays inside execution facts."""

    facts = execution_facts if isinstance(execution_facts, dict) else {}
    results = _normalize_tool_results(tool_results)
    stripped = str(reply or "").strip()
    fallback = fallback_reply or build_deterministic_fallback(facts, results)
    if not stripped:
        violation = GuardViolation("unknown", "", "empty_reply")
        return GuardResult(False, ["empty_reply"], fallback, True, [violation])

    v2_enabled = _setting_bool("AGENT_REPLY_GUARD_V2_ENABLED", None, True)
    strict_numbers = _setting_bool("AGENT_REPLY_GUARD_STRICT_NUMBERS", strict_numbers, True)
    allow_rounding = _setting_bool("AGENT_REPLY_GUARD_ALLOW_ROUNDING", allow_rounding, True)
    block_medical_claims = _setting_bool("AGENT_REPLY_GUARD_BLOCK_MEDICAL_CLAIMS", block_medical_claims, True)

    violations: list[GuardViolation] = []
    facts_used: set[str] = set()

    number_violations, number_facts = _validate_numbers(stripped, facts, results, strict_numbers, allow_rounding)
    violations.extend(number_violations)
    facts_used.update(number_facts)
    violations.extend(_validate_pending_not_recorded(stripped, facts))
    violations.extend(_validate_failures_are_visible(stripped, facts, results))
    violations.extend(_validate_confirmations_are_visible(stripped, facts))
    if v2_enabled:
        violations.extend(_validate_food_names(stripped, facts, results))
        violations.extend(_validate_status_claims(stripped, facts, results))
        violations.extend(_validate_skill_not_claimed_as_action(stripped))
        violations.extend(_validate_privacy(stripped, facts, results))
        if block_medical_claims:
            violations.extend(_validate_medical_claims(stripped))

    reasons = [violation.reason for violation in violations]
    if violations:
        return GuardResult(False, reasons, fallback, True, violations, sorted(facts_used))
    return GuardResult(True, [], stripped, False, [], sorted(facts_used))


def extract_numbers_with_context(reply: str) -> list[NumberMention]:
    text = _strip_date_and_time_spans(str(reply or ""))
    mentions: list[NumberMention] = []
    for match in re.finditer(r"(?<![A-Za-z])-?\d+(?:\.\d+)?\s*(kcal|千卡|卡路里|卡|g|克|%|％)?", text, re.IGNORECASE):
        raw_number = match.group(0).strip()
        try:
            value = float(match.group(0).split()[0].rstrip("kcalg克卡千路里%％"))
        except ValueError:
            continue
        start = max(0, match.start() - 12)
        end = min(len(text), match.end() + 12)
        mentions.append(NumberMention(value=value, raw=raw_number, unit=match.group(1), context=text[start:end]))
    return mentions


def collect_allowed_numbers(execution_facts: dict[str, Any], tool_results: list[dict[str, Any]] | None = None) -> set[float]:
    values: set[float] = {100.0}

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, bool):
            return
        if isinstance(value, (int, float)):
            _add_number(values, float(value), key)
            return
        if isinstance(value, str):
            parsed = _parse_numeric_text(value)
            if parsed is not None and _is_numeric_fact_key(key):
                _add_number(values, parsed, key)
            return
        if isinstance(value, dict):
            for child_key, child in value.items():
                visit(child, str(child_key))
            return
        if isinstance(value, list):
            values.add(round(float(len(value)), 1))
            for item in value:
                visit(item, key)

    visit(execution_facts if isinstance(execution_facts, dict) else {})
    for result in _normalize_tool_results(tool_results):
        visit(result)
    return values


def collect_allowed_food_names(
    execution_facts: dict[str, Any],
    tool_results: list[dict[str, Any]] | None = None,
) -> set[str]:
    names: set[str] = set()

    def visit(value: Any, key: str = "") -> None:
        if isinstance(value, dict):
            for child_key, child in value.items():
                child_key = str(child_key)
                if child_key in {"message_text", "raw_text", "final_reply", "reply"}:
                    continue
                visit(child, child_key)
            return
        if isinstance(value, list):
            for item in value:
                visit(item, key)
            return
        if key in {"food_name", "input_name", "matched_name", "food_query", "query"}:
            text = str(value or "").strip()
            if text:
                names.add(text)

    visit(execution_facts if isinstance(execution_facts, dict) else {})
    for result in _normalize_tool_results(tool_results):
        visit(result)
    return names


def collect_successful_actions(tool_results: list[dict[str, Any]] | None = None) -> set[str]:
    actions: set[str] = set()
    for result in _normalize_tool_results(tool_results):
        tool = str(result.get("tool") or "")
        status = str(result.get("status") or "").strip().lower()
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        data_status = str(data.get("status") or "").strip().lower()
        if status in SUCCESS_STATUSES or data_status in SUCCESS_STATUSES:
            actions.add(tool)
            actions.add("any_success")
    return actions


def _validate_numbers(
    reply: str,
    facts: dict[str, Any],
    tool_results: list[dict[str, Any]],
    strict_numbers: bool,
    allow_rounding: bool,
) -> tuple[list[GuardViolation], set[str]]:
    if not strict_numbers:
        return [], set()
    allowed = collect_allowed_numbers(facts, tool_results)
    errors: list[GuardViolation] = []
    facts_used: set[str] = set()
    for mention in extract_numbers_with_context(reply):
        if _number_allowed(mention.value, allowed, allow_rounding):
            facts_used.add(f"number:{_fmt_number(mention.value)}")
            continue
        if not _number_needs_fact_check(mention):
            continue
        errors.append(
            GuardViolation(
                "unsupported_number",
                mention.raw,
                f"number_not_in_facts:{mention.raw}",
            )
        )
    return errors, facts_used


def _validate_food_names(reply: str, facts: dict[str, Any], tool_results: list[dict[str, Any]]) -> list[GuardViolation]:
    allowed = {_normalize_food_name(name) for name in collect_allowed_food_names(facts, tool_results)}
    allowed.discard("")
    if not allowed:
        return []

    errors: list[GuardViolation] = []
    for sentence in _sentences(reply):
        if not _contains_any(sentence, FOOD_CLAIM_WORDS):
            continue
        normalized_sentence = _normalize_food_name(sentence)
        for food in KNOWN_FOOD_TERMS:
            normalized_food = _normalize_food_name(food)
            if normalized_food and normalized_food in normalized_sentence and normalized_food not in allowed:
                errors.append(
                    GuardViolation(
                        "unsupported_food",
                        food,
                        f"food_not_in_facts:{food}",
                    )
                )
    return _unique_violations(errors)


def _validate_status_claims(reply: str, facts: dict[str, Any], tool_results: list[dict[str, Any]]) -> list[GuardViolation]:
    successful = _successful_actions_from_facts(facts) | collect_successful_actions(tool_results)
    blocked = _has_blocked_tool_result(tool_results)
    errors: list[GuardViolation] = []
    rules = [
        (("已记录", "记录成功"), {"record_food"}),
        (("已撤销",), {"undo_intake"}),
        (("已删除",), {"undo_intake", "clear_daily_intake", "delete_memory"}),
        (("已更新",), {"change_standard", "update_reminder_rule", "update_preference"}),
        (("已创建",), {"create_food", "create_pending", "create_combo", "update_reminder_rule"}),
        (("已关闭",), {"pause_reminders", "update_preference"}),
        (("已发送提醒", "提醒已发送"), {"send_reminder"}),
    ]
    for words, required in rules:
        if _contains_any(reply, words) and not successful.intersection(required):
            value = next(word for word in words if word in reply)
            errors.append(GuardViolation("unsupported_status", value, f"unsupported_status:{value}"))
    if _contains_any(reply, ("已完成",)) and "any_success" not in successful:
        errors.append(GuardViolation("unsupported_status", "已完成", "unsupported_status:已完成"))
    if blocked and _contains_any(reply, ("已完成", "已记录", "已更新", "已创建")) and "any_success" not in successful:
        errors.append(GuardViolation("unsupported_status", "blocked_result_claimed_done", "blocked_result_claimed_done"))
    return _unique_violations(errors)


def _validate_pending_not_recorded(reply: str, facts: dict[str, Any]) -> list[GuardViolation]:
    pending = facts.get("pending") if isinstance(facts.get("pending"), list) else []
    errors: list[GuardViolation] = []
    for item in pending:
        if not isinstance(item, dict):
            continue
        food = str(item.get("food_name") or "").strip()
        status = str(item.get("status") or "")
        if not food or status == "completed":
            continue
        for sentence in _sentences(reply):
            if food in sentence and _contains_any(sentence, ("已记录", "记录成功")) and not _contains_any(sentence, FAILURE_WORDS):
                errors.append(
                    GuardViolation(
                        "unsupported_status",
                        food,
                        f"pending_claimed_recorded:{food}",
                    )
                )
                break
    return errors


def _validate_failures_are_visible(
    reply: str,
    facts: dict[str, Any],
    tool_results: list[dict[str, Any]],
) -> list[GuardViolation]:
    errors = facts.get("errors") if isinstance(facts.get("errors"), list) else []
    has_tool_error = any(str(result.get("status") or "").lower() in {"error", "failed"} for result in tool_results)
    if not errors and not has_tool_error:
        return []
    if _contains_any(reply, FAILURE_WORDS):
        return []
    return [GuardViolation("unsupported_status", "failure", "failed_action_not_explained")]


def _validate_confirmations_are_visible(reply: str, facts: dict[str, Any]) -> list[GuardViolation]:
    confirmations = facts.get("confirmations") if isinstance(facts.get("confirmations"), list) else []
    if not confirmations:
        return []
    if _contains_any(reply, ("确认", "候选", "更准确", "confirm", "candidate")):
        return []
    return [GuardViolation("unsupported_status", "confirmation", "confirmation_not_explained")]


def _validate_skill_not_claimed_as_action(reply: str) -> list[GuardViolation]:
    match = SKILL_EXECUTION_RE.search(reply)
    if not match:
        return []
    return [GuardViolation("unsupported_action", match.group(0), f"skill_claimed_as_action:{match.group(0)}")]


def _validate_privacy(reply: str, facts: dict[str, Any], tool_results: list[dict[str, Any]]) -> list[GuardViolation]:
    if not _has_permission_denied(facts, tool_results):
        return []
    normalized = _normalize_food_name(reply)
    contains_food = any(_normalize_food_name(food) in normalized for food in KNOWN_FOOD_TERMS)
    contains_private_number = any(_number_needs_fact_check(mention) for mention in extract_numbers_with_context(reply))
    contains_nutrition_word = _contains_any(reply, ("kcal", "热量", "碳水", "蛋白", "脂肪", "摄入", "吃了", "已记录"))
    if contains_food or contains_private_number or contains_nutrition_word:
        return [GuardViolation("privacy_risk", "permission_denied", "permission_denied_detail_leak")]
    if not _contains_any(reply, ("没有权限", "无权", "不能查看", "权限", "permission")):
        return [GuardViolation("privacy_risk", "permission_denied", "permission_denied_not_explained")]
    return []


def _validate_medical_claims(reply: str) -> list[GuardViolation]:
    for pattern in MEDICAL_CLAIM_PATTERNS:
        match = pattern.search(reply)
        if match:
            return [GuardViolation("medical_claim", match.group(0), f"medical_claim:{match.group(0)}")]
    return []


def _successful_actions_from_facts(facts: dict[str, Any]) -> set[str]:
    actions: set[str] = set()
    if _non_empty_list(facts.get("recorded")):
        actions.update({"record_food", "any_success"})
    if any(isinstance(item, dict) and item.get("status") == "deleted" for item in _as_list(facts.get("undo"))):
        actions.update({"undo_intake", "any_success"})
    if any(isinstance(item, dict) and item.get("status") in {"created", "updated"} for item in _as_list(facts.get("standard_updates"))):
        actions.update({"change_standard", "any_success"})
    if _non_empty_list(facts.get("cleanup")):
        actions.update({"clear_daily_intake", "any_success"})
    if any(isinstance(item, dict) and item.get("status") == "success" for item in _as_list(facts.get("intake_nutrition_queries"))):
        actions.update({"query_intake_food_nutrition", "any_success"})

    reminders = facts.get("reminders") if isinstance(facts.get("reminders"), dict) else {}
    for event in _as_list(reminders.get("events")):
        if not isinstance(event, dict):
            continue
        tool = str(event.get("tool") or "")
        status = str(event.get("status") or "")
        if status in SUCCESS_STATUSES:
            actions.update({tool, "any_success"})

    memory = facts.get("memory") if isinstance(facts.get("memory"), dict) else {}
    for event in _as_list(memory.get("events")):
        if not isinstance(event, dict):
            continue
        tool = str(event.get("tool") or "")
        status = str(event.get("status") or "")
        if status in SUCCESS_STATUSES:
            actions.update({tool, "any_success"})
    return actions


def _has_blocked_tool_result(tool_results: list[dict[str, Any]]) -> bool:
    for result in tool_results:
        status = str(result.get("status") or "").strip().lower()
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        data_status = str(data.get("status") or "").strip().lower()
        if status in BLOCKED_STATUSES or data_status in BLOCKED_STATUSES:
            return True
    return False


def _has_permission_denied(facts: dict[str, Any], tool_results: list[dict[str, Any]]) -> bool:
    for result in tool_results:
        status = str(result.get("status") or "").strip().lower()
        error = str(result.get("error") or "")
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        message = " ".join([status, error, str(data.get("message") or ""), str(data.get("status") or "")])
        if "permission_denied" in message or "cross_user" in message or "denied" == status:
            return True
    for error in _as_list(facts.get("errors")):
        if isinstance(error, dict) and _contains_any(str(error), ("permission_denied", "cross_user", "denied", "没有权限")):
            return True
    return False


def _number_needs_fact_check(mention: NumberMention) -> bool:
    if mention.unit:
        return True
    context = mention.context.lower()
    return any(word.lower() in context for word in NUMERIC_FIELD_WORDS)


def _number_allowed(value: float, allowed: set[float], allow_rounding: bool) -> bool:
    tolerance = 1.0 if allow_rounding else 0.05
    rounded = round(float(value) + 1e-9, 1)
    return any(abs(rounded - allowed_value) <= tolerance for allowed_value in allowed)


def _add_number(values: set[float], value: float, key: str = "") -> None:
    rounded = round(float(value) + 1e-9, 1)
    values.add(rounded)
    if float(value).is_integer():
        values.add(round(float(int(value)), 1))
    normalized_key = key.lower()
    if any(token in normalized_key for token in ("ratio", "rate", "progress", "percent")):
        values.add(round(float(value) * 100 + 1e-9, 1))


def _allowed_numeric_values(facts: dict[str, Any]) -> set[float]:
    return collect_allowed_numbers(facts)


def _is_numeric_fact_key(key: str) -> bool:
    key = key.lower()
    return any(
        token in key
        for token in (
            "grams",
            "kcal",
            "carbs",
            "protein",
            "fat",
            "count",
            "ratio",
            "rate",
            "progress",
            "percent",
            "days",
            "default_grams",
        )
    )


def _parse_numeric_text(value: str) -> float | None:
    text = value.strip()
    if not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _strip_date_and_time_spans(text: str) -> str:
    text = re.sub(r"\d{4}-\d{2}-\d{2}", " ", text)
    text = re.sub(r"\b\d{1,2}:\d{2}\b", " ", text)
    return text


def _normalize_tool_results(tool_results: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for item in tool_results or []:
        if isinstance(item, dict):
            normalized.append(item)
        elif hasattr(item, "to_dict"):
            payload = item.to_dict()
            if isinstance(payload, dict):
                normalized.append(payload)
    return normalized


def _normalize_food_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).lower()
    return re.sub(r"[\s,，。.!！?？:：;；、_\-（）()]+", "", text)


def _sentences(reply: str) -> list[str]:
    return [part.strip() for part in re.split(r"[。\n.!?？；;]", reply) if part.strip()]


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(word.lower() in lowered for word in words)


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _non_empty_list(value: Any) -> bool:
    return isinstance(value, list) and bool(value)


def _unique_violations(violations: list[GuardViolation]) -> list[GuardViolation]:
    seen: set[tuple[str, str, str]] = set()
    unique: list[GuardViolation] = []
    for violation in violations:
        key = (violation.type, violation.value, violation.reason)
        if key in seen:
            continue
        seen.add(key)
        unique.append(violation)
    return unique


def _setting_bool(key: str, explicit: bool | None, default: bool) -> bool:
    if explicit is not None:
        return bool(explicit)
    return str(os.getenv(key, DEFAULT_SETTINGS.get(key, str(default).lower()))).strip().lower() in {"1", "true", "yes", "on"}


def _fmt_number(value: float) -> str:
    if float(value).is_integer():
        return str(int(value))
    return f"{value:g}"
