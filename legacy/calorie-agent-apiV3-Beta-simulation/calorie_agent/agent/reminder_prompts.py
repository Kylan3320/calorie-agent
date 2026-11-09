from __future__ import annotations

from typing import Any


def compose_reminder_text(candidate: dict[str, Any]) -> str:
    reminder_type = str(candidate.get("reminder_type") or "")
    facts = candidate.get("facts") if isinstance(candidate.get("facts"), dict) else {}
    if reminder_type == "meal_missing":
        meal = _meal_label(facts.get("meal_type"))
        return f"今天还没有看到{meal}记录。需要我帮你补记吗？"
    if reminder_type == "target_gap":
        protein = _num(facts.get("protein"))
        target = _num(facts.get("target_protein"))
        gap = _num(facts.get("protein_gap"))
        if gap > 0:
            return f"今天蛋白质目前 {protein:g}g，距离目标 {target:g}g 还差 {gap:g}g。晚些时候可以优先补充高蛋白食物。"
        kcal_remaining = _num(facts.get("kcal_remaining"))
        return f"今天热量还剩 {kcal_remaining:g} kcal，如果有漏记可以直接补充。"
    if reminder_type == "anomaly":
        anomaly_type = str(facts.get("anomaly_type") or "")
        if anomaly_type == "daily_kcal_over":
            return f"今天热量已到 { _num(facts.get('kcal')):g} kcal，明显高于目标 { _num(facts.get('target_kcal')):g} kcal。若有录错，可以直接说撤销或修改。"
        if anomaly_type == "protein_gap":
            return f"今天蛋白质还差 { _num(facts.get('gap')):g}g，晚些时候可以留意补足。"
        if anomaly_type == "possible_duplicate":
            return f"刚才的 {facts.get('food_name', '记录')} 看起来可能重复了。确认录错时，可以直接说撤销上一条。"
        if anomaly_type == "food_grams_high":
            return f"刚才的 {facts.get('food_name', '食物')} 克重是 { _num(facts.get('grams')):g}g，明显偏大。若填错，可以直接说撤销或改成正确克重。"
        return "刚才的摄入记录看起来偏离平时较多。若填错，可以直接说撤销或修改。"
    if reminder_type == "pending":
        food = str(facts.get("food_name") or "待补全记录")
        grams = facts.get("grams")
        grams_text = f"{_num(grams):g}g" if grams not in {None, ""} else ""
        return f"你还有一个“{food}{grams_text}”待补全记录。可以补充克重或每 100g 碳水/蛋白质/脂肪，或说取消这个。"
    if reminder_type == "daily_summary":
        kcal = _num(facts.get("kcal"))
        target = _num(facts.get("target_kcal"))
        protein_gap = _num(facts.get("protein_gap"))
        if protein_gap > 0:
            return f"今日累计 {kcal:g} kcal，目标 {target:g} kcal。蛋白质还差 {protein_gap:g}g。"
        return f"今日累计 {kcal:g} kcal，目标 {target:g} kcal。"
    if reminder_type == "inactivity":
        days = _num(facts.get("days"))
        return f"最近 {days:g} 天没有看到新的饮食记录。需要我帮你补记吗？"
    return str(candidate.get("message_text") or "需要我提醒你补充饮食记录吗？")


def _meal_label(value: Any) -> str:
    labels = {
        "breakfast": "早餐",
        "lunch": "午餐",
        "dinner": "晚餐",
        "post_workout": "练后",
        "snack": "加餐",
    }
    return labels.get(str(value or ""), "这餐")


def _num(value: Any) -> float:
    try:
        return round(float(value), 1)
    except (TypeError, ValueError):
        return 0.0
