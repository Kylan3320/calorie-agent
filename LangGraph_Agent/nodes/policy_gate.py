"""Policy gate node wrapper."""

from typing import Any

from ..policy import evaluate_policy
from ..schemas import AgentPlan
from ..state import LangGraphAgentState


def policy_gate(state: LangGraphAgentState) -> dict[str, Any]:
    if state.get("mode") == "core_candidate":
        return _core_policy_gate(state)
    if state.get("mode") == "write_candidate":
        return _write_policy_gate(state)

    plan = AgentPlan.from_dict(state.get("plan", {}))
    decision = evaluate_policy(plan, mode=str(state.get("mode") or "shadow"))
    risk = _risk_from_plan(plan)
    return {"policy_decision": decision.to_dict(), "risk": risk}


def _risk_from_plan(plan: AgentPlan) -> dict[str, Any]:
    levels = [action.safety for action in plan.actions]
    if "destructive" in levels:
        level = "destructive"
    elif "permission_sensitive" in levels:
        level = "permission_sensitive"
    elif "write" in levels:
        level = "write"
    else:
        level = "read_only"
    return {"level": level, "reasons": levels}


def _write_policy_gate(state: LangGraphAgentState) -> dict[str, Any]:
    plan = AgentPlan.from_dict(state.get("plan", {}))
    text = str(state.get("normalized_text") or state.get("raw_text") or "")
    allowed_tools = {str(item) for item in state.get("write_allowed_tools", []) if str(item)}
    blocked: list[dict[str, Any]] = []
    confirmation_required = bool(plan.needs_confirmation)

    if any(token in text for token in ("濡堝", "鐖哥埜", "瀛╁瓙", "瀹朵汉", "妈妈", "爸爸", "孩子", "家人")):
        blocked.append(_block("message", "permission_sensitive", "family_member_blocked"))
    if any(token in text for token in ("淇敼", "鐩爣", "鏍囧噯", "修改", "目标", "标准")):
        blocked.append(_block("message", "write", "mixed_scope_blocked"))
    if any(token in text for token in ("鍒犻櫎", "鎾ら攢", "娓呯┖", "删除", "撤销", "清空")):
        blocked.append(_block("message", "destructive", "destructive_blocked"))

    for action in plan.actions:
        if action.tool not in allowed_tools:
            blocked.append(_block(action.action_id, action.safety, f"tool_not_allowed:{action.tool}", tool=action.tool))
            continue
        if action.safety == "destructive":
            blocked.append(_block(action.action_id, action.safety, "destructive_blocked", tool=action.tool))
            confirmation_required = True
            continue
        if action.tool == "record_food":
            reason = _record_food_policy_error(action)
            if reason:
                blocked.append(_block(action.action_id, action.safety, reason, tool=action.tool))
        elif _has_write_flags(action.parameters):
            blocked.append(_block(action.action_id, action.safety, "read_action_has_write_flags", tool=action.tool))

    allowed = not blocked
    decision = {
        "allowed": allowed,
        "blocked_actions": blocked,
        "confirmation_required": confirmation_required,
        "reason": "all_actions_allowed" if allowed else "v3_write_policy_blocked",
    }
    return {"policy_decision": decision, "risk": _write_risk_from_plan(plan, blocked)}


def _record_food_policy_error(action: Any) -> str:
    params = action.parameters if hasattr(action, "parameters") else {}
    if not str(params.get("food_query") or params.get("food_name") or "").strip():
        return "record_food_missing_food_query"
    if _has_write_flags(params):
        return "record_food_has_forbidden_write_flags"
    return ""


def _block(action_id: str, safety: str, reason: str, *, tool: str = "") -> dict[str, Any]:
    return {"action_id": action_id, "tool": tool, "safety": safety, "reason": reason}


def _write_risk_from_plan(plan: AgentPlan, blocked: list[dict[str, Any]]) -> dict[str, Any]:
    levels = [action.safety for action in plan.actions]
    if any(str(item.get("safety") or "") == "destructive" for item in blocked) or "destructive" in levels:
        level = "destructive"
    elif any(str(item.get("safety") or "") == "permission_sensitive" for item in blocked) or "permission_sensitive" in levels:
        level = "permission_sensitive"
    elif "write" in levels:
        level = "write"
    else:
        level = "read_only"
    return {"level": level, "reasons": levels, "blocked_count": len(blocked)}


def _has_write_flags(params: dict[str, Any]) -> bool:
    forbidden = {"delete", "undo", "hard_delete", "update", "upsert", "profile", "member_id", "family_member", "external_source"}
    return any(bool(params.get(key)) for key in forbidden)


CORE_READONLY_TOOLS = {"query_today", "query_food_nutrition", "generate_daily_report"}
CORE_WRITE_TOOLS = {"record_food", "query_today", "query_food_nutrition"}


def _core_policy_gate(state: LangGraphAgentState) -> dict[str, Any]:
    plan = AgentPlan.from_dict(state.get("plan", {}))
    text = str(state.get("normalized_text") or state.get("raw_text") or "")
    blocked_reason = _core_unsupported_reason(plan, text)
    if blocked_reason:
        return {
            "policy_decision": {
                "allowed": False,
                "blocked_actions": [_block("message", "permission_sensitive", blocked_reason)],
                "confirmation_required": False,
                "reason": "v42_core_scope_blocked",
            },
            "risk": _risk_from_plan(plan),
            "core_path": "unsupported",
            "core_candidate": _core_candidate("fallback_unsupported_scope", "unsupported", blocked_reason, fallback_used=True),
        }
    if not plan.actions:
        return {
            "policy_decision": {
                "allowed": False,
                "blocked_actions": [_block("message", "read_only", "no_actions")],
                "confirmation_required": False,
                "reason": "v42_core_no_actions",
            },
            "risk": _risk_from_plan(plan),
            "core_path": "unsupported",
            "core_candidate": _core_candidate("fallback_no_actions", "unsupported", "no_actions", fallback_used=True),
        }

    tools = {action.tool for action in plan.actions}
    if "record_food" in tools:
        path = "write"
    elif tools <= CORE_READONLY_TOOLS:
        path = "read_only"
    else:
        reason = f"tool_not_supported:{','.join(sorted(tools))}"
        return {
            "policy_decision": {
                "allowed": False,
                "blocked_actions": [_block("message", "read_only", reason)],
                "confirmation_required": False,
                "reason": "v42_core_scope_blocked",
            },
            "risk": _risk_from_plan(plan),
            "core_path": "unsupported",
            "core_candidate": _core_candidate("fallback_unsupported_scope", "unsupported", reason, fallback_used=True),
        }
    return {
        "policy_decision": {"allowed": True, "blocked_actions": [], "confirmation_required": False, "reason": "v42_core_routed"},
        "risk": _risk_from_plan(plan),
        "core_path": path,
        "core_candidate": _core_candidate("routed", path, ""),
    }


def _core_unsupported_reason(plan: AgentPlan, text: str) -> str:
    if any(action.tool == "undo_intake" for action in plan.actions):
        return "destructive_scope_not_supported"
    if any(action.safety in {"destructive", "permission_sensitive"} for action in plan.actions):
        return "risk_scope_not_supported"
    if any(action.tool == "record_food" for action in plan.actions):
        tools = {action.tool for action in plan.actions}
        disallowed = sorted(tools - CORE_WRITE_TOOLS)
        if disallowed:
            return f"write_path_tool_not_supported:{','.join(disallowed)}"
    lower_text = text.lower()
    if any(token in lower_text for token in ("family", "member", "mother", "father", "child")):
        return "family_member_scope_not_supported"
    if any(token in text for token in ("妈妈", "爸爸", "孩子", "家人")):
        return "family_member_scope_not_supported"
    return ""


def _core_candidate(status: str, path: str, blocked_reason: str, *, fallback_used: bool = False) -> dict[str, Any]:
    return {
        "version": "v4.2",
        "status": status,
        "path": path,
        "selected_reply_source": "current_runtime" if fallback_used else "",
        "fallback_used": fallback_used,
        "blocked_reason": blocked_reason,
        "allowed_readonly_tools": sorted(CORE_READONLY_TOOLS),
        "allowed_write_tools": sorted(CORE_WRITE_TOOLS),
        "real_write": False,
    }


__all__ = ["policy_gate"]
