"""Meal resolution, portions and snapshot operations; all I/O goes through Store."""
from copy import deepcopy
from datetime import date, timedelta
import re
from ..storage import boohee_client
from ..storage.food_match import normalize_food_name, match_food_candidates
from ..storage.bitable_adapter import stable_uuid, normalize_food_key
from ..storage.timeutil import round_number
from .base import calculate_food_intake, positive_grams

NUTRIENTS = ("carbs", "protein", "fat", "kcal")

def totals(items):
    return {k: round_number(sum(i["nutrition"][k] for i in items)) for k in NUTRIENTS}

def resolve_date(value, anchor):
    if value in (None, "", "today", "今天"):
        return anchor
    if value in ("yesterday", "昨天"):
        return (date.fromisoformat(anchor) - timedelta(days=1)).isoformat()
    return date.fromisoformat(value).isoformat()

def exact_food(query, foods, unit=""):
    key = normalize_food_name(query)
    exact = [f for f in foods if key in [normalize_food_name(f["name"]),
                *[normalize_food_name(a) for a in f.get("aliases", [])]]]
    if exact:
        return exact[0] if len(exact) == 1 else None
    # Models can repeat a portion's size in the food name. Only remove that
    # suffix when the selected unit and the food's own source units prove it.
    suffix = re.fullmatch(r"(.+?)[（(]([^()（）]+)[）)]", query.strip())
    unit_suffix = re.fullmatch(r".+?[（(]([^()（）]+)[）)]", unit.strip())
    if not suffix or not unit_suffix:
        return None
    qualifier = normalize_food_name(suffix[2])
    size = normalize_food_name(unit_suffix[1])
    if qualifier not in (size, size + "号"):
        return None
    food = exact_food(suffix[1], foods)
    matches = [u for u in (food or {}).get("units", [])
               if normalize_food_name(u.get("unit_name", u.get("unit", u.get("name", "")))) == normalize_food_name(unit)]
    return food if len(matches) == 1 else None

def resolve_item(rt, item, *, pending=None):
    """Never accepts model-generated nutrition or unit weights."""
    item = deepcopy(item)
    query = item["food_query"]
    candidates = deepcopy((pending or {}).get("candidates", []))
    food = deepcopy((pending or {}).get("food"))
    if item.get("choice") is not None:
        if pending and pending.get("offered_message_id") == rt.message["message_id"]:
            return None, {**pending, "reason":"请用户选择食物候选", "choice":None}
        n = item["choice"] - 1
        if not pending or not 0 <= n < len(candidates):
            raise ValueError("choice must refer to displayed pending candidates")
        food = deepcopy(candidates[n])
        if (normalize_food_name(query) != normalize_food_name(pending["food_query"])
                and exact_food(query, [food], item.get("unit", "")) is None):
            raise ValueError("food query conflicts with the selected pending candidate")
    if food is None:
        foods = rt.store.load_foods()
        food = exact_food(query, foods, item.get("unit", ""))
        if food is None:
            candidates = [c.food for c in match_food_candidates(query, foods)]
            try:
                remote = boohee_client.search_food(query)
                candidates.extend(f for f in remote if f.get("boohee_id") not in
                                  {c.get("boohee_id") for c in candidates if c.get("boohee_id")})
            except boohee_client.BooheeError as e:
                item["source_error"] = str(e)
            food = exact_food(query, candidates, item.get("unit", ""))
        if food is None:
            return None, {**item, "candidates": candidates, "reason": "请选择食物候选" if candidates else "缺可核实的食物营养资料"}
    food.setdefault("food_id", stable_uuid(f"calorie-agent:food:{normalize_food_key(food['name'])}"))
    units = food.get("units", [])
    error = ""
    if item.get("grams") is None and not units and not food.get("boohee_id"):
        personal_key = food["food_id"] + ":" + item.get("unit", "")
        if personal_key not in rt.settings.get("portions", {}):
            try:
                remote = exact_food(food["name"], boohee_client.search_food(food["name"]))
                if remote:
                    food.update({**remote, "food_id": food["food_id"]})
            except boohee_client.BooheeError as e:
                error = str(e)
    if food.get("boohee_id") and not food.get("detail_loaded"):
        try:
            food.update(boohee_client.food_detail(food["boohee_id"]))
            units = food.get("units", [])
            if rt.preset.allow_write:
                rt.check_deadline()
                rt.store.cache_food(food)
        except (boohee_client.BooheeError, OSError, TimeoutError) as e:
            error = str(e)
    grams = positive_grams(item.get("grams"))
    estimated = False
    basis = "user_edible_grams"
    if grams is None:
        quantity = positive_grams(item.get("quantity"))
        unit = item.get("unit", "")
        personal = rt.settings.get("portions", {}).get(food["food_id"] + ":" + unit)
        if quantity and personal:
            grams, basis = quantity * personal["eat_weight"], "confirmed_personal_portion"
        elif quantity:
            matches = [u for u in units if normalize_food_name(u.get("unit_name", u.get("unit", u.get("name", "")))) == normalize_food_name(unit)]
            if len(matches) == 1 and positive_grams(matches[0].get("eat_weight")):
                grams, basis = quantity * float(matches[0]["eat_weight"]), "boohee_eat_weight"
        estimated = grams is not None
    if grams is None:
        return None, {**item, "food": food, "candidates": candidates, "reason": "请补充可食克重或有依据的份量", "source_error": error}
    snapshot = {"food": food, "quantity_text": item.get("quantity_text", ""),
                "quantity": item.get("quantity"), "unit": item.get("unit", ""),
                "basis": basis, "estimated": estimated,
                "kcal_source": "source" if food.get("kcal_per_100g") is not None else "macro_formula"}
    return {"food_id": food["food_id"], "matched_name": food["name"], "grams": round_number(grams),
            "nutrition": calculate_food_intake(food, grams), "snapshot": snapshot}, None

def from_row(row):
    snap = deepcopy(row.get("snapshot", {}))
    if not snap.get("food"):
        # Legacy intake snapshots can be scaled without changing their original values.
        grams = positive_grams(row["grams"])
        snap["food"] = {"name": row["food_name"], "food_id": row.get("food_id", ""),
            **{f"{k}_per_100g": row["nutrition"][k] * 100 / grams for k in NUTRIENTS}}
        snap["basis"] = "legacy_intake_snapshot"
    return dict(food_id=row.get("food_id", ""), matched_name=row["food_name"],
                grams=row["grams"], nutrition=deepcopy(row["nutrition"]), snapshot=snap)

def scale_item(item, ratio):
    ratio = positive_grams(ratio)
    scaled = deepcopy(item)
    scaled["grams"] = round_number(item["grams"] * ratio)
    scaled["nutrition"] = calculate_food_intake(scaled["snapshot"]["food"], scaled["grams"])
    scaled["snapshot"]["reuse_ratio"] = ratio
    return scaled

def select_rows(rt, refs, *, day="", meal_type="", latest=False):
    target_ids = refs or (rt.session.get("last_meal", []) if latest else [])
    rows = (rt.store.intake_by_ids(rt.user_id, target_ids) if target_ids and hasattr(rt.store, "intake_by_ids")
            else rt.store.intake_items(rt.user_id, day) if day else rt.store.intake_items(rt.user_id))
    known = set(rt.session.get("refs", []))
    if refs:
        # An opaque target must have been returned to this session by a tool.
        if any(ref not in known for ref in refs):
            raise ValueError("target reference has not been shown in this session")
        rows = [r for r in rows if r["record_id"] in refs]
    elif latest:
        rows = [r for r in rows if r["record_id"] in rt.session.get("last_meal", [])]
    if day:
        rows = [r for r in rows if r["date"] == day]
    if meal_type:
        rows = [r for r in rows if r["meal_type"] == meal_type]
    return rows

def meal_totals(rt, items):
    keys = {i.get("snapshot", {}).get("meal_id", i.get("message_id", "")) for i in items}
    dates = sorted({i.get("date", rt.day) for i in items})
    from datetime import date, timedelta
    rows = rt.store.intake_items(rt.user_id, dates[0], (date.fromisoformat(dates[-1])+timedelta(days=1)).isoformat()) if dates else []
    result = []
    for key in sorted(keys - {""}):
        group = [r for r in rows if r.get("snapshot", {}).get("meal_id", r.get("message_id", "")) == key]
        source = next((r for r in items if r.get("snapshot", {}).get("meal_id", r.get("message_id", "")) == key), {})
        result.append({"meal_id": key, "date": source.get("date", rt.day), "meal_type": source.get("meal_type", "unknown"),
                       "count": len(group), "estimated": any(r.get("snapshot", {}).get("estimated") for r in group), "total": totals(group)})
    return result


def record_meal(rt, p):
    if rt.prepared is None:
        session = deepcopy(rt.session)
        pending_items = session.setdefault("unfinished", {})
        cancelled = []
        if p.get("cancel_pending"):
            for key in p["cancel_pending"]:
                if key in pending_items:
                    pending_items.pop(key)
                    cancelled.append(key)
        day = resolve_date(p.get("date"), rt.day)
        meal = p.get("meal_type") or "unknown"
        items, missing, completed_ids = [], [], []
        reuse = p.get("reuse")
        if reuse:
            if reuse.get("favorite"):
                items = deepcopy(rt.settings.get("favorites", {}).get(reuse["favorite"], []))
            else:
                source_day = ""
                if reuse.get("date") or not (reuse.get("latest") or reuse.get("record_ids")):
                    source_day = resolve_date(reuse.get("date"), rt.day)
                rows = select_rows(rt, reuse.get("record_ids", []), day=source_day,
                                   meal_type=reuse.get("meal_type", ""), latest=reuse.get("latest", False))
                batches = {r.get("snapshot", {}).get("meal_id", r["message_id"]) for r in rows}
                if len(batches) > 1 and not reuse.get("record_ids"):
                    return {"status": "pending", "data": {"question": "该餐有多个批次，请选择", "items": rows}}
                items = [from_row(r) for r in rows]
            if not items:
                return {"status": "pending", "data": {"question": "未找到可复用的餐食"}}
            items = [scale_item(i, reuse.get("ratio", 1)) for i in items]
        for index, entry in enumerate(p.get("items", [])):
            pid = entry.get("pending_id")
            original = pending_items.get(pid) if pid else None
            if pid and original is None:
                raise ValueError("unfinished item unavailable in this session")
            previous = deepcopy(original or {})
            if original:
                same_query = normalize_food_name(entry["food_query"]) == normalize_food_name(original["food_query"])
                known_food = original.get("food")
                if (not same_query and entry.get("choice") is None
                        and not (known_food and exact_food(entry["food_query"], [known_food], entry.get("unit", "")))):
                    for key in ("food", "candidates", "choice", "offered_message_id"):
                        previous.pop(key, None)
                if entry.get("grams") is not None:
                    for key in ("quantity", "unit", "quantity_text"):
                        previous.pop(key, None)
                elif entry.get("quantity") is not None or entry.get("unit"):
                    previous.pop("grams", None)
                    previous.pop("quantity_text", None)
            merged = {**previous, **{k:v for k,v in entry.items() if v is not None}}
            resolved, unfinished = resolve_item(rt, merged, pending=previous if original else None)
            entry_day = resolve_date(p.get("date"), rt.day) if p.get("date") else (original or {}).get("date", day)
            entry_meal = p.get("meal_type") or (original or {}).get("meal_type", meal)
            if resolved:
                resolved["snapshot"]["meal_id"] = (original or {}).get("meal_id", rt.action_message["message_id"])
                items.append({**resolved, "date": entry_day, "meal_type": entry_meal})
                if pid:
                    completed_ids.append(pid)
            else:
                pid = pid or f"{rt.action_id}:item:{index + 1}"
                offered = (original or {}).get("offered_message_id")
                if not original or unfinished.get("candidates", []) != original.get("candidates", []):
                    offered = rt.message["message_id"]
                pending_items[pid] = {**unfinished, "pending_id": pid, "date": entry_day, "meal_type": entry_meal,
                                      "offered_message_id": offered,
                                      "meal_id": (original or {}).get("meal_id", rt.action_message["message_id"]),
                                      "raw_text": (original or {}).get("raw_text", rt.message["text"])}
                missing.append(pending_items[pid])
        for pid in completed_ids:
            pending_items.pop(pid, None)
        for i in items:
            i.setdefault("date", day)
            i.setdefault("meal_type", meal)
            if reuse:
                i["snapshot"]["meal_id"] = rt.action_message["message_id"]
        rt.prepare({"items": items, "session": session, "missing": list(pending_items.values()), "cancelled": cancelled})
    prepared = rt.prepared
    items = prepared["items"]
    rt.check_deadline()
    write = rt.store.write_intake(rt.action_message, items, "unknown")
    rows = (rt.store.intake_for_action(rt.user_id, rt.action_message["message_id"]) if hasattr(rt.store, "intake_for_action") else
            [r for r in rt.store.intake_items(rt.user_id, include_deleted=True)
             if r["message_id"] == rt.action_message["message_id"]])
    ids = [r["record_id"] for r in rows]
    if items and len(ids) != len(items):
        raise RuntimeError("intake readback does not match prepared batch")
    session = deepcopy(prepared["session"])
    session["refs"] = list(dict.fromkeys(session.get("refs", []) + ids))
    if ids:
        session["last_meal"] = ids
    rt.session = session
    written = [{**from_row(r), **{k:r[k] for k in ("record_id","message_id","date","meal_type")}} for r in rows]
    return {"status": "ok" if items or prepared["cancelled"] else "pending", "data": {
        "items": written, "total": totals(written),
        "meal_totals": meal_totals(rt, written),
        "completed_count": len(items), "unfinished": prepared["missing"], "cancelled": prepared["cancelled"],
        "write_status": write["status"]}}

def revise_intake(rt, p):
    if rt.prepared is None:
        rows = select_rows(rt, [p["record_id"]] if p.get("record_id") else [], latest=not p.get("record_id"))
        if p.get("food_query"):
            rows = [r for r in rows if normalize_food_name(r["food_name"]) == normalize_food_name(p["food_query"])]
        if len(rows) != 1:
            return {"status": "pending", "data": {"question": "请明确要修改的摄入项", "items": rows}}
        row = rows[0]
        item = None
        if not p.get("delete"):
            item = from_row(row)
            if p.get("ratio") is not None:
                item = scale_item(item, p["ratio"])
            else:
                item, missing = resolve_item(rt, {"food_query": row["food_name"], **p}, pending={"food": item["snapshot"]["food"]})
                if missing:
                    return {"status": "pending", "data": {"unfinished": [missing]}}
            item["snapshot"]["meal_id"] = row.get("snapshot", {}).get("meal_id", row["message_id"])
        rt.prepare({"record_id": row["record_id"], "item": item, "date": row["date"], "original": row})
    plan = rt.prepared
    rt.check_deadline()
    current = next((r for r in rt.store.intake_items(rt.user_id, plan["date"], include_deleted=True)
                    if r["record_id"] == plan["record_id"]), None)
    original = plan["original"]
    if current is None or any(current.get(k) != original.get(k) for k in
                              ("agent_revision", "grams", "nutrition", "snapshot", "status")):
        raise ValueError("prepared intake target has changed; start a new request")
    result = rt.store.revise_intake(rt.user_id, plan["record_id"], plan["item"])
    return {"status": "ok", "data": {"operation": "deleted" if plan["item"] is None else "revised", "item": result, "meal_totals": meal_totals(rt, [plan["original"]])}}

def calculate_meal(rt, p):
    items, unfinished = [], []
    for item in p["items"]:
        resolved, missing = resolve_item(rt, item)
        if resolved:
            items.append(resolved)
        if missing:
            unfinished.append(missing)
    total = totals(items)
    standard = rt.store.get_standard(rt.user_id)
    consumed = rt.store.today_summary(rt.user_id)["totals"]
    return {"status": "pending" if unfinished else "ok", "data": {
        "items": items, "total": total, "unfinished": unfinished, "date": rt.day,
        "budget_difference": {k: round_number(standard[k] - consumed[k] - total[k]) for k in NUTRIENTS}}}
