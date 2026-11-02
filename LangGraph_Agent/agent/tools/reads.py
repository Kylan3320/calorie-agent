"""Knowledge retrieval tool; structured ledger queries live in registry."""
from ..storage.knowledge_retriever import build_knowledge_retriever

def run_query_knowledge(rt, params):
    """Dietary knowledge Q&A via the knowledge_retriever seam (contracts/knowledge_schema.md).

    Retrieved source evidence and actual sources are returned for synthesis.
    """
    query = str(params.get("query") or rt.message.get("text") or "").strip()
    if not query:
        return {"status": "not_found", "data": {"status": "not_found", "entries": [], "evidence": "", "query": ""}}
    kind = rt.config.knowledge_retriever
    try:
        retriever = build_knowledge_retriever(kind)
        entries = rt.store.load_knowledge() if kind == "faq" else []
        result = retriever(query, entries)
    except ValueError as exc:
        return {"status": "error", "data": {}, "error": f"knowledge_retriever_config:{exc}"}
    for entry in result.get("entries", []):
        if not entry.get("source"):
            import re
            match = re.search(r"来源[：:]\s*(.+)", entry.get("answer", ""))
            entry["source"] = match[1].strip() if match else "现有知识资料"
    result = {**result, "query": query, "retriever": kind}
    return {"status": str(result.get("status") or "not_found"), "data": result}

