"""Reply guard: final fact check before the reply leaves the graph.

Two checks (the project's core AI-risk control, kept from V0/V2):
1. Number tracing: every number in the reply must exist in execution facts,
   or be a simple remainder/percent derived from a totals-vs-standard pair.
2. Claim honesty: "已记录"/"已撤销" may only appear when a matching ok fact exists.
"""

from __future__ import annotations

import json
import re
from typing import Any

from ..state import AgentState
from ..storage.timeutil import round_number

_SAFE_FALLBACK = "这条回复没有通过事实校验，已替换为安全回复。请重新描述你的需求，或稍后再试。"
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?")


def reply_guard(state: AgentState) -> dict[str, Any]:
    reply = str(state.get("final_reply") or "")
    results = state.get("tool_results") or []
    pending_route = state.get("pending_route") or {}
    violations: list[dict[str, Any]] = []

    allowed = _allowed_numbers(results, state.get("context") or {})
    for number in _numbers_in_text(reply):
        if number not in allowed:
            violations.append({"type": "unsupported_number", "number": number})
            break

    has_recorded_fact = _has_recorded_fact(results, pending_route)
    write_attempted_incomplete = any(
        isinstance(item, dict) and item.get("status") in {"pending_created", "needs_confirmation", "needs_confirmation_plan"}
        for item in results
    )
    if "已记录" in reply and not has_recorded_fact and write_attempted_incomplete:
        violations.append({"type": "pending_claimed_recorded"})
    if "已撤销" in reply and not _has_undo_fact(results):
        violations.append({"type": "undo_claimed_without_fact"})
    if _claims_knowledge_sourced(reply) and not _has_knowledge_fact(results):
        violations.append({"type": "knowledge_claimed_sourced"})

    if violations:
        return {
            "final_reply": _SAFE_FALLBACK,
            "reply_source": "safe_fallback",
            "guard_result": {"passed": False, "violations": violations, "fallback_used": True},
        }
    return {"guard_result": {"passed": True, "violations": [], "fallback_used": False}}


def _allowed_numbers(results: list[Any], context: dict[str, Any]) -> set[float]:
    facts_blob = json.dumps({"tool_results": results, "context": context}, ensure_ascii=False, default=str)
    allowed = {float(item) for item in _NUMBER_RE.findall(facts_blob)}
    allowed.update({0.0, 1.0, 2.0, 100.0})

    for totals, standard in _totals_standard_pairs(results, context):
        for key in ("kcal", "carbs", "protein", "fat"):
            value = float(totals.get(key) or 0)
            target = float(standard.get(key) or 0)
            if target > 0:
                allowed.add(round_number(max(target - value, 0)))
                allowed.add(round_number(value / target * 100))
    return allowed


def _totals_standard_pairs(results: list[Any], context: dict[str, Any]) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    nutrition_keys = {"carbs", "protein", "fat", "kcal"}
    totals_candidates = _find_dicts_with_keys(results, nutrition_keys)
    standard_dicts = _find_dicts_with_keys(results, nutrition_keys | {"source"})
    pairs: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for totals in totals_candidates:
        for standard in standard_dicts:
            if totals is not standard:
                pairs.append((totals, standard))
    _ = context
    return pairs


def _find_dicts_with_keys(node: Any, keys: set[str], depth: int = 0) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    if depth > 8:
        return found
    if isinstance(node, dict):
        if keys <= set(node.keys()):
            found.append(node)
        for value in node.values():
            found.extend(_find_dicts_with_keys(value, keys, depth + 1))
    elif isinstance(node, list):
        for item in node:
            found.extend(_find_dicts_with_keys(item, keys, depth + 1))
    return found


def _numbers_in_text(text: str) -> set[float]:
    return {float(item) for item in _NUMBER_RE.findall(text)}


def _has_recorded_fact(results: list[Any], pending_route: dict[str, Any]) -> bool:
    if pending_route.get("handled") and pending_route.get("route") == "answer":
        return True
    for item in results:
        if not isinstance(item, dict):
            continue
        if item.get("tool") == "record_food" and item.get("status") == "ok":
            return True
    return False


def _has_undo_fact(results: list[Any]) -> bool:
    for item in results:
        if not isinstance(item, dict):
            continue
        if item.get("tool") != "undo_last" or item.get("status") != "ok":
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        undo = data.get("undo_result") if isinstance(data.get("undo_result"), dict) else {}
        if undo.get("status") == "deleted":
            return True
    return False


def _claims_knowledge_sourced(reply: str) -> bool:
    return "根据知识库" in reply or "知识库显示" in reply


def _has_knowledge_fact(results: list[Any]) -> bool:
    return any(
        isinstance(item, dict) and item.get("tool") == "query_knowledge" and item.get("status") == "ok"
        for item in results
    )


__all__ = ["reply_guard"]
