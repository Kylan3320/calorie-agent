"""Data contracts for plans, tool results, policy decisions, and guard results."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

PLAN_SCHEMA_VERSION = "agent_plan.v4"

SAFETY_LEVELS = {"read_only", "write", "destructive"}

SUPPORTED_TOOLS = {
    "record_food",
    "query_today",
    "query_food_nutrition",
    "undo_last",
    "query_knowledge",
    "query_history",
}


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
    action_id: str
    tool: str
    parameters: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    safety: str = "read_only"
    depends_on: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, payload: dict[str, Any], index: int = 0) -> "AgentAction":
        tool = str(payload.get("tool") or "").strip()
        if tool not in SUPPORTED_TOOLS:
            raise ValueError(f"unsupported tool: {tool}")
        parameters = _dict(payload.get("parameters") or payload.get("params"))
        safety = _infer_safety(tool)
        depends_on = [str(item) for item in _list(payload.get("depends_on")) if str(item)]
        return cls(
            action_id=str(payload.get("action_id") or f"a{index + 1}"),
            tool=tool,
            parameters=parameters,
            reason=str(payload.get("reason") or ""),
            safety=safety,
            depends_on=depends_on,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AgentPlan:
    goal: str
    actions: list[AgentAction] = field(default_factory=list)
    needs_confirmation: bool = False
    confidence: float | None = None
    schema_version: str = PLAN_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "AgentPlan":
        if not isinstance(payload, dict):
            raise ValueError("plan must be a JSON object")
        actions = [
            AgentAction.from_dict(_dict(action), index)
            for index, action in enumerate(_list(payload.get("actions")))
        ]
        return cls(
            goal=str(payload.get("goal") or "unknown"),
            actions=actions,
            needs_confirmation=bool(payload.get("needs_confirmation") or False),
            confidence=_float_or_none(payload.get("confidence")),
            schema_version=str(payload.get("schema_version") or PLAN_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "goal": self.goal,
            "actions": [action.to_dict() for action in self.actions],
            "needs_confirmation": self.needs_confirmation,
            "confidence": self.confidence,
        }


@dataclass(frozen=True)
class ToolResult:
    tool: str
    status: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    action_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    blocked_actions: list[dict[str, Any]] = field(default_factory=list)
    confirmation_required: bool = False
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class GuardResult:
    passed: bool
    safe_reply: str = ""
    violations: list[dict[str, Any]] = field(default_factory=list)
    fallback_used: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _infer_safety(tool: str) -> str:
    from .tools.registry import get_tool

    entry = get_tool(tool)
    if entry is None:
        raise ValueError(f"unregistered tool: {tool}")
    return entry["safety"]
