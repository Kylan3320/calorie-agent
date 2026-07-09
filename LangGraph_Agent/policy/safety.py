from __future__ import annotations

from ..schemas import AgentAction


def destructive_block_for(action: AgentAction) -> dict[str, object]:
    return {
        "action_id": action.action_id,
        "tool": action.tool,
        "safety": action.safety,
        "reason": "destructive_blocked_in_v0",
    }
