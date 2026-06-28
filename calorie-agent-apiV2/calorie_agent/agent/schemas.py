from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


PLAN_SCHEMA_VERSION = "agent_plan.v1"

SUPPORTED_AGENT_TOOLS = {
    "record_food",
    "query_today",
    "undo_intake",
    "change_standard",
    "search_food",
    "create_food",
    "create_pending",
    "cancel_pending",
    "clear_daily_intake",
    "search_memory",
    "suggest_default_grams",
    "learn_alias",
    "confirm_alias",
    "create_combo",
    "use_combo",
    "list_memories",
    "delete_memory",
    "update_preference",
    "generate_daily_report",
    "generate_weekly_report",
    "generate_monthly_report",
    "query_trend",
    "compare_period",
    "analyze_target_gap",
    "get_report",
    "regenerate_report",
    "list_reminder_rules",
    "update_reminder_rule",
    "pause_reminders",
    "resume_reminders",
    "snooze_reminder",
    "generate_daily_summary_reminder",
    "detect_anomalies",
    "send_reminder",
}


class PlanValidationError(ValueError):
    """Raised when a model response cannot be used as an agent plan."""


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class AgentAction:
    tool: str
    params: dict[str, Any] = field(default_factory=dict)
    target: dict[str, Any] = field(default_factory=dict)
    action_id: str = ""
    reason: str = ""
    confidence: float | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any], index: int = 0) -> "AgentAction":
        tool = str(payload.get("tool") or "").strip()
        if not tool:
            raise PlanValidationError(f"actions[{index}].tool is required")
        if tool not in SUPPORTED_AGENT_TOOLS:
            raise PlanValidationError(f"unsupported tool: {tool}")
        return cls(
            tool=tool,
            params=_dict(payload.get("params")),
            target=_dict(payload.get("target")),
            action_id=str(payload.get("action_id") or f"action_{index}"),
            reason=str(payload.get("reason") or ""),
            confidence=_float_or_none(payload.get("confidence")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "params": self.params,
            "target": self.target,
            "action_id": self.action_id,
            "reason": self.reason,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class AgentPlan:
    goal: str
    actions: list[AgentAction] = field(default_factory=list)
    schema_version: str = PLAN_SCHEMA_VERSION
    reply_style: str = "compact"
    needs_confirmation: bool = False
    confidence: float | None = None
    notes: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AgentPlan":
        if not isinstance(payload, dict):
            raise PlanValidationError("agent plan must be a JSON object")
        actions = [
            AgentAction.from_dict(_dict(action), index)
            for index, action in enumerate(_list(payload.get("actions")))
        ]
        return cls(
            goal=str(payload.get("goal") or "unknown"),
            actions=actions,
            schema_version=str(payload.get("schema_version") or PLAN_SCHEMA_VERSION),
            reply_style=str(payload.get("reply_style") or payload.get("final_response_style") or "compact"),
            needs_confirmation=bool(payload.get("needs_confirmation") or False),
            confidence=_float_or_none(payload.get("confidence")),
            notes=str(payload.get("notes") or ""),
            raw=dict(payload),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "goal": self.goal,
            "actions": [action.to_dict() for action in self.actions],
            "reply_style": self.reply_style,
            "needs_confirmation": self.needs_confirmation,
            "confidence": self.confidence,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class ToolResult:
    tool: str
    status: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    action_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "status": self.status,
            "data": self.data,
            "error": self.error,
            "action_id": self.action_id,
        }
