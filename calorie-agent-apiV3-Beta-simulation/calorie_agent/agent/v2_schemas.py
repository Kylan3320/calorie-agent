from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


V2_PLAN_SCHEMA_VERSION = "agent_plan.v2"

PLANNER_STATUSES = {
    "success",
    "invalid_json",
    "invalid_tool",
    "validation_failed",
    "fallback",
}

SAFETY_LEVELS = {
    "read_only",
    "write",
    "destructive",
    "permission_sensitive",
}

V2_SUPPORTED_TOOLS = {
    "record_food",
    "query_today",
    "undo_intake",
    "change_standard",
    "search_food",
    "create_food",
    "create_pending",
    "cancel_pending",
    "generate_daily_report",
    "generate_weekly_report",
    "generate_monthly_report",
    "query_trend",
    "compare_period",
    "analyze_target_gap",
    "get_report",
    "regenerate_report",
    "query_intake_food_nutrition",
    "list_reminder_rules",
    "update_reminder_rule",
    "pause_reminders",
    "resume_reminders",
    "snooze_reminder",
    "resolve_member",
    "update_member_relation",
}


class V2PlanSchemaError(ValueError):
    """Raised when a V2 planner response cannot be represented as a plan."""


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _str_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


@dataclass(frozen=True)
class AgentAction:
    action_id: str
    tool: str
    parameters: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    safety: str = "read_only"
    depends_on: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any], index: int = 0) -> "AgentAction":
        if not isinstance(payload, dict):
            raise V2PlanSchemaError(f"actions[{index}] must be a JSON object")
        tool = str(payload.get("tool") or "").strip()
        if not tool:
            raise V2PlanSchemaError(f"actions[{index}].tool is required")
        parameters = _dict(payload.get("parameters") if "parameters" in payload else payload.get("params"))
        target = _dict(payload.get("target"))
        if target and "target" not in parameters:
            parameters = {**parameters, "target": target}
        safety = str(payload.get("safety") or _infer_safety(tool)).strip()
        if safety not in SAFETY_LEVELS:
            safety = _infer_safety(tool)
        return cls(
            action_id=str(payload.get("action_id") or f"a{index + 1}"),
            tool=tool,
            parameters=parameters,
            reason=str(payload.get("reason") or ""),
            safety=safety,
            depends_on=_str_list(payload.get("depends_on")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "action_id": self.action_id,
            "tool": self.tool,
            "parameters": self.parameters,
            "reason": self.reason,
            "safety": self.safety,
            "depends_on": self.depends_on,
        }


@dataclass(frozen=True)
class AgentPlan:
    goal: str
    actions: list[AgentAction] = field(default_factory=list)
    used_skills: list[str] = field(default_factory=list)
    confidence: float | None = None
    needs_confirmation: bool = False
    confirmation_reason: str | None = None
    planner_notes: str | None = None
    schema_version: str = V2_PLAN_SCHEMA_VERSION
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AgentPlan":
        if not isinstance(payload, dict):
            raise V2PlanSchemaError("agent plan must be a JSON object")
        actions = [
            AgentAction.from_dict(_dict(action), index)
            for index, action in enumerate(_list(payload.get("actions")))
        ]
        return cls(
            goal=str(payload.get("goal") or "unknown"),
            actions=actions,
            used_skills=_str_list(payload.get("used_skills")),
            confidence=_float_or_none(payload.get("confidence")),
            needs_confirmation=bool(payload.get("needs_confirmation") or False),
            confirmation_reason=(
                str(payload.get("confirmation_reason"))
                if payload.get("confirmation_reason") is not None
                else None
            ),
            planner_notes=(
                str(payload.get("planner_notes") or payload.get("notes"))
                if payload.get("planner_notes") is not None or payload.get("notes") is not None
                else None
            ),
            schema_version=str(payload.get("schema_version") or V2_PLAN_SCHEMA_VERSION),
            raw=dict(payload),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "goal": self.goal,
            "actions": [action.to_dict() for action in self.actions],
            "used_skills": self.used_skills,
            "confidence": self.confidence,
            "needs_confirmation": self.needs_confirmation,
            "confirmation_reason": self.confirmation_reason,
            "planner_notes": self.planner_notes,
        }


@dataclass(frozen=True)
class PlannerResult:
    status: str
    plan: AgentPlan | None = None
    raw_response: str = ""
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    loaded_skill_names: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["plan"] = self.plan.to_dict() if self.plan is not None else None
        return data


def _infer_safety(tool: str) -> str:
    if tool in {
        "query_today",
        "search_food",
        "generate_daily_report",
        "generate_weekly_report",
        "generate_monthly_report",
        "query_trend",
        "compare_period",
        "analyze_target_gap",
        "get_report",
        "query_intake_food_nutrition",
        "list_reminder_rules",
    }:
        return "read_only"
    if tool == "undo_intake":
        return "destructive"
    if tool in {"resolve_member", "update_member_relation"}:
        return "permission_sensitive"
    return "write"
