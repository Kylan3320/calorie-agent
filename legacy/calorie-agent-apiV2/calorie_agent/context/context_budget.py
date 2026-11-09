from __future__ import annotations

from dataclasses import asdict, is_dataclass, replace
import json
from typing import Any


LOW_PRIORITY_CONTEXT_FIELDS = (
    "retrieved_facts",
    "structured_memory",
    "short_term_context",
    "loaded_skills",
)


def estimate_chars(value: Any) -> int:
    try:
        return len(json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, default=str))
    except Exception:
        return len(str(value))


def trim_context(context: Any, budget_chars: int) -> Any:
    budget = max(0, int(budget_chars or 0))
    original_chars = estimate_chars(context)
    report = dict(getattr(context, "budget_report", {}) or {})
    report.update(
        {
            "budget_chars": budget,
            "estimated_chars_before_trim": original_chars,
            "trimmed": False,
        }
    )

    if original_chars <= budget:
        report["estimated_chars"] = original_chars
        return replace(context, budget_report=report) if is_dataclass(context) else context

    warnings = list(getattr(context, "warnings", []) or [])
    warnings.append(f"context_trimmed: estimated_chars={original_chars} budget_chars={budget}")

    candidate = context
    for field_name in LOW_PRIORITY_CONTEXT_FIELDS:
        candidate = _trim_field(candidate, field_name)
        current_chars = estimate_chars(candidate)
        if current_chars <= budget:
            report.update({"estimated_chars": current_chars, "trimmed": True})
            return replace(candidate, budget_report=report, warnings=warnings)

    final_chars = estimate_chars(candidate)
    report.update({"estimated_chars": final_chars, "trimmed": True})
    return replace(candidate, budget_report=report, warnings=warnings)


def _trim_field(context: Any, field_name: str) -> Any:
    if not is_dataclass(context) or not hasattr(context, field_name):
        return context

    value = getattr(context, field_name)
    if field_name == "loaded_skills":
        trimmed_value = _compact_skills(value)
    elif field_name == "short_term_context":
        trimmed_value = _compact_short_term(value)
    elif field_name == "structured_memory":
        trimmed_value = _compact_memory(value)
    elif field_name == "retrieved_facts":
        trimmed_value = _compact_facts(value)
    else:
        trimmed_value = _compact_value(value)
    return replace(context, **{field_name: trimmed_value})


def _compact_skills(skills: Any) -> list[Any]:
    if not isinstance(skills, list) or not skills:
        return []

    top = skills[0]
    if is_dataclass(top):
        updates: dict[str, Any] = {}
        if hasattr(top, "description"):
            updates["description"] = _truncate(getattr(top, "description"), 180)
        if hasattr(top, "reasons"):
            updates["reasons"] = list(getattr(top, "reasons") or [])[:3]
        if hasattr(top, "required_context"):
            updates["required_context"] = list(getattr(top, "required_context") or [])[:8]
        if hasattr(top, "strategy"):
            updates["strategy"] = _truncate(getattr(top, "strategy"), 280)
        if hasattr(top, "must_ask_when"):
            updates["must_ask_when"] = list(getattr(top, "must_ask_when") or [])[:3]
        if hasattr(top, "forbidden"):
            updates["forbidden"] = list(getattr(top, "forbidden") or [])[:3]
        if hasattr(top, "examples"):
            updates["examples"] = []
        return [replace(top, **updates)]

    return [_compact_value(top)]


def _compact_short_term(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in ("current_date", "timezone", "recent_action_results", "active_pending_summary"):
        if key in value:
            result[key] = _compact_value(value[key])
    return result


def _compact_memory(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key in (
        "aliases",
        "default_grams",
        "combos",
        "preferences",
        "planner_profile",
        "member_relations_summary",
        "permissions_summary",
    ):
        if key in value:
            result[key] = _compact_value(value[key])
    return result


def _compact_facts(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, dict):
            source = item.get("source")
            status = item.get("status")
            compact = {}
            if source is not None:
                compact["source"] = source
            if status is not None:
                compact["status"] = status
            result[key] = compact or {"source": "trimmed"}
        else:
            result[key] = _compact_value(item)
    return result


def _compact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _truncate(value, 240)
    if isinstance(value, list):
        return [_compact_value(item) for item in value[:5]]
    if isinstance(value, dict):
        return {key: _compact_value(item) for key, item in list(value.items())[:8]}
    if is_dataclass(value):
        return _compact_value(asdict(value))
    return value


def _truncate(value: Any, limit: int) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 12)].rstrip() + "...[trimmed]"


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    return value
