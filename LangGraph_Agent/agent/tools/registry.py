"""Tool registry: the single whitelist mapping tool -> runner + safety."""

from __future__ import annotations

from typing import Any, Callable

from ..schemas import ToolResult
from .base import ToolContext
from .reads import run_query_food_nutrition, run_query_history, run_query_knowledge, run_query_today
from .writes import run_record_food, run_undo_last

Runner = Callable[[dict[str, str], dict[str, Any], ToolContext], ToolResult]

TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "record_food": {"safety": "write", "runner": run_record_food},
    "query_today": {"safety": "read_only", "runner": run_query_today},
    "query_food_nutrition": {"safety": "read_only", "runner": run_query_food_nutrition},
    "undo_last": {"safety": "destructive", "runner": run_undo_last},
    "query_knowledge": {"safety": "read_only", "runner": run_query_knowledge},
    "query_history": {"safety": "read_only", "runner": run_query_history},
}


def get_tool(tool_name: str) -> dict[str, Any] | None:
    entry = TOOL_REGISTRY.get(tool_name)
    return dict(entry) if entry else None


__all__ = ["TOOL_REGISTRY", "Runner", "ToolContext", "get_tool"]
