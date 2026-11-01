"""Execute the persisted plan through the registry and checkpoint execution facts."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..schemas import AgentPlan, ToolResult
from ..state import AgentState
from ..storage import message_key
from ..tools import ToolContext, get_tool


def tool_executor(state: AgentState) -> dict[str, Any]:
    plan = AgentPlan.from_dict(state["plan"])
    message, store = state["message"], state["store"]
    route = state.get("pending_route") or {}
    execution = state.get("execution")
    ctx = ToolContext(store, state["config"], {**state["context"], "pending_route": route,
                      "intake_date": execution["payload"]["intake_date"] if execution else store.today_string()})
    results = list(execution["payload"].get("results", [])) if execution else []
    errors = list(state.get("errors") or [])
    task = state["context"].get("active_pending")
    for index, action in enumerate(plan.actions):
        if index < len(results):
            continue
        action_message = {**message, "message_id": f"{message_key(message)}:agent:{index + 1}"}
        try:
            if route.get("route") in {"cancel", "defer"}:
                result = ToolResult(action.tool, "pending_cancelled" if route["route"] == "cancel" else "pending_deferred")
            else:
                result = get_tool(action.tool)["runner"](action_message, action.parameters, ctx)
            result = replace(result, action_id=action.action_id)
            if result.status == "error" or result.error:
                errors.append(result.error or f"{action.tool}:error")
                results.append(result.to_dict())
                break
            results.append(result.to_dict())
            if execution:
                execution["payload"] = {**execution["payload"], "results": results}
                store.save_execution(execution, execution["payload"])
        except Exception as exc:
            errors.append(f"{action.tool}:{type(exc).__name__}:{exc}")
            if len(results) == index:
                results.append(ToolResult(action.tool, "error", error=errors[-1], action_id=action.action_id).to_dict())
            break
    if task and route.get("handled") and not errors and route["route"] != "defer":
        try:
            current = store.get_pending(task["pending_id"])
            if current and current["status"] == "pending":
                store.mark_pending_status(task, "cancelled" if route["route"] == "cancel" else "completed")
        except Exception as exc:
            errors.append(f"pending_completion_failed:{type(exc).__name__}:{exc}")
    observations = [{"action_id": r.get("action_id", ""), "tool": r["tool"], "status": r["status"]} for r in results]
    return {"tool_results": results, "observations": observations, "errors": errors, "execution": execution}


__all__ = ["tool_executor"]
