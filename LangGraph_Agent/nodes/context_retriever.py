"""Context retriever node."""

from __future__ import annotations

from typing import Any

from ..rag import retrieve_all
from ..state import LangGraphAgentState


def context_retriever(state: LangGraphAgentState) -> dict[str, Any]:
    retrieved = retrieve_all(str(state.get("normalized_text") or ""))
    return {"retrieved_context": retrieved}

__all__ = ["context_retriever"]
