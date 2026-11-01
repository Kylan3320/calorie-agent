"""Accept only the deterministic rendering of execution facts and quoted evidence."""

from __future__ import annotations

from typing import Any

from ..state import AgentState
from .reply_composer_node import reply_composer

_SAFE_FALLBACK = "这次结果暂时无法确认，请查询记录核对。"


def reply_guard(state: AgentState) -> dict[str, Any]:
    violations = []
    if state.get("final_reply", "") != reply_composer(state)["final_reply"]:
        violations.append({"type": "reply_does_not_match_facts"})
    for item in state.get("tool_results", []):
        if item["tool"] == "query_knowledge" and item["status"] == "ok":
            data = item["data"]
            entries = data["entries"]
            if not entries or not entries[0]["answer"] or entries[0]["answer"] not in data["evidence"]:
                violations.append({"type": "knowledge_answer_without_evidence"})
    if violations:
        return {"final_reply": _SAFE_FALLBACK, "reply_source": "safe_fallback",
                "guard_result": {"passed": False, "violations": violations, "fallback_used": True}}
    return {"guard_result": {"passed": True, "violations": [], "fallback_used": False}}
