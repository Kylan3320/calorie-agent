from __future__ import annotations

import json
import os
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from ..schemas import PLAN_SCHEMA_VERSION, AgentPlan


LLM_PLANNER_PROMPT_VERSION = "langgraph_agent_llm_planner.v0"


class LLMAction(BaseModel):
    action_id: str = Field(description="Stable action id, such as a1.")
    tool: str = Field(description="One registered tool name.")
    parameters: dict[str, Any] = Field(default_factory=dict)
    reason: str = Field(default="")
    safety: str = Field(description="read_only, write, destructive, or permission_sensitive")
    depends_on: list[str] = Field(default_factory=list)


class LLMPlan(BaseModel):
    schema_version: str = Field(default=PLAN_SCHEMA_VERSION)
    goal: str
    actions: list[LLMAction] = Field(default_factory=list)
    used_context: list[str] = Field(default_factory=list)
    needs_confirmation: bool = False
    confidence: float | None = None


SYSTEM_PROMPT = """
You are the planner for LangGraph Agent V0, a local calorie tracking prototype.
Return only a structured plan. Do not execute tools.

Registered tools:
- record_food: write action for one food and grams.
- query_today: read today's fake intake summary.
- search_food: read fake food candidates.
- query_food_nutrition: read nutrition for a stored fake intake record.
- undo_intake: destructive action for undo requests.
- generate_daily_report: read fake daily report facts.

Rules:
- Never calculate nutrition totals.
- Never claim a write, delete, or update happened.
- Use one record_food action per food item.
- If the user asks what they ate today or remaining targets, use query_today.
- If the user asks about a recorded item's nutrition, use query_food_nutrition.
- If the user asks to undo or delete, use undo_intake and safety=destructive.
- If no task needs a tool, return no actions.
- Every action must declare safety.
""".strip()


def plan_with_llm(
    state: dict[str, Any],
    *,
    model: BaseChatModel | None = None,
) -> AgentPlan:
    chat_model = model or _default_model()
    structured = chat_model.with_structured_output(LLMPlan)
    result = structured.invoke(_messages(state))
    if isinstance(result, LLMPlan):
        payload = result.model_dump()
    elif isinstance(result, dict):
        payload = result
    else:
        payload = dict(result)  # type: ignore[arg-type]
    payload["schema_version"] = payload.get("schema_version") or PLAN_SCHEMA_VERSION
    return AgentPlan.from_dict(payload)


def _messages(state: dict[str, Any]) -> list[Any]:
    context = {
        "mode": state.get("mode", "shadow"),
        "normalized_text": state.get("normalized_text", ""),
        "retrieved_context": state.get("retrieved_context", {}),
    }
    return [
        SystemMessage(content=SYSTEM_PROMPT),
        HumanMessage(
            content="Context JSON:\n"
            + json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n\nUser message:\n"
            + str(state.get("normalized_text", ""))
        ),
    ]


def _default_model() -> BaseChatModel:
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:
        raise RuntimeError("langchain-openai is required for planner_backend=llm") from exc

    api_key = os.getenv("DEEPSEEK_API_KEY", "")
    if not api_key:
        raise RuntimeError("DEEPSEEK_API_KEY is required for planner_backend=llm")
    return ChatOpenAI(
        model=os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        api_key=api_key,
        base_url=os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com").rstrip("/"),
        temperature=0,
    )


__all__ = [
    "LLMAction",
    "LLMPlan",
    "LLM_PLANNER_PROMPT_VERSION",
    "SYSTEM_PROMPT",
    "plan_with_llm",
]
