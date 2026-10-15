"""Planner node: deterministic first, DeepSeek only as escalation (2026-09-07 token 工作单).

确定性规划器是一等后端：record/query/undo/knowledge/history 全部有可靠规则，
规则命中即直接用（零 LLM 成本）；只有规则判 unknown 的模糊输入才升级 DeepSeek。
这把 LLM 调用从"每条消息一次"压到"仅模糊输入一次"（REQUIREMENTS.md §8 工作单 1）。
"""

from __future__ import annotations

import re
from typing import Any

from ..planners import build_plan, plan_with_deepseek
from ..schemas import AgentPlan
from ..state import AgentState

# 高频寒暄/无意义输入：确定性判 unknown 即终局，不烧 LLM（工作单 1 的补充闸门）
_SMALLTALK_RE = re.compile(
    r"^(你好+|您好|在吗|在么|谢谢|多谢|感谢|早安|午安|晚安|好的|嗯+|哦+|哈哈+|嗨|hi|hello|okay?|收到|test|测试)[\s!！~。.？?]*$",
    re.IGNORECASE,
)


def planner(state: AgentState) -> dict[str, Any]:
    config = state.get("config")
    text = str(state.get("normalized_text") or "")
    errors = list(state.get("errors") or [])
    backend = getattr(config, "planner", "deterministic")
    deterministic = build_plan(text)

    if backend != "deepseek":
        print(f"[planner] backend=deterministic goal={deterministic.goal} actions={len(deterministic.actions)}")
        return {"plan": deterministic.to_dict(), "planner_fallback": False, "errors": errors}

    if deterministic.actions:
        print(f"[planner] deterministic hit goal={deterministic.goal} actions={len(deterministic.actions)} (LLM skipped)")
        return {"plan": deterministic.to_dict(), "planner_fallback": False, "errors": errors}

    if _SMALLTALK_RE.match(text.strip()):
        print(f"[planner] smalltalk gate: skip LLM")
        return {"plan": deterministic.to_dict(), "planner_fallback": False, "errors": errors}

    # 确定性判 unknown：升级 DeepSeek 处理模糊表达（LLM 的真正价值场景）
    try:
        plan = plan_with_deepseek(text, _planner_context(state), model=getattr(config, "model", ""))
        if plan.actions:
            print(f"[planner] deepseek escalation goal={plan.goal} actions={len(plan.actions)}")
            return {"plan": plan.to_dict(), "planner_fallback": False, "errors": errors}
        # LLM 也认为无事可做：维持确定性 unknown 结论
        print("[planner] deepseek escalation: no actions either")
        return {"plan": deterministic.to_dict(), "planner_fallback": False, "errors": errors}
    except Exception as exc:  # noqa: BLE001 - LLM 失败必须降级而非崩溃
        errors.append(f"deepseek_planner_failed:{type(exc).__name__}:{exc}")
        print(f"[planner] deepseek FAILED {type(exc).__name__}: {exc} -> deterministic unknown")
        return {"plan": deterministic.to_dict(), "planner_fallback": True, "errors": errors}


def _planner_context(state: AgentState) -> dict[str, Any]:
    task = (state.get("context") or {}).get("active_pending")
    context: dict[str, Any] = {}
    if task:
        context["active_pending"] = {
            "intent": task.get("intent", ""),
            "food_name": task.get("food_name", ""),
            "raw_text": task.get("raw_text", ""),
        }
    return context


__all__ = ["planner"]
