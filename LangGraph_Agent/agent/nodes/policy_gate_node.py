"""Policy gate node: preset-driven allow/block/confirm decision."""

from __future__ import annotations

from typing import Any
from dataclasses import replace

from ..policy import evaluate_policy, risk_level
from ..schemas import AgentPlan
from ..state import AgentState
from ..storage.timeutil import now_millis


def policy_gate(state: AgentState) -> dict[str, Any]:
    plan = AgentPlan.from_dict(state.get("plan") or {})
    preset = state.get("preset")
    context = state["context"]
    route = state.get("pending_route") or {}
    execution = state.get("execution")
    if preset.allow_write:
        execution = execution or state["store"].begin_execution(state["message"], {
            "message": state["message"], "plan": plan.to_dict(), "pending_route": route,
            "active_pending": context.get("active_pending"), "intake_date": state.get("intake_date") or state["store"].today_string(), "results": []})
        saved = execution["payload"]
        plan = AgentPlan.from_dict(saved["plan"])
        context = {"active_pending": saved.get("active_pending")}
        route = saved["pending_route"]
    decision = evaluate_policy(plan, preset)
    task = context.get("active_pending")
    if task and task["intent"] == "confirm_undo" and route.get("handled"):
        decision = replace(decision, confirmation_required=False)
    if task and route.get("handled") and (not execution or len(execution["payload"]["results"]) < len(plan.actions)):
        current = state["store"].get_pending(task["pending_id"])
        if (not current or current["status"] != "pending"
                or (current["expires_at"] and current["expires_at"] <= now_millis())):
            decision = replace(decision, allowed=False, reason="pending_no_longer_active",
                               blocked_actions=[{"reason": "pending_no_longer_active"}])
    return {
        "plan": plan.to_dict(), "context": context, "pending_route": route, "execution": execution,
        "policy_decision": decision.to_dict(),
        "risk": risk_level(plan, decision),
    }


def route_after_policy(state: AgentState) -> str:
    decision = state.get("policy_decision") or {}
    return "executor" if decision.get("allowed") else "composer"


__all__ = ["policy_gate", "route_after_policy"]
