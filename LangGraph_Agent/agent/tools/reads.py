"""Knowledge retrieval tool; structured ledger queries live in registry."""
from ..schemas import ToolResult
from ..storage.knowledge_retriever import build_knowledge_retriever
from .base import ToolContext
from typing import Any

def run_query_knowledge(message: dict[str, str], params: dict[str, Any], ctx: ToolContext) -> ToolResult:
    """Dietary knowledge Q&A via the knowledge_retriever seam (contracts/knowledge_schema.md).

    Retrieved source evidence is returned for grounded synthesis;
    evidence travels in data so reply_guard's number tracing covers knowledge text.
    """
    query = str(params.get("query") or message.get("text") or "").strip()
    if not query:
        return ToolResult("query_knowledge", "not_found", {"status": "not_found", "entries": [], "evidence": "", "query": ""})
    kind = str(getattr(ctx.config, "knowledge_retriever", "") or "faq")
    try:
        retriever = build_knowledge_retriever(kind)
        entries = ctx.store.load_knowledge() if kind == "faq" else []
        result = retriever(query, entries)
    except ValueError as exc:
        return ToolResult("query_knowledge", "error", error=f"knowledge_retriever_config:{exc}")
    for index, entry in enumerate(result.get("entries", []), 1):
        entry["source_id"] = f"K{index}"
        if not entry.get("source"):
            import re
            match = re.search(r"来源[：:]\s*(.+)", entry.get("answer", ""))
            entry["source"] = match[1].strip() if match else "现有知识资料"
    result = {**result, "query": query, "retriever": kind}
    return ToolResult("query_knowledge", str(result.get("status") or "not_found"), result)

