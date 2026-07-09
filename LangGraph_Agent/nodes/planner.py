"""Planner node wrappers."""

from typing import Any

from ..llm_planner import plan_with_llm
from ..planners.deterministic import _build_plan, deterministic_planner as _base_deterministic_planner
from ..schemas import AgentPlan
from ..state import LangGraphAgentState


def llm_planner(state: LangGraphAgentState) -> dict[str, Any]:
    override = _plan_override(state)
    if override is not None:
        return _write_plan_update(state, override) if state.get("mode") in {"write_candidate", "core_candidate"} else override
    errors = list(state.get("errors", []))
    try:
        plan = plan_with_llm(dict(state))
        if state.get("mode") in {"write_candidate", "core_candidate"}:
            return {"plan": _normalize_write_plan(plan).to_dict(), "errors": errors}
        return {"plan": plan.to_dict(), "errors": errors}
    except Exception as exc:
        errors.append(f"llm_planner_failed:{type(exc).__name__}:{exc}")
        text = str(state.get("normalized_text") or "")
        plan = _build_plan(text)
        payload = plan.to_dict()
        payload["planner_fallback"] = "deterministic"
        return {"plan": payload, "errors": errors}


def _deterministic_planner(state: LangGraphAgentState) -> dict[str, Any]:
    override = _plan_override(state)
    if override is not None:
        return _write_plan_update(state, override) if state.get("mode") in {"write_candidate", "core_candidate"} else override
    if state.get("mode") not in {"write_candidate", "core_candidate"}:
        return _base_deterministic_planner(state)

    base = _base_deterministic_planner(state)
    plan = AgentPlan.from_dict(base.get("plan", {}))
    if state.get("mode") == "write_candidate" and not plan.actions:
        plan = _missing_grams_record_plan(str(state.get("normalized_text") or state.get("raw_text") or "")) or plan
    return {"plan": _normalize_write_plan(plan).to_dict(), "errors": list(state.get("errors", []))}


def _plan_override(state: LangGraphAgentState) -> dict[str, Any] | None:
    override = state.get("plan_override")
    if not isinstance(override, dict) or not override:
        return None
    errors = list(state.get("errors", []))
    try:
        return {"plan": AgentPlan.from_dict(override).to_dict(), "errors": errors}
    except Exception as exc:
        errors.append(f"plan_override_failed:{type(exc).__name__}:{exc}")
        return {"plan": AgentPlan(goal="unknown").to_dict(), "errors": errors}


def _write_plan_update(state: LangGraphAgentState, update: dict[str, Any]) -> dict[str, Any]:
    return {"plan": _normalize_write_plan(AgentPlan.from_dict(update.get("plan", {}))).to_dict(), "errors": list(update.get("errors", state.get("errors", [])))}


def _normalize_write_plan(plan: AgentPlan) -> AgentPlan:
    actions = []
    for action in plan.actions:
        payload = action.to_dict()
        if action.tool == "search_food":
            payload["tool"] = "query_food_nutrition"
            payload["safety"] = "read_only"
        actions.append(payload)
    return AgentPlan.from_dict({**plan.to_dict(), "actions": actions})


def _missing_grams_record_plan(text: str) -> AgentPlan | None:
    stripped = str(text or "").strip()
    record_tokens = ("璁板綍", "记录")
    if not any(token in stripped for token in record_tokens):
        return None
    if any(token in stripped for token in ("濡堝", "鐖哥埜", "瀛╁瓙", "瀹朵汉", "鍒犻櫎", "鎾ら攢", "鐩爣", "鏍囧噯", "妈妈", "爸爸", "孩子", "家人", "删除", "撤销", "目标", "标准")):
        return None
    food = stripped.replace("甯垜", "").replace("帮我", "")
    for token in record_tokens:
        food = food.replace(token, "")
    food = food.strip(" 锛?銆?;锛?，。；")
    if not food:
        return None
    return AgentPlan.from_dict(
        {
            "goal": "record_intake",
            "actions": [
                {
                    "action_id": "a1",
                    "tool": "record_food",
                    "parameters": {"food_query": food, "meal_type": "unknown"},
                    "reason": "鐢ㄦ埛杈撳叆椋熺墿浣嗙己灏戝厠鏁?",
                    "safety": "write",
                }
            ],
            "used_context": ["project_docs", "food_db"],
            "confidence": 0.72,
        }
    )


deterministic_planner = _deterministic_planner

__all__ = ["deterministic_planner", "llm_planner"]
