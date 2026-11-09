from __future__ import annotations

from typing import Any


def build_deterministic_fallback(
    execution_facts: dict[str, Any],
    tool_results: list[dict[str, Any]] | None = None,
    default_reply: str = "",
) -> str:
    """Build a fact-only fallback reply without model calls or tool execution."""

    facts = execution_facts if isinstance(execution_facts, dict) else {}
    results = [item for item in (tool_results or []) if isinstance(item, dict)]

    permission = _permission_denied_line(results)
    if permission:
        return permission

    query_line = _intake_query_line(facts, results)
    if query_line:
        return query_line
    if default_reply:
        return default_reply

    recorded_line = _recorded_line(facts)
    pending_line = _pending_line(facts)
    failure_line = _failure_line(facts, results)
    summary_line = _summary_line(facts)

    lines = [line for line in (recorded_line, pending_line, summary_line, failure_line) if line]
    if lines:
        return "\n".join(lines)
    return "我还没能确认可回复的执行结果，请换一种说法补充食物、克重或要查询的内容。"


def _permission_denied_line(tool_results: list[dict[str, Any]]) -> str:
    for result in tool_results:
        status = str(result.get("status") or "").lower()
        data = result.get("data") if isinstance(result.get("data"), dict) else {}
        text = " ".join([status, str(result.get("error") or ""), str(data.get("status") or ""), str(data.get("message") or "")])
        if "permission_denied" in text or "cross_user" in text or status == "denied":
            return "你没有权限查看该记录。"
    return ""


def _intake_query_line(facts: dict[str, Any], tool_results: list[dict[str, Any]]) -> str:
    query_results = _as_list(facts.get("intake_nutrition_queries"))
    for result in tool_results:
        if result.get("tool") == "query_intake_food_nutrition":
            data = result.get("data") if isinstance(result.get("data"), dict) else {}
            if data:
                query_results.append(data)

    for result in query_results:
        if not isinstance(result, dict):
            continue
        status = str(result.get("status") or "")
        query = result.get("query") if isinstance(result.get("query"), dict) else {}
        food_query = str(query.get("food_query") or "该食物")
        if status == "no_match":
            return f"今天没有找到{food_query}的有效记录。"
        if status == "multiple_candidates":
            candidates = result.get("candidates") if isinstance(result.get("candidates"), list) else []
            names = [str(item.get("food_name") or "") for item in candidates if isinstance(item, dict) and item.get("food_name")]
            if names:
                return "找到多个可能的食物：" + "、".join(names) + "。请指定要查询哪一个。"
            return "找到多个可能的食物，请指定要查询哪一个。"
        if status == "success":
            return _format_intake_query_success(result, food_query)
    return ""


def _format_intake_query_success(result: dict[str, Any], food_query: str) -> str:
    records = result.get("matched_records") if isinstance(result.get("matched_records"), list) else []
    total = result.get("total") if isinstance(result.get("total"), dict) else {}
    food_name = str(records[0].get("food_name") or food_query) if records and isinstance(records[0], dict) else food_query
    if len(records) > 1:
        details = []
        for record in records:
            if not isinstance(record, dict):
                continue
            meal = str(record.get("meal_type") or "")
            prefix = f"{meal} " if meal and meal != "unknown" else ""
            details.append(f"{prefix}{_fmt(record.get('grams'))}g，{_fmt(record.get('kcal'))} kcal")
        detail_text = "；".join(details)
        return (
            f"今天{food_name}有 {len(records)} 条记录：{detail_text}。"
            f"合计 {_fmt(total.get('grams'))}g，{_fmt(total.get('kcal'))} kcal，"
            f"碳水 {_fmt(total.get('carbs'))}g，蛋白质 {_fmt(total.get('protein'))}g，脂肪 {_fmt(total.get('fat'))}g。"
        )
    return (
        f"今天{food_name}记录：{_fmt(total.get('grams'))}g，{_fmt(total.get('kcal'))} kcal，"
        f"碳水 {_fmt(total.get('carbs'))}g，蛋白质 {_fmt(total.get('protein'))}g，脂肪 {_fmt(total.get('fat'))}g。"
    )


def _recorded_line(facts: dict[str, Any]) -> str:
    recorded = facts.get("recorded") if isinstance(facts.get("recorded"), list) else []
    if not recorded:
        return ""
    items = []
    for item in recorded:
        if not isinstance(item, dict):
            continue
        food = str(item.get("food_name") or item.get("input_name") or "该食物")
        items.append(f"{food} {_fmt(item.get('grams'))}g")
    if not items:
        return ""
    return "已记录：" + "、".join(items) + "。"


def _pending_line(facts: dict[str, Any]) -> str:
    pending = facts.get("pending") if isinstance(facts.get("pending"), list) else []
    if not pending:
        confirmations = facts.get("confirmations") if isinstance(facts.get("confirmations"), list) else []
        if confirmations:
            return "还需要你确认具体目标后再继续。"
        return ""
    item = pending[0] if isinstance(pending[0], dict) else {}
    food = str(item.get("food_name") or "该食物")
    if str(item.get("intent") or "") == "missing_grams":
        return f"还需要你补充{food}的克重。"
    if item.get("reply"):
        return str(item["reply"])
    return f"还需要你补充{food}的信息。"


def _failure_line(facts: dict[str, Any], tool_results: list[dict[str, Any]]) -> str:
    errors = facts.get("errors") if isinstance(facts.get("errors"), list) else []
    if errors:
        first = errors[0] if isinstance(errors[0], dict) else {}
        return "有步骤未完成：" + str(first.get("error") or first.get("tool") or "处理失败") + "。"
    for result in tool_results:
        status = str(result.get("status") or "").lower()
        if status in {"error", "failed"}:
            return "有步骤未完成：" + str(result.get("error") or result.get("tool") or "处理失败") + "。"
    return ""


def _summary_line(facts: dict[str, Any]) -> str:
    summary = facts.get("latest_summary") if isinstance(facts.get("latest_summary"), dict) else {}
    totals = summary.get("totals") if isinstance(summary.get("totals"), dict) else {}
    if not totals:
        return ""
    kcal = totals.get("kcal")
    if kcal is None:
        return ""
    return f"今日累计 {_fmt(kcal)} kcal。"


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _fmt(value: Any) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "0"
    if number.is_integer():
        return str(int(number))
    return f"{number:g}"
