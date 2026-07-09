"""Final home for LangGraph tool registry metadata."""

from __future__ import annotations

from typing import Any

from .read_tools import execute_readonly_tool
from .write_tools import execute_fake_write_tool


READONLY_TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "query_today": {
        "name": "query_today",
        "safety": "read_only",
        "executor": execute_readonly_tool,
        "classification": "read_only",
        "source": "legacy_readonly",
        "eval": "core_readonly",
    },
    "query_food_nutrition": {
        "name": "query_food_nutrition",
        "safety": "read_only",
        "executor": execute_readonly_tool,
        "classification": "read_only",
        "source": "legacy_intake_or_food_database",
        "eval": "core_readonly",
    },
    "generate_daily_report": {
        "name": "generate_daily_report",
        "safety": "read_only",
        "executor": execute_readonly_tool,
        "classification": "read_only",
        "source": "reporting_readonly",
        "eval": "core_readonly",
    },
}


WRITE_TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    "record_food": {
        "name": "record_food",
        "safety": "write",
        "executor": execute_fake_write_tool,
        "classification": "fake_write",
        "source": "fake_write_adapter",
        "eval": "core_write",
    },
}


TOOL_REGISTRY: dict[str, dict[str, Any]] = {
    **READONLY_TOOL_REGISTRY,
    **WRITE_TOOL_REGISTRY,
}


def get_readonly_tool_metadata(tool_name: str) -> dict[str, Any] | None:
    metadata = READONLY_TOOL_REGISTRY.get(tool_name)
    return dict(metadata) if metadata is not None else None


def get_write_tool_metadata(tool_name: str) -> dict[str, Any] | None:
    metadata = WRITE_TOOL_REGISTRY.get(tool_name)
    return dict(metadata) if metadata is not None else None


def get_tool_metadata(tool_name: str) -> dict[str, Any] | None:
    metadata = TOOL_REGISTRY.get(tool_name)
    return dict(metadata) if metadata is not None else None


__all__ = [
    "READONLY_TOOL_REGISTRY",
    "TOOL_REGISTRY",
    "WRITE_TOOL_REGISTRY",
    "get_readonly_tool_metadata",
    "get_tool_metadata",
    "get_write_tool_metadata",
]
