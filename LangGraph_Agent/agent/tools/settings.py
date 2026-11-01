"""Explicit user settings and fixed, session-bound proposals."""
from copy import deepcopy
import re
from .meals import from_row, select_rows, resolve_date
from .base import positive_grams
from ..storage.bitable_adapter import stable_uuid

BARE_CONFIRM = {"确认", "好的", "好", "可以", "是", "yes", "ok"}
BARE_CANCEL = {"取消", "算了", "不要", "不确认"}

def update_user_settings(rt, p):
    if rt.prepared is None:
        settings = deepcopy(rt.settings)
        kind = p["kind"]
        values = deepcopy(p.get("values", {}))
        if kind == "targets":
            if not values or set(values) - {"carbs", "protein", "fat", "kcal"}:
                raise ValueError("targets require specified nutrient fields")
            values = {k: positive_grams(v) for k,v in values.items()}
            if p.get("scope") == "today":
                target = settings.setdefault("daily_targets", {}).setdefault(rt.day, {})
            else:
                target = settings.setdefault("targets", {})
            target.update(values)
        elif kind == "preferences":
            settings.setdefault("preferences", {}).update(values)
        elif kind == "portion":
            food_query = values.get("food_query", "")
            from .meals import exact_food
            food = exact_food(food_query, rt.store.load_foods())
            if food is None or not values.get("unit") or not positive_grams(values.get("eat_weight")):
                return {"status": "pending", "data": {"question": "请明确食物、单位及每单位可食克重"}}
            settings.setdefault("portions", {})[food["food_id"] + ":" + values["unit"]] = {
                "eat_weight": float(values["eat_weight"]), "source": "user_confirmed"}
        elif kind == "favorite":
            name = str(values.get("name", "")).strip()
            if not name:
                return {"status": "pending", "data": {"question": "请给常用餐命名"}}
            favorites = settings.setdefault("favorites", {})
            if values.get("remove"):
                favorites.pop(name, None)
            else:
                rows = select_rows(rt, values.get("record_ids", []), latest=not values.get("record_ids"))
                if not rows:
                    return {"status": "pending", "data": {"question": "请明确要保存的餐食"}}
                favorites[name] = [from_row(r) for r in rows]
        elif kind == "reminder":
            rule_id = str(values.get("rule_id") or stable_uuid(f"reminder:{rt.user_id}:{values.get('meal_type', '')}"))
            rules = settings.setdefault("reminders", {})
            rule = {**rules.get(rule_id, {}), **values, "rule_id": rule_id}
            rule.setdefault("enabled", False)
            rule.setdefault("weekdays", list(range(7)))
            missing = []
            if rule["enabled"]:
                if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(rule.get("time", ""))):
                    missing.append("提醒时间 HH:MM")
                if rule.get("meal_type") not in {"breakfast", "lunch", "dinner", "snack"}:
                    missing.append("餐次")
                if not settings.get("feishu_chat_id"):
                    missing.append("先与飞书机器人发起会话")
                if not rule["weekdays"] or any(type(d) is not int or d not in range(7) for d in rule["weekdays"]):
                    raise ValueError("weekdays must contain Monday=0 through Sunday=6")
            if missing:
                rule["enabled"] = False
                rules[rule_id] = rule
                rt.prepare({"settings": settings, "original_settings": deepcopy(rt.settings), "kind": kind, "values": rule, "missing": missing})
            else:
                rules[rule_id] = rule
        else:
            raise ValueError("unsupported settings kind")
        if rt.prepared is None:
            rt.prepare({"settings": settings, "original_settings": deepcopy(rt.settings), "kind": kind, "values": values, "missing": []})
    rt.check_deadline()
    current = rt.store.load_document("user_settings", rt.user_id)
    original = rt.prepared.get("original_settings", rt.settings)
    if {k:v for k,v in current.items() if k != "feishu_chat_id"} != {k:v for k,v in original.items() if k != "feishu_chat_id"}:
        raise ValueError("prepared settings have changed; start a new request")
    rt.settings = deepcopy(rt.prepared["settings"])
    if "feishu_chat_id" in current:
        rt.settings["feishu_chat_id"] = current["feishu_chat_id"]
    rt.store.save_document("user_settings", rt.user_id, "", rt.settings)
    return {"status": "pending" if rt.prepared["missing"] else "ok", "data": {
        "kind": rt.prepared["kind"], "values": rt.prepared["values"], "missing": rt.prepared["missing"],
        "effective_targets": rt.store.get_standard(rt.user_id)}}
