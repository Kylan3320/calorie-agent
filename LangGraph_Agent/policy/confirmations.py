from __future__ import annotations

from ..schemas import AgentPlan


def confirmation_required_for(plan: AgentPlan) -> bool:
    return bool(plan.needs_confirmation) or any(action.safety == "destructive" for action in plan.actions)
