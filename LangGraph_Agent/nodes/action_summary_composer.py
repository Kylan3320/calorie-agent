from __future__ import annotations

from typing import Any


def action_summary_composer(state: dict[str, Any]) -> dict[str, str]:
    plan = state.get("plan") if isinstance(state.get("plan"), dict) else {}
    actions = plan.get("actions") if isinstance(plan.get("actions"), list) else []
    tool_results = state.get("tool_results") if isinstance(state.get("tool_results"), list) else []
    blocked = state.get("policy_decision") if isinstance(state.get("policy_decision"), dict) else {}
    blocked_actions = blocked.get("blocked_actions") if isinstance(blocked.get("blocked_actions"), list) else []

    if blocked_actions:
        summary = f"blocked {len(blocked_actions)} action(s)"
    elif tool_results:
        summary = f"executed {len(tool_results)} action(s)"
    elif actions:
        summary = f"planned {len(actions)} action(s)"
    else:
        summary = "no actions"
    return {"action_summary": summary}


__all__ = ["action_summary_composer"]
