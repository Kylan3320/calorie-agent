"""Tool executor node: run whitelisted actions via the registry.

Per-action idempotency: each action gets message_id suffix ":agent:{n}" so a
retried message never double-writes (V2 convention, contracts/bitable_schema.md).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..schemas import AgentPlan, ToolResult
from ..state import AgentState
from ..tools import ToolContext, get_tool


def tool_executor(state: AgentState) -> dict[str, Any]:
    plan = AgentPlan.from_dict(state.get("plan") or {})
    decision = state.get("policy_decision") or {}
    message = state.get("message") or {}
    store = state.get("store")
    ctx = ToolContext(store=store, config=state.get("config"), context=state.get("context") or {})
    results: list[dict[str, Any]] = []

    destructive = [action for action in plan.actions if action.safety == "destructive"]
    if decision.get("confirmation_required") and destructive:
        action = destructive[0]
        pending = store.create_pending(message, "confirm_undo", "undo_last", None, "unknown")
        results.append(
            ToolResult(
                action.tool,
                "needs_confirmation_plan",
                {
                    "undo_result": {"status": "needs_confirmation"},
                    "pending_result": {"status": pending.get("status"), "intent": "confirm_undo"},
                },
                action_id=action.action_id,
            ).to_dict()
        )
    else:
        for index, action in enumerate(plan.actions):
            entry = get_tool(action.tool)
            if not entry:
                results.append(
                    ToolResult(action.tool, "error", error=f"tool_not_registered:{action.tool}", action_id=action.action_id).to_dict()
                )
                continue
            action_message = {**message, "message_id": f"{message.get('message_id', '')}:agent:{index + 1}"}
            try:
                result = entry["runner"](action_message, dict(action.parameters), ctx)
                result = replace(result, action_id=action.action_id)
            except Exception as exc:  # noqa: BLE001 - tool failure becomes a fact, not a crash
                result = ToolResult(action.tool, "error", error=f"{type(exc).__name__}:{exc}", action_id=action.action_id)
            results.append(result.to_dict())

    observations = [
        {"action_id": item.get("action_id", ""), "tool": item.get("tool", ""), "status": item.get("status", "")}
        for item in results
    ]
    print(f"[executor] statuses={[(o['tool'], o['status']) for o in observations]}")
    return {"tool_results": results, "observations": observations}


__all__ = ["tool_executor"]
