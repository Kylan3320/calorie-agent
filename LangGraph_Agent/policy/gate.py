from __future__ import annotations

from ..schemas import AgentPlan, PolicyDecision
from .confirmations import confirmation_required_for
from .permissions import permission_block_for
from .safety import destructive_block_for


def evaluate_policy(plan: AgentPlan, *, mode: str = "shadow") -> PolicyDecision:
    blocked: list[dict[str, object]] = []
    confirmation_required = confirmation_required_for(plan)

    for action in plan.actions:
        if action.safety == "destructive":
            blocked.append(destructive_block_for(action))
            continue
        if action.safety in {"write", "permission_sensitive"}:
            blocked.append(permission_block_for(action))

    reason = "all_actions_allowed" if not blocked else "v0_shadow_blocks_non_read_actions"
    if mode != "shadow":
        reason = f"{reason}; mode={mode} not fully implemented in V0"
    return PolicyDecision(
        allowed=not blocked,
        blocked_actions=blocked,
        confirmation_required=confirmation_required,
        reason=reason,
    )
