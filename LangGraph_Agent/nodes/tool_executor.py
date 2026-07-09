"""Tool executor node wrappers."""

from typing import Any

from ... import legacy_app
from ..schemas import AgentPlan
from ..state import LangGraphAgentState
from ..tools import execute_fake_tool, execute_readonly_tool
from ..tools.read_tools import READONLY_ALLOWED_TOOLS
from ..tools.write_tools import FakeWriteLegacy, WRITE_ALLOWED_TOOLS, execute_fake_write_tool


def fake_tool_executor(state: LangGraphAgentState) -> dict[str, Any]:
    if state.get("mode") == "core_candidate":
        return _core_tool_executor(state)
    if state.get("mode") == "readonly_candidate":
        return _readonly_tool_executor(state)
    if state.get("mode") == "write_candidate":
        return _write_tool_executor(state)

    plan = AgentPlan.from_dict(state.get("plan", {}))
    decision = state.get("policy_decision", {})
    results = [
        execute_fake_tool(action, decision).to_dict()
        for action in plan.actions
    ]
    observations = [
        {"action_id": item.get("action_id", ""), "tool": item.get("tool", ""), "status": item.get("status", "")}
        for item in results
    ]
    return {"tool_results": results, "observations": observations}


def _core_tool_executor(state: LangGraphAgentState) -> dict[str, Any]:
    path = str(state.get("core_path") or "")
    candidate = dict(state.get("core_candidate") or {})
    if path == "unsupported":
        return {"tool_results": [], "observations": [], "core_source_result": {}, "final_reply": "", "action_summary": ""}

    source = _write_tool_executor(state) if path == "write" else _readonly_tool_executor(state)
    source_candidate_key = "write_candidate" if path == "write" else "readonly_candidate"
    source_candidate = source.get(source_candidate_key) if isinstance(source.get(source_candidate_key), dict) else {}
    status = str(source_candidate.get("status") or "fallback_missing_candidate")
    candidate = {
        **candidate,
        "status": status,
        "path": path,
        "selected_reply_source": "langgraph_core_candidate" if status == "ok" else "current_runtime",
        "fallback_used": status != "ok",
        "blocked_reason": str(source_candidate.get("blocked_reason") or ""),
        "source_candidate": "write_candidate" if path == "write" else "readonly_candidate",
        "real_write": False,
    }
    for key in ("write_count", "pending_count", "duplicate_count"):
        if key in source_candidate:
            candidate[key] = source_candidate[key]
    source_result = {
        **source,
        "final_reply": "",
        "action_summary": "",
        "guard_result": {},
    }
    return {
        "tool_results": list(source.get("tool_results", [])),
        "observations": list(source.get("observations", [])),
        "core_source_result": source_result,
        "core_candidate": candidate,
    }


def _write_tool_executor(state: LangGraphAgentState) -> dict[str, Any]:
    plan = AgentPlan.from_dict(state.get("plan", {}))
    policy = state.get("policy_decision", {})
    blocked = policy.get("blocked_actions") if isinstance(policy, dict) else []
    allowed_tools = {str(item) for item in state.get("write_allowed_tools", []) if str(item)}
    message = state.get("message") if isinstance(state.get("message"), dict) else {}
    legacy = state.get("write_legacy")
    if not isinstance(legacy, FakeWriteLegacy):
        legacy = FakeWriteLegacy()

    if not plan.actions:
        return _write_candidate_update("fallback_no_actions", [], "no_actions", allowed_tools=allowed_tools)
    if blocked:
        return _write_candidate_update(_blocked_status(blocked), [], _blocked_reason(blocked), allowed_tools=allowed_tools)

    results = [
        execute_fake_write_tool(
            action,
            message,
            legacy=legacy,
            allowed_tools=allowed_tools,
            fixture_mode=str(state.get("fixture_mode") or ""),
        ).to_dict()
        for action in plan.actions
    ]
    observations = [
        {"action_id": item.get("action_id", ""), "tool": item.get("tool", ""), "status": item.get("status", "")}
        for item in results
    ]
    status = _write_candidate_status(results)
    return {
        "tool_results": results,
        "observations": observations,
        "write_candidate": {
            "status": status,
            "selected_reply_source": "langgraph_write_candidate" if status == "ok" else "current_runtime",
            "allowed_tools": sorted(allowed_tools),
            "blocked_reason": "" if status == "ok" else _result_blocked_reason(results),
            "fallback_used": status != "ok",
            "write_count": _write_count(results),
            "pending_count": _status_count(results, {"pending_created"}),
            "duplicate_count": _status_count(results, {"duplicate_skipped"}),
        },
    }


def _readonly_tool_executor(state: LangGraphAgentState) -> dict[str, Any]:
    plan = AgentPlan.from_dict(state.get("plan", {}))
    policy = state.get("policy_decision", {})
    blocked = policy.get("blocked_actions") if isinstance(policy, dict) else []
    allowed_tools = {str(item) for item in state.get("readonly_allowed_tools", []) if str(item)}
    message = state.get("message") if isinstance(state.get("message"), dict) else {}
    legacy = state.get("readonly_legacy") or legacy_app

    if not plan.actions:
        return _candidate_update("fallback_no_actions", [], "no_actions", allowed_tools=allowed_tools)
    if blocked:
        return _candidate_update("fallback_policy_blocked", [], "policy_blocked", allowed_tools=allowed_tools)

    tools = [action.tool for action in plan.actions]
    disallowed = [tool for tool in tools if tool not in allowed_tools]
    if disallowed:
        return _candidate_update("fallback_tool_not_allowed", [], f"tool_not_allowed:{','.join(disallowed)}", allowed_tools=allowed_tools)

    results = [
        execute_readonly_tool(action, message, legacy=legacy, allowed_tools=allowed_tools).to_dict()
        for action in plan.actions
    ]
    observations = [
        {"action_id": item.get("action_id", ""), "tool": item.get("tool", ""), "status": item.get("status", "")}
        for item in results
    ]
    errors = [str(item.get("error") or "") for item in results if str(item.get("status") or "") == "error" or item.get("error")]
    if errors:
        status = "fallback_tool_error"
        reason = ";".join(error for error in errors if error)
    else:
        no_fact_results = [item for item in results if not _has_candidate_facts(item)]
        status = "ok" if not no_fact_results else "fallback_no_facts"
        reason = "" if not no_fact_results else ";".join(_no_fact_reason(item) for item in no_fact_results)
    return {
        "tool_results": results,
        "observations": observations,
        "readonly_candidate": {
            "status": status,
            "selected_reply_source": "langgraph_readonly_candidate" if status == "ok" else "current_runtime",
            "allowed_tools": sorted(allowed_tools),
            "blocked_reason": reason,
            "fallback_used": status != "ok",
        },
    }


def _write_candidate_update(status: str, results: list[dict[str, Any]], reason: str, *, allowed_tools: set[str]) -> dict[str, Any]:
    return {
        "tool_results": results,
        "observations": [],
        "write_candidate": {
            "status": status,
            "selected_reply_source": "current_runtime",
            "allowed_tools": sorted(allowed_tools or WRITE_ALLOWED_TOOLS),
            "blocked_reason": reason,
            "fallback_used": True,
            "write_count": 0,
            "pending_count": 0,
            "duplicate_count": 0,
        },
    }


def _write_candidate_status(results: list[dict[str, Any]]) -> str:
    statuses = {str(item.get("status") or "") for item in results if isinstance(item, dict)}
    if "error" in statuses:
        return "fallback_tool_error"
    if "needs_confirmation" in statuses:
        return "fallback_needs_confirmation"
    if "pending_created" in statuses:
        return "fallback_pending"
    if all(status in {"ok", "duplicate_skipped"} for status in statuses):
        return "ok"
    return "fallback_no_facts"


def _blocked_status(blocked: list[Any]) -> str:
    reasons = {str(item.get("reason") or "") for item in blocked if isinstance(item, dict)}
    if any("destructive" in reason for reason in reasons):
        return "fallback_destructive_blocked"
    if any("family_member" in reason or "permission" in reason for reason in reasons):
        return "fallback_permission_sensitive_blocked"
    if any("mixed_scope" in reason for reason in reasons):
        return "fallback_mixed_scope_blocked"
    return "fallback_policy_blocked"


def _blocked_reason(blocked: list[Any]) -> str:
    return ";".join(str(item.get("reason") or "") for item in blocked if isinstance(item, dict))


def _result_blocked_reason(results: list[dict[str, Any]]) -> str:
    reasons = []
    for item in results:
        if str(item.get("status") or "") == "ok":
            continue
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        pending = data.get("pending_result") if isinstance(data.get("pending_result"), dict) else {}
        reasons.append(str(item.get("error") or pending.get("reason") or item.get("status") or ""))
    return ";".join(reason for reason in reasons if reason)


def _write_count(results: list[dict[str, Any]]) -> int:
    count = 0
    for item in results:
        data = item.get("data") if isinstance(item.get("data"), dict) else {}
        write = data.get("intake_write_result") if isinstance(data.get("intake_write_result"), dict) else {}
        try:
            count += int(write.get("created_count") or 0)
        except (TypeError, ValueError):
            pass
    return count


def _status_count(results: list[dict[str, Any]], statuses: set[str]) -> int:
    return len([item for item in results if str(item.get("status") or "") in statuses])


def _candidate_update(
    status: str,
    results: list[dict[str, Any]],
    reason: str,
    *,
    allowed_tools: set[str] | None = None,
) -> dict[str, Any]:
    return {
        "tool_results": results,
        "observations": [],
        "readonly_candidate": {
            "status": status,
            "selected_reply_source": "current_runtime",
            "allowed_tools": sorted(allowed_tools or READONLY_ALLOWED_TOOLS),
            "blocked_reason": reason,
            "fallback_used": True,
        },
    }


def _has_candidate_facts(result: dict[str, Any]) -> bool:
    if str(result.get("status") or "") != "ok":
        return False
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    tool = str(result.get("tool") or "")
    if tool == "query_today":
        return isinstance(data.get("today_summary"), dict)
    if tool == "query_food_nutrition":
        mode = str(data.get("mode") or "")
        payload = data.get("result") if isinstance(data.get("result"), dict) else {}
        if mode == "stored_intake":
            return isinstance(payload.get("total"), dict)
        if mode == "food_database":
            return isinstance(payload.get("nutrition"), dict)
        return False
    if tool == "generate_daily_report":
        return isinstance(data.get("facts"), dict)
    return False


def _no_fact_reason(result: dict[str, Any]) -> str:
    tool = str(result.get("tool") or "")
    status = str(result.get("status") or "")
    return f"no_facts:{tool}:{status}"


__all__ = ["fake_tool_executor"]
