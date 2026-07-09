"""Observation repair node wrapper."""

from typing import Any

from ..state import LangGraphAgentState


def observation_repair(state: LangGraphAgentState) -> dict[str, Any]:
    return {}

__all__ = ["observation_repair"]
