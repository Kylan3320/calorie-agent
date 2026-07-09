from __future__ import annotations

from ..schemas import AgentAction


def permission_block_for(action: AgentAction) -> dict[str, object]:
    return {
        "action_id": action.action_id,
        "tool": action.tool,
        "safety": action.safety,
        "reason": "write_blocked_in_v0_shadow",
    }
