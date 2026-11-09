from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .reply_guard import validate_reply
from .schemas import AgentPlan


AGENT_EVAL_VERSION = "agent_eval.v1"


def load_eval_cases(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("agent eval fixture must be a JSON array")
    return [case for case in payload if isinstance(case, dict)]


def evaluate_cases(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [evaluate_case(case) for case in cases]


def evaluate_case(case: dict[str, Any]) -> dict[str, Any]:
    plan = AgentPlan.from_dict(case.get("planner_response") if isinstance(case.get("planner_response"), dict) else {})
    failures: list[str] = []
    failures.extend(_compare_list("tools", _tools(plan), case.get("expected_tools")))
    failures.extend(_compare_list("foods", _foods(plan), case.get("expected_foods")))
    failures.extend(_compare_list("meal_types", _meal_types(plan), case.get("expected_meal_types")))
    failures.extend(_check_params(plan, case.get("expected_params")))
    failures.extend(_check_targets(plan, case.get("expected_targets")))
    failures.extend(_check_forbidden_tools(plan, case.get("must_not_include_tools")))
    failures.extend(_check_reply_guard(case))
    return {
        "id": case.get("id", ""),
        "passed": not failures,
        "failures": failures,
        "tool_count": len(plan.actions),
        "version": AGENT_EVAL_VERSION,
    }


def _tools(plan: AgentPlan) -> list[str]:
    return [action.tool for action in plan.actions]


def _foods(plan: AgentPlan) -> list[str]:
    return [
        str(action.params.get("food_query") or action.params.get("food_name") or "")
        for action in plan.actions
        if action.tool in {"record_food", "create_food", "create_pending", "search_food"}
    ]


def _meal_types(plan: AgentPlan) -> list[str]:
    return [
        str(action.params.get("meal_type") or "unknown")
        for action in plan.actions
        if action.tool == "record_food"
    ]


def _compare_list(name: str, actual: list[str], expected: Any) -> list[str]:
    if expected is None:
        return []
    if actual == list(expected):
        return []
    return [f"{name}: expected {list(expected)}, got {actual}"]


def _check_targets(plan: AgentPlan, expected: Any) -> list[str]:
    if not isinstance(expected, list):
        return []
    failures: list[str] = []
    for index, target in enumerate(expected):
        if not isinstance(target, dict):
            continue
        if index >= len(plan.actions):
            failures.append(f"target[{index}]: missing action")
            continue
        actual = plan.actions[index].target
        for key, value in target.items():
            if actual.get(key) != value:
                failures.append(f"target[{index}].{key}: expected {value}, got {actual.get(key)}")
    return failures


def _check_params(plan: AgentPlan, expected: Any) -> list[str]:
    if not isinstance(expected, list):
        return []
    failures: list[str] = []
    for index, params in enumerate(expected):
        if not isinstance(params, dict):
            continue
        if index >= len(plan.actions):
            failures.append(f"params[{index}]: missing action")
            continue
        actual = plan.actions[index].params
        for key, value in params.items():
            if actual.get(key) != value:
                failures.append(f"params[{index}].{key}: expected {value}, got {actual.get(key)}")
    return failures


def _check_forbidden_tools(plan: AgentPlan, forbidden: Any) -> list[str]:
    if not isinstance(forbidden, list):
        return []
    used = set(_tools(plan))
    return [f"forbidden_tool_used:{tool}" for tool in forbidden if tool in used]


def _check_reply_guard(case: dict[str, Any]) -> list[str]:
    guard_case = case.get("reply_guard")
    if not isinstance(guard_case, dict):
        return []
    result = validate_reply(
        str(guard_case.get("reply") or ""),
        guard_case.get("execution_facts") if isinstance(guard_case.get("execution_facts"), dict) else {},
    )
    expected = bool(guard_case.get("expected_passed"))
    if result.passed == expected:
        return []
    return [f"reply_guard: expected {expected}, got {result.to_dict()}"]
