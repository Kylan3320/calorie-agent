"""Input normalizer node."""

from __future__ import annotations

import re
from typing import Any

from ..state import LangGraphAgentState


def input_normalizer(state: LangGraphAgentState) -> dict[str, Any]:
    text = str(state.get("raw_text") or "").strip()
    normalized = re.sub(r"\s+", " ", text)
    return {"normalized_text": normalized}

__all__ = ["input_normalizer"]
