"""Reply guard node wrapper."""

import re
from typing import Any

from ..schemas import GuardResult
from ..state import LangGraphAgentState


def reply_guard(state: LangGraphAgentState) -> dict[str, Any]:
    if state.get("mode") == "core_candidate":
        return _core_reply_guard(state)
    if state.get("mode") == "readonly_candidate":
        return _readonly_reply_guard(state)
    if state.get("mode") == "write_candidate":
        return _write_reply_guard(state)

    reply = str(state.get("final_reply") or "")
    results = state.get("tool_results", [])
    decision = state.get("policy_decision", {})
    violations: list[dict[str, Any]] = []

    blocked = decision.get("blocked_actions") if isinstance(decision, dict) else []
    blocked_tools = {str(item.get("tool") or "") for item in blocked if isinstance(item, dict)}
    if "record_food" in blocked_tools and ("已记录" in reply or "写入成功" in reply):
        violations.append({"type": "blocked_write_claimed_success", "text": reply})
    if "undo_intake" in blocked_tools and ("已撤销" in reply or "已删除" in reply):
        violations.append({"type": "blocked_destructive_claimed_success", "text": reply})

    allowed_numbers = _allowed_numbers(results)
    for number in _numbers(reply):
        if number not in allowed_numbers and number not in {0.0, 1.0, 2.0}:
            violations.append({"type": "unsupported_number", "number": number})

    if violations:
        safe_reply = "V0 已完成规划，但回复包含未通过事实校验的内容，已切换为安全回复。"
        guard = GuardResult(False, safe_reply=safe_reply, violations=violations, fallback_used=True)
        return {"final_reply": safe_reply, "guard_result": guard.to_dict()}
    guard = GuardResult(True)
    return {"guard_result": guard.to_dict()}


def _readonly_reply_guard(state: LangGraphAgentState) -> dict[str, Any]:
    candidate = dict(state.get("readonly_candidate") or {})
    if candidate.get("status") != "ok":
        guard = GuardResult(False, violations=[{"type": "candidate_not_ok", "status": candidate.get("status", "")}], fallback_used=True)
        candidate["reply_guard"] = guard.to_dict()
        return {"guard_result": guard.to_dict(), "readonly_candidate": candidate}

    reply = str(state.get("final_reply") or "")
    violations: list[dict[str, Any]] = []
    if not reply:
        violations.append({"type": "empty_reply"})
    blocked_phrases = ("已记录本次", "写入成功", "已写入", "已撤销", "已删除")
    if any(phrase in reply for phrase in blocked_phrases):
        violations.append({"type": "write_or_delete_claim", "text": reply})
    allowed_numbers = _allowed_numbers(state.get("tool_results", []))
    allowed_numbers.update({0.0, 1.0, 2.0, 3.0})
    for number in _numbers(reply):
        if number not in allowed_numbers:
            violations.append({"type": "unsupported_number", "number": number})

    if violations:
        guard = GuardResult(False, violations=violations, fallback_used=True)
        candidate["status"] = _fallback_status_from_violations(violations)
        candidate["selected_reply_source"] = "current_runtime"
        candidate["blocked_reason"] = ";".join(str(item.get("type") or "") for item in violations if isinstance(item, dict))
        candidate["fallback_used"] = True
        candidate["reply_guard"] = guard.to_dict()
    else:
        guard = GuardResult(True)
        candidate["reply_guard"] = guard.to_dict()
    return {"guard_result": guard.to_dict(), "readonly_candidate": candidate}


def _write_reply_guard(state: LangGraphAgentState) -> dict[str, Any]:
    candidate = dict(state.get("write_candidate") or {})
    if candidate.get("status") != "ok":
        guard = GuardResult(False, violations=[{"type": "candidate_not_ok", "status": candidate.get("status", "")}], fallback_used=True)
        candidate["reply_guard"] = guard.to_dict()
        return {"guard_result": guard.to_dict(), "write_candidate": candidate}

    reply = str(state.get("final_reply") or "")
    results = state.get("tool_results", [])
    violations: list[dict[str, Any]] = []
    if not reply:
        violations.append({"type": "empty_reply"})
    if _pending_or_confirmation(results) and ("宸茶褰?" in reply or "璁板綍鎴愬姛" in reply):
        violations.append({"type": "pending_claimed_recorded"})
    if _duplicate_only(results) and "鏂拌褰?" in reply:
        violations.append({"type": "duplicate_claimed_new_record"})
    if any(word in reply for word in ("宸叉挙閿€", "宸插垹闄?")):
        violations.append({"type": "destructive_claim"})
    allowed_numbers = _allowed_numbers(results)
    allowed_numbers.update({0.0, 1.0, 2.0, 3.0})
    for number in _numbers(reply):
        if number not in allowed_numbers:
            violations.append({"type": "unsupported_number", "number": number})

    if violations:
        guard = GuardResult(False, violations=violations, fallback_used=True)
        candidate["status"] = "fallback_reply_guard_failed"
        candidate["selected_reply_source"] = "current_runtime"
        candidate["blocked_reason"] = ";".join(str(item.get("type") or "") for item in violations)
        candidate["fallback_used"] = True
        candidate["reply_guard"] = guard.to_dict()
    else:
        guard = GuardResult(True)
        candidate["reply_guard"] = guard.to_dict()
    return {"guard_result": guard.to_dict(), "write_candidate": candidate}


def _core_reply_guard(state: LangGraphAgentState) -> dict[str, Any]:
    candidate = dict(state.get("core_candidate") or {})
    if candidate.get("status") != "ok":
        guard = GuardResult(False, violations=[{"type": "candidate_not_ok", "status": candidate.get("status", "")}], fallback_used=True)
        candidate["reply_guard"] = guard.to_dict()
        return {"guard_result": guard.to_dict(), "core_candidate": candidate}

    source_guard = state.get("guard_result") if isinstance(state.get("guard_result"), dict) else {}
    violations: list[dict[str, Any]] = []
    if source_guard and source_guard.get("passed") is False:
        violations.append({"type": "source_guard_failed", "source_guard": source_guard})
    if not str(state.get("final_reply") or "").strip():
        violations.append({"type": "empty_reply"})
    if _has_real_write_claim(state.get("tool_results", [])):
        violations.append({"type": "real_write_claim"})

    if violations:
        guard = GuardResult(False, violations=violations, fallback_used=True)
        candidate = {
            **candidate,
            "status": "fallback_reply_guard_failed",
            "selected_reply_source": "current_runtime",
            "blocked_reason": ";".join(str(item.get("type") or "") for item in violations),
            "fallback_used": True,
            "reply_guard": guard.to_dict(),
        }
    else:
        guard = GuardResult(True)
        candidate["reply_guard"] = guard.to_dict()
    return {"guard_result": guard.to_dict(), "core_candidate": candidate}


def _fallback_status_from_violations(violations: list[dict[str, Any]]) -> str:
    types = {str(item.get("type") or "") for item in violations if isinstance(item, dict)}
    if types == {"empty_reply"}:
        return "fallback_empty_reply"
    if "no_facts" in types:
        return "fallback_no_facts"
    return "fallback_reply_guard_failed"


def _pending_or_confirmation(results: list[Any]) -> bool:
    return any(isinstance(item, dict) and str(item.get("status") or "") in {"pending_created", "needs_confirmation"} for item in results)


def _duplicate_only(results: list[Any]) -> bool:
    statuses = [str(item.get("status") or "") for item in results if isinstance(item, dict)]
    return bool(statuses) and all(status == "duplicate_skipped" for status in statuses)


def _allowed_numbers(results: list[Any]) -> set[float]:
    values: set[float] = set()

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)
        elif isinstance(value, (int, float)):
            values.add(float(value))

    visit(results)
    return values


def _numbers(text: str) -> set[float]:
    values = set()
    for item in re.findall(r"\d+(?:\.\d+)?", text):
        try:
            values.add(float(item))
        except ValueError:
            pass
    return values


def _has_real_write_claim(value: Any) -> bool:
    if isinstance(value, dict):
        if value.get("real_write") is True:
            return True
        return any(_has_real_write_claim(item) for item in value.values())
    if isinstance(value, list):
        return any(_has_real_write_claim(item) for item in value)
    return False

__all__ = ["reply_guard"]
