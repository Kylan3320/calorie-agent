"""Native tool results. Schemas live with the registered tool objects."""
from dataclasses import dataclass, field, asdict
from typing import Any

@dataclass(frozen=True)
class ToolResult:
    tool: str
    status: str
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    action_id: str = ""

    def to_dict(self):
        return asdict(self)
