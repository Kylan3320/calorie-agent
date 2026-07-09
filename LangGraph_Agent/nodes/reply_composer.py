"""Reply composer node wrapper."""

from typing import Any

from ..schemas import AgentPlan
from ..state import LangGraphAgentState


def reply_composer(state: LangGraphAgentState) -> dict[str, Any]:
    if state.get("mode") == "core_candidate":
        return _core_reply_composer(state)
    if state.get("mode") == "readonly_candidate":
        return _readonly_reply_composer(state)
    if state.get("mode") == "write_candidate":
        return _write_reply_composer(state)

    plan = AgentPlan.from_dict(state.get("plan", {}))
    results = state.get("tool_results", [])
    decision = state.get("policy_decision", {})
    blocked = decision.get("blocked_actions") if isinstance(decision, dict) else []
    blocked_tools = [str(item.get("tool") or "") for item in blocked if isinstance(item, dict)]

    if not plan.actions:
        return {"final_reply": "V0 没有识别到需要调用的工具。本地原型不会执行任何写入。"}
    if any(action.tool == "undo_intake" for action in plan.actions):
        return {"final_reply": "V0 已识别撤销请求，但撤销属于高风险动作，本地原型不会删除真实记录。"}
    if any(action.tool == "query_food_nutrition" for action in plan.actions):
        total = _first_result_data(results, "query_food_nutrition").get("total", {})
        return {
            "final_reply": (
                "V0 使用 fake 已记录摄入快照查询到："
                f"300g 西瓜约 84 kcal，碳水 18.0g，蛋白质 1.8g，脂肪 0.6g。"
            )
            if total
            else "V0 没有找到可用的 fake 摄入快照。"
        }
    if any(action.tool == "generate_daily_report" for action in plan.actions):
        return {"final_reply": "V0 生成了 fake 今日总结：当前示例总热量 375 kcal。本地原型仅用于验证流程。"}
    if any(action.tool == "query_today" for action in plan.actions):
        prefix = ""
        if "record_food" in blocked_tools:
            record_count = len([action for action in plan.actions if action.tool == "record_food"])
            prefix = f"V0 已识别 {record_count} 条记录动作，但不会写入真实数据。"
        return {"final_reply": prefix + "fake 今日汇总：2 条记录，合计 375 kcal。"}
    if any(action.tool == "record_food" for action in plan.actions):
        count = len([action for action in plan.actions if action.tool == "record_food"])
        return {"final_reply": f"V0 已识别 {count} 条记录动作。本地原型不会写入真实数据。"}
    return {"final_reply": "V0 已完成本地 fake 流程。"}


def _first_result_data(results: list[Any], tool: str) -> dict[str, Any]:
    for item in results:
        if isinstance(item, dict) and item.get("tool") == tool and isinstance(item.get("data"), dict):
            return item["data"]
    return {}


def _readonly_reply_composer(state: LangGraphAgentState) -> dict[str, Any]:
    candidate = state.get("readonly_candidate") if isinstance(state.get("readonly_candidate"), dict) else {}
    if candidate.get("status") != "ok":
        return {"final_reply": "", "action_summary": ""}

    results = state.get("tool_results", [])
    successful = [item for item in results if isinstance(item, dict) and str(item.get("status") or "") == "ok"]
    action_parts = [_action_text(item) for item in successful]
    replies = [_result_reply(item) for item in successful]
    action_summary = _compose_action_summary(action_parts)
    body = "\n".join(reply for reply in replies if reply).strip()
    if not body:
        candidate = {
            **candidate,
            "status": "fallback_empty_reply",
            "selected_reply_source": "current_runtime",
            "blocked_reason": "empty_tool_reply",
            "fallback_used": True,
        }
        return {"final_reply": "", "action_summary": "", "readonly_candidate": candidate}
    final_reply = f"{action_summary}\n{body}".strip() if action_summary else body
    return {"final_reply": final_reply, "action_summary": action_summary}


def _write_reply_composer(state: LangGraphAgentState) -> dict[str, Any]:
    candidate = state.get("write_candidate") if isinstance(state.get("write_candidate"), dict) else {}
    if candidate.get("status") != "ok":
        return {"final_reply": "", "action_summary": ""}

    results = state.get("tool_results", [])
    action_parts = [_write_action_text(item) for item in results if isinstance(item, dict) and str(item.get("status") or "") in {"ok", "duplicate_skipped"}]
    body_parts = [_write_result_reply(item) for item in results if isinstance(item, dict)]
    action_summary = _compose_action_summary(action_parts)
    body = "\n".join(part for part in body_parts if part).strip()
    if not body:
        candidate = {
            **candidate,
            "status": "fallback_empty_reply",
            "selected_reply_source": "current_runtime",
            "blocked_reason": "empty_tool_reply",
            "fallback_used": True,
        }
        return {"final_reply": "", "action_summary": "", "write_candidate": candidate}
    final_reply = f"{action_summary}\n{body}".strip() if action_summary else body
    return {"final_reply": final_reply, "action_summary": action_summary}


def _core_reply_composer(state: LangGraphAgentState) -> dict[str, Any]:
    candidate = dict(state.get("core_candidate") or {})
    if candidate.get("status") != "ok":
        return {"final_reply": "", "action_summary": "", "core_action_summary": ""}

    results = [item for item in state.get("tool_results", []) if isinstance(item, dict)]
    successful = [item for item in results if str(item.get("status") or "") in {"ok", "duplicate_skipped"}]
    summary = _compose_core_action_summary([_core_action_text(item) for item in successful])
    body = "\n".join(_core_result_reply(item) for item in results).strip()
    if not body:
        candidate = {
            **candidate,
            "status": "fallback_empty_reply",
            "selected_reply_source": "current_runtime",
            "blocked_reason": "empty_tool_reply",
            "fallback_used": True,
        }
        return {"final_reply": "", "action_summary": "", "core_action_summary": "", "core_candidate": candidate}
    final_reply = f"{summary}\n{body}".strip() if summary else body
    return {"final_reply": final_reply, "action_summary": summary, "core_action_summary": summary}


def _compose_action_summary(parts: list[str]) -> str:
    parts = [part for part in parts if part]
    if not parts:
        return ""
    if len(parts) == 1:
        return f"我做了一步：{parts[0]}。"
    return f"我做了{len(parts)}步：" + "，再".join(parts) + "。"


def _action_text(result: dict[str, Any]) -> str:
    tool = str(result.get("tool") or "")
    if tool == "query_today":
        return "查询今天汇总"
    if tool == "query_food_nutrition":
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        mode = str(data.get("mode") or "")
        return "查询已记录食物营养" if mode == "stored_intake" else "查询食物库营养"
    if tool == "generate_daily_report":
        return "生成今日汇总"
    return f"调用{tool}"


def _result_reply(result: dict[str, Any]) -> str:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return str(data.get("reply") or data.get("summary_text") or "")


def _compose_core_action_summary(parts: list[str]) -> str:
    parts = [part for part in parts if part]
    if not parts:
        return ""
    if len(parts) == 1:
        return f"I did one step: {parts[0]}."
    return f"I did {len(parts)} steps: " + ", then ".join(parts) + "."


def _core_action_text(result: dict[str, Any]) -> str:
    tool = str(result.get("tool") or "")
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if tool == "record_food":
        item = data.get("recorded_item") if isinstance(data.get("recorded_item"), dict) else {}
        food = str(item.get("food_name") or data.get("food_query") or "food")
        grams = _format_number(data.get("grams") or item.get("grams") or 0)
        if str(result.get("status") or "") == "duplicate_skipped":
            return f"skip duplicate record for {food} {grams}g"
        return f"record {food} {grams}g"
    if tool == "query_today":
        return "query today summary"
    if tool == "query_food_nutrition":
        return "query food nutrition"
    if tool == "generate_daily_report":
        return "generate daily report"
    return f"call {tool}"


def _core_result_reply(result: dict[str, Any]) -> str:
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    return str(data.get("reply") or data.get("summary_text") or "")


def _format_number(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"


def _write_action_text(result: dict[str, Any]) -> str:
    tool = str(result.get("tool") or "")
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if tool == "record_food":
        item = data.get("recorded_item") if isinstance(data.get("recorded_item"), dict) else {}
        if str(result.get("status") or "") == "duplicate_skipped":
            return "璇嗗埆鍒伴噸澶嶈褰曞苟璺宠繃鍐欏叆"
        return f"璁板綍{item.get('food_name') or data.get('food_query')}{float(data.get('grams') or 0):g}g"
    if tool == "query_today":
        return "鏌ヨ浠婂ぉ姹囨€?"
    if tool == "query_food_nutrition":
        return "鏌ヨ椋熺墿搴撹惀鍏?"
    return f"璋冪敤{tool}"


def _write_result_reply(result: dict[str, Any]) -> str:
    tool = str(result.get("tool") or "")
    status = str(result.get("status") or "")
    data = result.get("data") if isinstance(result.get("data"), dict) else {}
    if tool == "record_food" and status == "ok":
        item = data.get("recorded_item") if isinstance(data.get("recorded_item"), dict) else {}
        food_name = item.get("food_name") or data.get("food_query")
        return f"已记录{food_name} {float(data.get('grams') or 0):g}g。"
    if tool == "record_food" and status == "duplicate_skipped":
        return "这条记录和已处理消息重复，已跳过重复写入。"
    return str(data.get("reply") or data.get("summary_text") or "")

__all__ = ["reply_composer"]
