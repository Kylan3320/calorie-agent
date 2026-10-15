"""Policy gate node: preset-driven allow/block/confirm decision."""

from __future__ import annotations

from typing import Any

from ..policy import evaluate_policy, risk_level
from ..schemas import AgentPlan
from ..state import AgentState


def policy_gate(state: AgentState) -> dict[str, Any]:
    plan = AgentPlan.from_dict(state.get("plan") or {})
    preset = state.get("preset")
    decision = evaluate_policy(plan, preset)
    return {
        "policy_decision": decision.to_dict(),
        "risk": risk_level(plan, decision),
    }


def route_after_policy(state: AgentState) -> str:
    decision = state.get("policy_decision") or {}
    return "executor" if decision.get("allowed") else "composer"


__all__ = ["policy_gate", "route_after_policy"]
