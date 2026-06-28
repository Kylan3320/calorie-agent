from __future__ import annotations

from datetime import date, timedelta
from types import ModuleType
from typing import Any

from .. import legacy_app
from . import report_cache, targets, trends


REPORT_FACT_VERSION = "report_facts.v1"
REPORT_TOOLS = {
    "generate_daily_report",
    "generate_weekly_report",
    "generate_monthly_report",
    "query_trend",
    "compare_period",
    "analyze_target_gap",
    "get_report",
    "regenerate_report",
}


def reporting_enabled(settings: dict[str, str]) -> bool:
    return _truthy(settings.get("reporting_enabled", "true"))


def generate_report(
    message: dict[str, str],
    report_type: str,
    params: dict[str, Any] | None = None,
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    settings = legacy._settings()
    if not reporting_enabled(settings):
        return {"status": "disabled", "reason": "reporting_disabled"}
    params = dict(params or {})
    params["report_type"] = report_type
    force = _truthy(params.get("force_regenerate") or params.get("force"))
    period = trends.resolve_period(params, settings, legacy)
    report_type = str(period.get("report_type") or report_type)
    cacheable = _cacheable_period(report_type, period, legacy)

    if not force and cacheable:
        cached = report_cache.get_cached_report(message, report_type, period, settings, legacy)
        if cached.get("status") == "hit":
            report = _report_from_cached(cached["report"])
            return {
                "status": "cache_hit",
                "report": report,
                "report_type": report_type,
                "period": report.get("period", trends._period_payload(period)),
                "summary_text": report.get("summary_text", ""),
                "facts": report.get("facts", {}),
                "cache_result": cached,
            }

    facts = _build_report_facts(message, report_type, period, settings, legacy)
    summary_text = format_report_summary(facts)
    report = {
        "report_id": report_cache._report_id(str(message.get("user_id") or ""), report_type, facts["period"]["start_date"], facts["period"]["end_date"], legacy),
        "report_type": report_type,
        "period": facts["period"],
        "summary_text": summary_text,
        "facts": facts,
        "source": "agent_runtime",
        "version": REPORT_FACT_VERSION,
    }
    cache_result = (
        report_cache.save_report(message, report, settings, legacy)
        if cacheable
        else {"status": "skipped", "reason": "current_daily_report_not_cached"}
    )
    return {
        "status": "generated",
        "report": report,
        "report_type": report_type,
        "period": facts["period"],
        "summary_text": summary_text,
        "facts": facts,
        "cache_result": cache_result,
    }


def query_trend(
    message: dict[str, str],
    params: dict[str, Any] | None = None,
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    params = dict(params or {})
    report_type = str(params.get("report_type") or "")
    if not report_type:
        period = str(params.get("period") or params.get("range") or "last_7_days")
        report_type = "monthly" if "month" in period else "weekly"
    result = generate_report(message, report_type, params, legacy)
    result["trend_query_used"] = True
    result["metric"] = str(params.get("metric") or "")
    if result.get("facts"):
        result["summary_text"] = format_trend_summary(result["facts"], result["metric"])
        result["report"]["summary_text"] = result["summary_text"]
    return result


def compare_period(
    message: dict[str, str],
    params: dict[str, Any] | None = None,
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    settings = legacy._settings()
    if not reporting_enabled(settings):
        return {"status": "disabled", "reason": "reporting_disabled"}
    params = dict(params or {})
    current_params = dict(params)
    current_params["report_type"] = str(params.get("report_type") or "weekly")
    current_period = trends.resolve_period(current_params, settings, legacy)
    previous_period = _resolve_previous_period(params, current_period, settings, legacy)
    current = trends.build_trend_facts(message, current_period, settings, legacy)
    previous = trends.build_trend_facts(message, previous_period, settings, legacy)
    comparison = trends.compare_trend_facts(current, previous)
    facts = {
        "version": REPORT_FACT_VERSION,
        "report_type": "compare",
        "metric": str(params.get("metric") or "kcal"),
        "period": current["period"],
        "previous_period": previous["period"],
        "comparison": comparison,
        "generated_at": legacy._now_millis(),
    }
    summary_text = format_compare_summary(facts)
    return {
        "status": "generated",
        "report_type": "compare",
        "period": facts["period"],
        "previous_period": facts["previous_period"],
        "summary_text": summary_text,
        "facts": facts,
        "trend_query_used": True,
    }


def analyze_target_gap(
    message: dict[str, str],
    params: dict[str, Any] | None = None,
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    params = dict(params or {})
    params.setdefault("period", "this_week")
    params.setdefault("report_type", "weekly")
    result = generate_report(message, str(params.get("report_type") or "weekly"), params, legacy)
    result["metric"] = str(params.get("metric") or "")
    if result.get("facts"):
        result["summary_text"] = format_target_gap_summary(result["facts"], result["metric"])
        result["report"]["summary_text"] = result["summary_text"]
    return result


def get_report(
    message: dict[str, str],
    params: dict[str, Any] | None = None,
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    settings = legacy._settings()
    cached = report_cache.get_report(message, params or {}, settings, legacy)
    if cached.get("status") == "hit":
        report = _report_from_cached(cached["report"])
        return {"status": "cache_hit", "report": report, "summary_text": report.get("summary_text", ""), "facts": report.get("facts", {})}
    return cached


def regenerate_report(
    message: dict[str, str],
    report_type: str,
    params: dict[str, Any] | None = None,
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    params = {**(params or {}), "force_regenerate": True}
    return generate_report(message, report_type, params, legacy)


def _build_report_facts(
    message: dict[str, str],
    report_type: str,
    period: dict[str, Any],
    settings: dict[str, str],
    legacy: ModuleType,
) -> dict[str, Any]:
    trend = trends.build_trend_facts(message, period, settings, legacy)
    target = targets.build_target_facts(message, trend, settings, legacy)
    facts = {
        "version": REPORT_FACT_VERSION,
        "report_type": report_type,
        "period": trend["period"],
        "trend": trend,
        "targets": target,
        "suggestions": _suggestions(trend, target),
        "generated_at": legacy._now_millis(),
    }
    return facts


def _cacheable_period(report_type: str, period: dict[str, Any], legacy: ModuleType) -> bool:
    if report_type != "daily":
        return True
    today = legacy._today_string()
    start = period.get("start_date")
    end = period.get("end_date")
    start_text = start.isoformat() if hasattr(start, "isoformat") else str(start or "")
    end_text = end.isoformat() if hasattr(end, "isoformat") else str(end or "")
    return not (start_text == today and end_text == today)


def format_report_summary(facts: dict[str, Any]) -> str:
    trend = facts.get("trend") if isinstance(facts.get("trend"), dict) else {}
    targets_facts = facts.get("targets") if isinstance(facts.get("targets"), dict) else {}
    recorded_days = int(trend.get("recorded_day_count") or 0)
    if recorded_days <= 0:
        return "本周期没有有效摄入记录，不能按 0 kcal 判断。"

    report_type = str(facts.get("report_type") or "")
    totals = trend.get("totals") if isinstance(trend.get("totals"), dict) else {}
    averages = trend.get("averages_recorded_days") if isinstance(trend.get("averages_recorded_days"), dict) else {}
    rates = targets_facts.get("hit_rates") if isinstance(targets_facts.get("hit_rates"), dict) else {}
    suggestions = facts.get("suggestions") if isinstance(facts.get("suggestions"), list) else []
    top = trend.get("top_foods") if isinstance(trend.get("top_foods"), list) else []
    top_text = _top_food_text(top)
    if report_type == "daily":
        line = (
            f"日报：累计 {_fmt(totals.get('kcal'))} kcal，"
            f"碳水 {_fmt(totals.get('carbs'))}g，蛋白 {_fmt(totals.get('protein'))}g，脂肪 {_fmt(totals.get('fat'))}g。"
        )
    elif report_type == "monthly":
        line = (
            f"月报：有记录 {recorded_days} 天，记录日均 {_fmt(averages.get('kcal'))} kcal，"
            f"蛋白日均 {_fmt(averages.get('protein'))}g，热量达标率 {_fmt(rates.get('kcal'))}%。"
        )
    else:
        line = (
            f"周报：有记录 {recorded_days} 天，记录日均 {_fmt(averages.get('kcal'))} kcal，"
            f"蛋白日均 {_fmt(averages.get('protein'))}g，热量达标率 {_fmt(rates.get('kcal'))}%。"
        )
    detail = f"主要食物：{top_text}。" if top_text else ""
    advice = f"建议：{suggestions[0]}" if suggestions else ""
    return "\n".join(part for part in [line, detail, advice] if part)


def format_trend_summary(facts: dict[str, Any], metric: str = "") -> str:
    trend = facts.get("trend") if isinstance(facts.get("trend"), dict) else {}
    metric = metric if metric in trends.METRICS else "kcal"
    recorded_days = int(trend.get("recorded_day_count") or 0)
    if recorded_days <= 0:
        return "这段时间没有有效摄入记录，不能判断趋势。"
    averages = trend.get("averages_recorded_days") if isinstance(trend.get("averages_recorded_days"), dict) else {}
    volatility = trend.get("volatility") if isinstance(trend.get("volatility"), dict) else {}
    return f"趋势：有记录 {recorded_days} 天，{_metric_label(metric)}记录日均 {_fmt(averages.get(metric))}{_metric_unit(metric)}，波动 {_fmt(volatility.get(metric))}{_metric_unit(metric)}。"


def format_compare_summary(facts: dict[str, Any]) -> str:
    comparison = facts.get("comparison") if isinstance(facts.get("comparison"), dict) else {}
    deltas = comparison.get("average_deltas") if isinstance(comparison.get("average_deltas"), dict) else {}
    metric = str(facts.get("metric") or "kcal")
    if metric not in trends.METRICS:
        metric = "kcal"
    delta = deltas.get(metric)
    if delta is None:
        return "对比周期里有效记录不足，暂时不能判断变化。"
    direction = "更高" if float(delta) > 0 else "更低" if float(delta) < 0 else "基本持平"
    return f"对比结果：本周期{_metric_label(metric)}记录日均比上一周期{direction} {_fmt(abs(float(delta)))}{_metric_unit(metric)}。"


def format_target_gap_summary(facts: dict[str, Any], metric: str = "") -> str:
    target = facts.get("targets") if isinstance(facts.get("targets"), dict) else {}
    metric = metric if metric in trends.METRICS else "kcal"
    recorded_days = int(target.get("recorded_day_count") or 0)
    if recorded_days <= 0:
        return "这段时间没有有效摄入记录，不能判断达标情况。"
    rates = target.get("hit_rates") if isinstance(target.get("hit_rates"), dict) else {}
    gaps = target.get("average_gaps") if isinstance(target.get("average_gaps"), dict) else {}
    return f"达标分析：有记录 {recorded_days} 天，{_metric_label(metric)}达标率 {_fmt(rates.get(metric))}%，记录日均偏差 {_fmt(gaps.get(metric))}{_metric_unit(metric)}。"


def _resolve_previous_period(
    params: dict[str, Any],
    current_period: dict[str, Any],
    settings: dict[str, str],
    legacy: ModuleType,
) -> dict[str, Any]:
    compare_to = params.get("compare_to") or params.get("previous_period")
    if compare_to:
        return trends.resolve_period({"period": compare_to, "report_type": current_period.get("report_type")}, settings, legacy)
    start = current_period["start_date"]
    end = current_period["end_date"]
    days = (end - start).days + 1
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)
    return {
        "report_type": current_period.get("report_type") or "custom",
        "period_key": "previous_period",
        "start_date": previous_start,
        "end_date": previous_end,
    }


def _report_from_cached(row: dict[str, Any]) -> dict[str, Any]:
    facts = row.get("facts") if isinstance(row.get("facts"), dict) else {}
    period = facts.get("period") if isinstance(facts.get("period"), dict) else {
        "start_date": row.get("period_start"),
        "end_date": row.get("period_end"),
    }
    return {
        "report_id": row.get("report_id", ""),
        "report_type": row.get("report_type", ""),
        "period": period,
        "summary_text": row.get("summary_text", ""),
        "facts": facts,
        "source": row.get("source", "cache"),
        "version": row.get("version", ""),
    }


def _suggestions(trend: dict[str, Any], target: dict[str, Any]) -> list[str]:
    if int(trend.get("recorded_day_count") or 0) <= 0:
        return ["先保持记录完整，再判断摄入趋势。"]
    rates = target.get("hit_rates") if isinstance(target.get("hit_rates"), dict) else {}
    gaps = target.get("average_gaps") if isinstance(target.get("average_gaps"), dict) else {}
    if rates.get("protein") is not None and float(rates.get("protein") or 0) < 60:
        return ["蛋白质达标率偏低，优先补稳定的高蛋白食物。"]
    if gaps.get("kcal") is not None and float(gaps.get("kcal") or 0) > 200:
        return ["热量持续偏高，先检查主食和高脂调料的份量。"]
    if gaps.get("kcal") is not None and float(gaps.get("kcal") or 0) < -200:
        return ["热量持续偏低，注意不要用漏记代替低摄入。"]
    return ["当前摄入整体可控，继续保持记录完整。"]


def _top_food_text(items: list[Any]) -> str:
    names = []
    for item in items[:3]:
        if isinstance(item, dict) and item.get("food_name"):
            names.append(f"{item['food_name']} {_fmt(item.get('grams'))}g")
    return "、".join(names)


def _metric_label(metric: str) -> str:
    return {"kcal": "热量", "carbs": "碳水", "protein": "蛋白质", "fat": "脂肪"}.get(metric, metric)


def _metric_unit(metric: str) -> str:
    return " kcal" if metric == "kcal" else "g"


def _fmt(value: Any) -> str:
    try:
        return f"{round(float(value) + 1e-9, 1):g}"
    except (TypeError, ValueError):
        return "0"


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}
