"""Preset definitions and policy evaluation.

Presets replace the old candidate-version branches (REQUIREMENTS.md D11):
behavior differences live here and in config.yaml, never inside graph nodes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..schemas import AgentPlan, PolicyDecision
from ..tools.registry import TOOL_REGISTRY

READONLY_TOOLS = {name for name, entry in TOOL_REGISTRY.items() if entry["safety"] == "read_only"}
ALL_TOOLS = set(TOOL_REGISTRY)

FORBIDDEN_WRITE_FLAGS = {
    "delete",
    "undo",
    "hard_delete",
    "update",
    "upsert",
    "profile",
    "member_id",
    "external_source",
}

MAX_ACTIONS = 8


@dataclass(frozen=True)
class Preset:
    name: str
    allowed_tools: frozenset[str]
    store_kind: str  # "bitable" | "fake"
    send_reply: bool
    allow_write: bool


def resolve_preset(name: str, config_allowed_tools: list[str] | tuple[str, ...]) -> Preset:
    if name == "live":
        allowed = {str(item) for item in config_allowed_tools} & ALL_TOOLS
        return Preset(
            name=name,
            allowed_tools=frozenset(allowed),
            store_kind="bitable",
            send_reply=True,
            allow_write=True,
        )
    if name == "shadow_fakewrite":
        return Preset(
            name=name,
            allowed_tools=frozenset(ALL_TOOLS),
            store_kind="fake",
            send_reply=False,
            allow_write=True,
        )
    if name == "shadow_readonly":
        return Preset(
            name=name,
            allowed_tools=frozenset(READONLY_TOOLS),
            store_kind="bitable",
            send_reply=False,
            allow_write=False,
        )
    raise ValueError(f"unknown preset: {name}")


def evaluate_policy(plan: AgentPlan, preset: Preset) -> PolicyDecision:
    blocked: list[dict[str, Any]] = []
    confirmation_required = bool(plan.needs_confirmation)

    if len(plan.actions) > MAX_ACTIONS:
        blocked.append({"action_id": "", "tool": "", "safety": "", "reason": "too_many_actions"})

    for action in plan.actions:
        safety = TOOL_REGISTRY[action.tool]["safety"]
        if action.tool not in preset.allowed_tools:
            blocked.append(
                {"action_id": action.action_id, "tool": action.tool, "safety": action.safety, "reason": f"tool_not_allowed:{action.tool}"}
            )
            continue
        if not preset.allow_write and safety in {"write", "destructive"}:
            blocked.append(
                {"action_id": action.action_id, "tool": action.tool, "safety": action.safety, "reason": "write_not_allowed_in_preset"}
            )
            continue
        if safety == "destructive":
            confirmation_required = True
            continue
        if action.tool == "record_food":
            reason = _record_food_error(action)
            if reason:
                blocked.append({"action_id": action.action_id, "tool": action.tool, "safety": action.safety, "reason": reason})
        elif _has_forbidden_write_flags(action.parameters):
            blocked.append(
                {"action_id": action.action_id, "tool": action.tool, "safety": action.safety, "reason": "read_action_has_write_flags"}
            )

    allowed = not blocked
    return PolicyDecision(
        allowed=allowed,
        blocked_actions=blocked,
        confirmation_required=confirmation_required,
        reason="all_actions_allowed" if allowed else "policy_blocked",
    )


def risk_level(plan: AgentPlan, decision: PolicyDecision) -> dict[str, Any]:
    levels = [action.safety for action in plan.actions]
    blocked_safety = {str(item.get("safety") or "") for item in decision.blocked_actions}
    if "destructive" in levels or "destructive" in blocked_safety:
        level = "destructive"
    elif "write" in levels or "write" in blocked_safety:
        level = "write"
    else:
        level = "read_only"
    return {"level": level, "reasons": levels, "blocked_count": len(decision.blocked_actions)}


def _record_food_error(action: Any) -> str:
    if not str(action.parameters.get("food_query") or action.parameters.get("food_name") or "").strip():
        return "record_food_missing_food_query"
    if _has_forbidden_write_flags(action.parameters):
        return "record_food_has_forbidden_write_flags"
    return ""


def _has_forbidden_write_flags(params: dict[str, Any]) -> bool:
    return any(bool(params.get(key)) for key in FORBIDDEN_WRITE_FLAGS)
