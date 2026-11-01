"""Reply composer: build the final reply from execution facts only.

No hardcoded demo numbers; every value printed here comes from tool_results
or pending facts, and reply_guard re-checks the numbers afterwards.
"""

from __future__ import annotations

from typing import Any

from ..state import AgentState

_BLOCKED_REASON_TEXT = {
    "write_not_allowed_in_preset": "当前是只读模式，不会执行写入。",
    "too_many_actions": "这条消息包含的动作太多，请拆开发送。",
    "pending_no_longer_active": "这条待确认任务已结束或过期，请重新发起请求。",
}


def reply_composer(state: AgentState) -> dict[str, Any]:
    decision = state.get("policy_decision") or {}
    results = state.get("tool_results") or []
    errors = state.get("errors") or []

    if decision and not decision.get("allowed"):
        return {
            "final_reply": _blocked_reply(decision),
            "reply_source": "safe_fallback",
            "action_summary": "",
        }

    if any(item.get("status") == "needs_confirmation_plan" for item in results if isinstance(item, dict)):
        ask = "要撤销最近一条有效记录吗？回复「确认」执行，回复「取消」放弃。"
        return {"final_reply": ask, "reply_source": "agent", "action_summary": ""}

    body_parts, labels = [], []
    for item in results:
        if not isinstance(item, dict):
            continue
        body, label = _compose_result(item)
        if body:
            body_parts.append(body)
        if label:
            labels.append(label)

    body = "\n".join(part for part in body_parts if part).strip()
    summary = _compose_summary(labels)
    reply = f"{summary}\n{body}".strip() if summary else body
    if not reply:
        if errors:
            reply = "刚才处理时遇到问题，请稍后重试或换个说法。"
        else:
            reply = "没有识别到需要处理的内容。你可以记录食物和克重、查询今日摄入，或撤销上一条。"
        return {"final_reply": reply, "reply_source": "safe_fallback", "action_summary": ""}
    return {"final_reply": reply, "reply_source": "agent", "action_summary": summary}


def _compose_summary(labels: list[str]) -> str:
    labels = [label for label in labels if label]
    if len(labels) <= 1:
        return ""
    return "本次处理：" + "，再".join(labels) + "。"


def _compose_result(item: dict[str, Any]) -> tuple[str, str]:
    tool = str(item.get("tool") or "")
    status = str(item.get("status") or "")
    data = item.get("data") if isinstance(item.get("data"), dict) else {}

    if status == "error":
        return "这一步处理失败，尚未确认结果，请稍后重试。", ""
    if status == "pending_cancelled":
        return "已取消这条待补全记录。", ""
    if status == "pending_deferred":
        return "已保留这条待补全记录，你可以稍后补充。", ""

    if tool == "record_food":
        if status == "food_library_added":
            food = data["imported"]
            return (
                f"已把「{food['name']}」加入食物库（每100g：{food['kcal_per_100g']:g} kcal，"
                f"碳水 {food['carbs_per_100g']:g}g、蛋白质 {food['protein_per_100g']:g}g、脂肪 {food['fat_per_100g']:g}g）。"
                f"现在发「{food['name']} + 克重」即可记录。数据来源：薄荷健康。"
            ), "确认食物入库"
        if status == "ok":
            write = data.get("intake_write_result") or {}
            if write.get("status") != "created" or write.get("created_count", 0) <= 0:
                return "这条记录的写入结果尚未确认，请查询记录核对。", ""
            nutrition = data.get("nutrition") if isinstance(data.get("nutrition"), dict) else {}
            body = (
                f"已记录{data.get('matched_name', '')} {float(data.get('grams') or 0):g}g："
                f"{float(nutrition.get('kcal') or 0):g} kcal"
                f"（碳水 {float(nutrition.get('carbs') or 0):g}g、"
                f"蛋白质 {float(nutrition.get('protein') or 0):g}g、"
                f"脂肪 {float(nutrition.get('fat') or 0):g}g）。"
            )
            if data.get("source") == "boohee":
                body += "数据来源：薄荷健康。"
            return body, f"记录{data.get('matched_name', '')}{float(data.get('grams') or 0):g}g"
        if status == "duplicate_skipped":
            return "这条消息之前处理过，已跳过重复写入。", "识别重复消息并跳过"
        if status == "pending_created":
            if item.get("error"):
                return "暂时无法查询外部食物库，这条记录尚未写入，请稍后重试。", ""
            pending = data.get("pending_result") if isinstance(data.get("pending_result"), dict) else {}
            if pending.get("intent") == "missing_food":
                return f"食物库里还没有「{data.get('food_query', '')}」，暂时无法记录这一条。", "创建待补全提醒"
            return f"「{data.get('food_query', '')}」还缺克重：回复一个克重即可（数字+单位）。", "创建待补全提醒"
        if status == "needs_confirmation":
            if data.get("mode") == "remote_candidates":
                rows = []
                for c in data.get("candidates", []):
                    if isinstance(c, dict):
                        rows.append(f"{c.get('index')}. {c.get('name')}（每100g {float(c.get('kcal_per_100g') or 0):g} kcal）")
                listing = "；".join(rows) if rows else "相近候选"
                return (
                    f"食物库里没有「{data.get('food_query', '')}」，薄荷健康找到候选：{listing}。"
                    "回复序号确认入库（数据来源：薄荷健康）。"
                ), "请求确认薄荷候选"
            names = [str(c.get("food_name") or "") for c in data.get("candidates", []) if isinstance(c, dict)]
            names = [name for name in names if name]
            hint = "、".join(f"{i + 1}. {name}" for i, name in enumerate(names)) if names else "相近食物"
            return f"「{data.get('food_query', '')}」找到多个相近食物：{hint}。请回复序号，或用更确切的名称重新记录。", "请求确认食物"
        return "", ""

    if tool == "query_today" and status == "ok":
        summary = data.get("today_summary") if isinstance(data.get("today_summary"), dict) else {}
        standard = data.get("daily_standard") if isinstance(data.get("daily_standard"), dict) else {}
        return format_today_reply(summary, standard), "查询今日汇总"

    if tool == "query_food_nutrition":
        if status == "ok" and data.get("mode") == "stored_intake":
            total = data.get("total") if isinstance(data.get("total"), dict) else {}
            return (
                f"今天已记录「{data.get('food_query', '')}」共 {float(total.get('grams') or 0):g}g："
                f"{float(total.get('kcal') or 0):g} kcal"
                f"（碳水 {float(total.get('carbs') or 0):g}g、"
                f"蛋白质 {float(total.get('protein') or 0):g}g、"
                f"脂肪 {float(total.get('fat') or 0):g}g）。"
            ), "查询已记录食物营养"
        if status == "ok" and data.get("mode") == "food_database":
            nutrition = data.get("nutrition") if isinstance(data.get("nutrition"), dict) else {}
            return (
                f"「{data.get('food_name', '')}」{float(data.get('grams') or 0):g}g 约为 "
                f"{float(nutrition.get('kcal') or 0):g} kcal"
                f"（碳水 {float(nutrition.get('carbs') or 0):g}g、"
                f"蛋白质 {float(nutrition.get('protein') or 0):g}g、"
                f"脂肪 {float(nutrition.get('fat') or 0):g}g）。"
            ), "查询食物库营养"
        if status == "needs_confirmation":
            names = [str((c.get("food") or {}).get("name", "")) for c in data.get("candidates", []) if isinstance(c, dict)]
            names = [name for name in names if name]
            hint = "、".join(names[:3]) if names else "相近食物"
            return f"「{data.get('food_query', '')}」找到多个相近食物：{hint}。请用更确切的名称再问一次。", "请求确认食物"
        if status == "not_found":
            return f"没有查到「{data.get('food_query', '')}」的营养数据。", "查询食物营养"
        return "", ""

    if tool == "undo_last":
        undo = data.get("undo_result") if isinstance(data.get("undo_result"), dict) else {}
        if undo.get("status") == "deleted":
            item = undo.get("item") if isinstance(undo.get("item"), dict) else {}
            return f"已撤销 {item.get('food_name', '记录')} {float(item.get('grams') or 0):g}g。", "撤销最近一条记录"
        if undo.get("status") == "needs_confirmation":
            return "", ""
        return "当前没有可撤销的记录。", "撤销最近一条记录"

    if tool == "query_history" and status == "ok":
        summary = data.get("summary") if isinstance(data.get("summary"), dict) else {}
        total = summary.get("total") if isinstance(summary.get("total"), dict) else {}
        days = summary.get("days") if isinstance(summary.get("days"), list) else []
        label = str(data.get("period_label") or "该时段")
        food = str(summary.get("food_query") or "")
        subject = f"「{food}」" if food else ""
        if int(total.get("count") or 0) == 0:
            return f"{label}{subject}没有相关记录。", "查询历史摄入"
        if food:
            head = (
                f"{label}{subject}共 {int(total.get('count') or 0)} 次、"
                f"{float(total.get('grams') or 0):g}g / {float(total.get('kcal') or 0):g} kcal"
                f"（碳水 {float(total.get('carbs') or 0):g}g、蛋白质 {float(total.get('protein') or 0):g}g、脂肪 {float(total.get('fat') or 0):g}g）。"
            )
            lines = [
                f"- {day.get('date', '')}：{float(day.get('grams') or 0):g}g / {float(day.get('kcal') or 0):g} kcal"
                for day in days
            ]
            return "\n".join([head, *lines]), "查询历史摄入"
        head = (
            f"{label}共 {int(total.get('count') or 0)} 条记录，合计 {float(total.get('kcal') or 0):g} kcal"
            f"（碳水 {float(total.get('carbs') or 0):g}g、蛋白质 {float(total.get('protein') or 0):g}g、脂肪 {float(total.get('fat') or 0):g}g）。"
        )
        lines = [f"- {day.get('date', '')}：{int(day.get('count') or 0)} 条 / {float(day.get('kcal') or 0):g} kcal" for day in days]
        body_parts = [head, *lines]
        over = summary.get("over_budget_days") if isinstance(summary.get("over_budget_days"), list) else []
        if over:
            body_parts.append("其中热量超标日：" + "、".join(str(d) for d in over))
        return "\n".join(body_parts), "查询历史摄入"

    if tool == "query_knowledge":
        if status == "ok":
            entries = data.get("entries") if isinstance(data.get("entries"), list) else []
            top = entries[0] if entries and isinstance(entries[0], dict) else {}
            answer = str(top.get("answer") or "")
            if not answer:
                return "知识库里还没有收录这个问题，之后补充后再来问我。", "查询饮食知识"
            source = str(top.get("source") or "")
            if source:
                answer = f"{answer}\n来源：{source}"
            return answer, "查询饮食知识"
        if status == "not_found":
            return "知识库里还没有收录这个问题，你可以换个问法试试。", "查询饮食知识"
        return "知识检索暂时不可用，请稍后再试。", ""
    return "", ""


def _blocked_reply(decision: dict[str, Any]) -> str:
    reasons = []
    for blocked in decision.get("blocked_actions", []):
        if not isinstance(blocked, dict):
            continue
        reason = str(blocked.get("reason") or "")
        if reason.startswith("tool_not_allowed:"):
            reasons.append(f"工具 {reason.split(':', 1)[1]} 不在当前允许范围内")
        else:
            reasons.append(_BLOCKED_REASON_TEXT.get(reason, reason or "未知原因"))
    unique = list(dict.fromkeys(reasons)) or ["未知原因"]
    return "这条请求被安全策略拦截：" + "；".join(unique) + "。"


# --- today summary formatter (ported from V2 legacy_app 2357-2441) -----------

def format_today_reply(summary: dict[str, Any], standard: dict[str, Any]) -> str:
    lines = ["今日摄入汇总："]
    lines.extend(_today_items_lines(summary))
    lines.extend(_today_totals_lines(summary, standard))
    return "\n".join(lines)


def _today_items_lines(summary: dict[str, Any]) -> list[str]:
    items = summary.get("items")
    lines = ["", "今日清单："]
    if not isinstance(items, list) or not items:
        lines.append("- 暂无有效记录")
        return lines
    for item in items:
        nutrition = item.get("nutrition") if isinstance(item.get("nutrition"), dict) else {}
        lines.append(
            f"- {_meal_label(str(item.get('meal_type') or 'unknown'))} "
            f"{item.get('food_name') or '未命名食物'} {float(item.get('grams') or 0):g}g："
            f"{float(nutrition.get('kcal') or 0):g} kcal，"
            f"碳水 {float(nutrition.get('carbs') or 0):g}g，"
            f"蛋白质 {float(nutrition.get('protein') or 0):g}g，"
            f"脂肪 {float(nutrition.get('fat') or 0):g}g"
        )
    return lines


def _today_totals_lines(summary: dict[str, Any], standard: dict[str, Any]) -> list[str]:
    totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
    count = int(summary.get("count") or 0)
    lines = [
        "",
        "今日累计：",
        f"日期：{summary.get('date', '')}，记录 {count} 条",
        f"标准来源：{'默认值' if standard.get('source') in {'default', 'fake'} else '多维表格'}",
    ]
    metrics = [("kcal", "热量", "kcal"), ("carbs", "碳水", "g"), ("protein", "蛋白质", "g"), ("fat", "脂肪", "g")]
    for key, label, unit in metrics:
        value = float(totals.get(key) or 0)
        target = float(standard.get(key) or 0)
        lines.append(f"{label}：{value:g}/{target:g} {unit} {_progress_bar(value, target)}")
    remaining = {
        key: max(round(float(standard.get(key) or 0) - float(totals.get(key) or 0), 1), 0)
        for key, _, _ in metrics
    }
    lines.append(
        "剩余："
        f"热量 {remaining['kcal']:g} kcal，"
        f"碳水 {remaining['carbs']:g}g，"
        f"蛋白质 {remaining['protein']:g}g，"
        f"脂肪 {remaining['fat']:g}g"
    )
    return lines


def _meal_label(meal_type: str) -> str:
    return {
        "breakfast": "早餐",
        "lunch": "午餐",
        "dinner": "晚餐",
        "post_workout": "练后餐",
        "snack": "加餐",
        "unknown": "未标注",
    }.get(meal_type, meal_type or "未标注")


def _progress_bar(value: float, target: float, width: int = 10) -> str:
    if target <= 0:
        return "[----------] n/a"
    percent = max(value / target * 100, 0)
    filled = min(width, int(round(percent / 100 * width)))
    return f"[{'#' * filled}{'-' * (width - filled)}] {percent:.1f}%"


__all__ = ["format_today_reply", "reply_composer"]
