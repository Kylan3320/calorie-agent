"""Meal resolution, portions and snapshot operations; all I/O goes through Store."""
from copy import deepcopy
from datetime import date, timedelta
import math
import re
from ..storage import boohee_client
from ..storage.food_match import normalize_food_name, match_food_candidates
from ..storage.bitable_adapter import stable_uuid, normalize_food_key, _is_valid_intake_status
from ..storage.timeutil import round_number
from .base import calculate_food_intake, positive_grams
from .portions import allow_estimate, cache_portion_evidence, personal_portion, portion_cache_refs, resolve_web_portion

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

def _has_macros(food):
    return all(food.get(key) is not None and math.isfinite(float(food[key])) and float(food[key]) >= 0
               for key in ("carbs_per_100g", "protein_per_100g", "fat_per_100g"))


def _missing(item, code, detail, *, requires_user=False, **facts):
    condition = {"code": code, "detail": detail}
    if requires_user:
        condition["requires_user"] = True
    return {**item, **facts, "reason": detail, "unmet_conditions": [condition]}


def resolve_item(rt, item, *, pending=None, portion_lookup=False, estimate=True):
    """Never accepts model-generated nutrition or unit weights."""
    item = deepcopy(item)
    evidence = item.pop("portion_evidence", None)
    query = item.get("food_query", "")
    portion_query = query
    quantity_text = item.get("quantity_text", "")
    if quantity_text not in rt.message.get("text", ""):
        quantity_text = ""
    candidates = deepcopy((pending or {}).get("candidates", []))
    food = deepcopy((pending or {}).get("food"))
    if portion_lookup and (ref := item.get("source_ref")):
        source = rt.session.get("food_sources", {}).get(ref)
        if not source:
            return None, _missing({**item, "food_query": query or "食物"}, "food_source_unavailable", "当前会话没有该食物来源引用")
        selected = source["food"]
        if query and not exact_food(query, [selected], item.get("unit", "")):
            return None, _missing(item, "food_source_mismatch", "食物名称与所选来源不一致")
        food = deepcopy(selected)
        query = item["food_query"] = selected["name"]
    if item.get("choice") is not None:
        if pending and pending.get("offered_message_id") == rt.message["message_id"]:
            return None, _missing(pending, "food_choice_not_presented", "候选编号尚未向用户展示", choice=None)
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
            return None, _missing(item, "food_identity_unverified" if candidates else "nutrition_source_missing",
                                  "食物身份尚未核实" if candidates else "缺可核实的食物营养资料", candidates=candidates)
    food.setdefault("food_id", stable_uuid(f"calorie-agent:food:{normalize_food_key(food['name'])}"))
    units = food.get("units", [])
    error = ""
    estimate = not portion_lookup or allow_estimate(rt, estimate)
    needs_portion = item.get("grams") is None and estimate and not evidence
    missing_macros = portion_lookup and not _has_macros(food)
    personal = personal_portion(rt.settings, food, item.get("unit", ""), portion_query, quantity_text) if estimate else None
    if (needs_portion and not units or missing_macros) and not food.get("boohee_id"):
        if missing_macros or not personal:
            try:
                remote = exact_food(food["name"], boohee_client.search_food(food["name"]))
                if remote:
                    food.update({**remote, "food_id": food["food_id"]})
                    units = food.get("units", [])
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
    portion_evidence = None
    portion_error = ""
    quantity = None
    if grams is None and estimate:
        quantity = positive_grams(item.get("quantity"))
        unit = item.get("unit", "")
        if quantity and personal:
            grams, basis = quantity * personal["eat_weight"], "confirmed_personal_portion"
        elif portion_lookup and evidence and quantity and _has_macros(food):
            try:
                grams, portion_evidence = resolve_web_portion(rt, food, item, quantity, evidence)
                basis = "web_edible_portion"
            except ValueError as exc:
                portion_error = str(exc)
        elif quantity:
            matches = [u for u in units if normalize_food_name(u.get("unit_name", u.get("unit", u.get("name", "")))) == normalize_food_name(unit)]
            if len(matches) == 1 and positive_grams(matches[0].get("eat_weight")):
                grams, basis = quantity * float(matches[0]["eat_weight"]), "boohee_eat_weight"
        estimated = grams is not None
    missing_macros = portion_lookup and not _has_macros(food)
    if grams is None or missing_macros:
        code, detail = (
            ("nutrition_source_incomplete", "缺可核实的完整食物营养资料") if missing_macros else
            ("measured_grams_missing", "本次要求精确，缺实测可食克重") if not estimate else
            ("portion_evidence_not_applicable", portion_error) if portion_error else
            ("portion_evidence_missing", "可食份量尚无足够依据") if quantity and item.get("unit") else
            ("portion_quantity_missing", "缺可核实的食用数量或单位"))
        missing = _missing(item, code, detail, requires_user=code == "measured_grams_missing",
                           food=food, candidates=candidates, source_error=error)
        if portion_lookup and estimate and quantity and item.get("unit") and not missing_macros:
            if cached := portion_cache_refs(rt, food, item["unit"]):
                missing["portion_cache_refs"] = cached
        return None, missing
    snapshot = {"food": food, "quantity_text": item.get("quantity_text", ""),
                "quantity": item.get("quantity"), "unit": item.get("unit", ""),
                "basis": basis, "estimated": estimated,
                "kcal_source": "source" if food.get("kcal_per_100g") is not None else "macro_formula"}
    if portion_evidence:
        snapshot["portion_evidence"] = {**portion_evidence, "adopted_grams": round_number(grams)}
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


def explicit_selection(rt, refs, *, kind, name="", day="", meal_type=""):
    """A named selection is atomic and ordered, unlike ordinary meal resolution."""
    known = set(rt.session.get("refs", []))
    rows = (rt.store.intake_by_ids(rt.user_id, refs, include_deleted=True) if hasattr(rt.store, "intake_by_ids")
            else rt.store.intake_items(rt.user_id, include_deleted=True))
    available = {r["record_id"]: r for r in rows}
    missing = []
    for ref in refs:
        row = available.get(ref)
        reason = ("不是本会话已返回的记录" if ref not in known else
                  "记录已删除或不可用" if row is None or not _is_valid_intake_status(row.get("status")) else
                  "记录不在指定日期或餐次" if (day and row["date"] != day) or
                  (meal_type and row["meal_type"] != meal_type) else "")
        if reason:
            missing.append({"record_id": ref, "reason": reason,
                            **({k: row[k] for k in ("food_name", "date", "meal_type") if k in row} if row else {})})
    if missing:
        selection = {"kind": kind, "record_ids": list(refs), "missing": missing}
        if name:
            selection["name"] = name
        rt.session["failed_selection"] = selection
        return [], {"status": "pending", "data": {
            "question": "所选记录有不可用项，本次未生效；请修订选择范围", "selection": selection}}
    rt.session.pop("failed_selection", None)
    return [available[ref] for ref in refs], None

def meal_totals(rt, items, *, rows=None):
    keys = {i.get("snapshot", {}).get("meal_id", i.get("message_id", "")) for i in items}
    dates = sorted({i.get("date", rt.day) for i in items})
    from datetime import date, timedelta
    if rows is None:
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
        cancelled_items = []
        if p.get("cancel_pending"):
            for key in p["cancel_pending"]:
                if key in pending_items:
                    cancelled_items.append(deepcopy(pending_items.pop(key)))
                    cancelled.append(key)
        day = resolve_date(p.get("date"), rt.day)
        meal = p.get("meal_type") or "unknown"
        items, cache_refs, completed_ids = [], {}, []
        reuse = p.get("reuse")
        if reuse:
            if reuse.get("favorite"):
                items = deepcopy(rt.settings.get("favorites", {}).get(reuse["favorite"], []))
            else:
                source_day = ""
                if reuse.get("date") or not (reuse.get("latest") or reuse.get("record_ids")):
                    source_day = resolve_date(reuse.get("date"), rt.day)
                if reuse.get("record_ids"):
                    rows, invalid = explicit_selection(rt, reuse["record_ids"], kind="reuse", day=source_day,
                                                       meal_type=reuse.get("meal_type", ""))
                    if invalid:
                        return invalid
                    session.pop("failed_selection", None)
                else:
                    rows = select_rows(rt, [], day=source_day,
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
            web_completion = original is not None and bool(entry.get("portion_evidence"))
            if original:
                query = entry.get("food_query") or original["food_query"]
                same_query = normalize_food_name(query) == normalize_food_name(original["food_query"])
                known_food = original.get("food")
                if (not same_query and entry.get("choice") is None
                        and not (known_food and exact_food(query, [known_food], entry.get("unit", "")))):
                    for key in ("food", "candidates", "choice", "offered_message_id", "source_ref"):
                        previous.pop(key, None)
                if entry.get("grams") is not None:
                    for key in ("quantity", "unit", "quantity_text"):
                        previous.pop(key, None)
                elif entry.get("quantity") is not None or entry.get("unit"):
                    previous.pop("grams", None)
                    previous.pop("quantity_text", None)
            merged = {**previous, **{k:v for k,v in entry.items() if v is not None}}
            if entry.get("source_ref") and not entry.get("food_query"):
                merged.pop("food_query", None)
            if web_completion:
                for key in ("quantity", "unit", "quantity_text"):
                    if original.get(key) not in (None, ""):
                        merged[key] = original[key]
            resolved, unfinished = resolve_item(rt, merged, pending=previous if original else None,
                                                portion_lookup=True, estimate=p.get("allow_estimate", True))
            entry_day = (original["date"] if web_completion else
                         resolve_date(p.get("date"), rt.day) if p.get("date") else (original or {}).get("date", day))
            entry_meal = original["meal_type"] if web_completion else p.get("meal_type") or (original or {}).get("meal_type", meal)
            if resolved:
                resolved["snapshot"]["meal_id"] = (original or {}).get("meal_id", rt.action_message["message_id"])
                items.append({**resolved, "date": entry_day, "meal_type": entry_meal})
                if pid:
                    completed_ids.append(pid)
            else:
                pid = pid or f"{rt.action_id}:item:{index + 1}"
                if refs := unfinished.pop("portion_cache_refs", None):
                    cache_refs[pid] = refs
                if any(condition["code"] in {"food_source_unavailable", "food_source_mismatch"}
                       for condition in unfinished.get("unmet_conditions", [])):
                    unfinished.pop("source_ref", None)
                offered = (original or {}).get("offered_message_id")
                if not original or unfinished.get("candidates", []) != original.get("candidates", []):
                    offered = rt.message["message_id"]
                pending_items[pid] = {**unfinished, "pending_id": pid, "date": entry_day, "meal_type": entry_meal,
                                      "offered_message_id": offered,
                                      "meal_id": (original or {}).get("meal_id", rt.action_message["message_id"]),
                                      "raw_text": (original or {}).get("raw_text", rt.message["text"])}
        for pid in completed_ids:
            pending_items.pop(pid, None)
        for i in items:
            i.setdefault("date", day)
            i.setdefault("meal_type", meal)
            if reuse:
                i["snapshot"]["meal_id"] = rt.action_message["message_id"]
        missing = [{**item, **({"portion_cache_refs": cache_refs[pid]} if pid in cache_refs else {})}
                   for pid, item in pending_items.items()]
        rt.prepare({"items": items, "session": session, "missing": missing, "cancelled": cancelled, "cancelled_items": cancelled_items})
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
    if not p.get("reuse"):
        for row in written:
            if evidence := row["snapshot"].get("portion_evidence"):
                cache_portion_evidence(rt, evidence)
    conditions = [{**condition, "ref": item["pending_id"]} for item in prepared["missing"]
                  for condition in item.get("unmet_conditions", [])]
    return {"status": "ok" if items or prepared["cancelled"] else "pending", "data": {
        "items": written, "total": totals(written),
        "meal_totals": meal_totals(rt, written),
        "completed_count": len(items), "unfinished": prepared["missing"], "cancelled": prepared["cancelled"], "cancelled_items": prepared.get("cancelled_items", []),
        "unmet_conditions": conditions,
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
                item, missing = resolve_item(rt, {**p, "food_query": row["food_name"]}, pending={"food": item["snapshot"]["food"]})
                if missing:
                    return {"status": "pending", "data": {"unfinished": [{**missing, "date": row["date"], "meal_type": row["meal_type"]}]}}
            item["snapshot"]["meal_id"] = row.get("snapshot", {}).get("meal_id", row["message_id"])
        rt.prepare({"record_id": row["record_id"], "item": item, "date": row["date"], "original": row})
    plan = rt.prepared
    rt.check_deadline()
    rows = (rt.store.intake_by_ids(rt.user_id, [plan["record_id"]], include_deleted=True)
            if hasattr(rt.store, "intake_by_ids") else rt.store.intake_items(rt.user_id, plan["date"], include_deleted=True))
    current = next((row for row in rows if row["record_id"] == plan["record_id"]), None)
    original = plan["original"]
    if current is None or any(current.get(k) != original.get(k) for k in
                              ("agent_revision", "grams", "nutrition", "snapshot", "status")):
        raise ValueError("prepared intake target has changed; start a new request")
    result = rt.store.revise_intake(rt.user_id, plan["record_id"], plan["item"])
    return {"status": "ok", "data": {"operation": "deleted" if plan["item"] is None else "revised", "item": result, "meal_totals": meal_totals(rt, [plan["original"]])}}

def calculate_meal(rt, p):
    from ..planning import calculate_plan
    return calculate_plan(rt, p)
