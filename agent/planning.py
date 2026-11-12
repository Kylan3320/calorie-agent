"""Session-bound food references and editable, non-ledger meal calculations."""
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
import math
import re

from .storage.bitable_adapter import DEFAULT_DAILY_STANDARD
from .storage import memory
from .storage.timeutil import round_number
from .tools.base import calculate_food_intake, positive_grams
from .tools.meals import NUTRIENTS, exact_food, from_row, resolve_date, resolve_item, scale_item, totals
from .tools.portions import query_web


def _food_source(rt, food, *, row=None):
    value = {"food": deepcopy(food), "kind": "intake_snapshot" if row else "food"}
    if row:
        value.update(record_id=row["record_id"], item=from_row(row))
    identity = json.dumps([rt.user_id, rt.message["chat_id"], value], ensure_ascii=False, sort_keys=True)
    ref = "food:" + hashlib.sha256(identity.encode()).hexdigest()[:24]
    rt.session.setdefault("food_sources", {})[ref] = value
    return ref


def _option(rt, food, *, row=None, source_ref=""):
    features = {key: deepcopy(food[key]) for key in ("brand", "category", "type", "source", "boohee_id") if food.get(key)}
    if row:
        features.update({key: deepcopy(row[key]) for key in ("date", "meal_type", "grams", "nutrition") if key in row})
    if food.get("units"):
        features["units"] = [{"unit": u.get("unit_name", u.get("unit", u.get("name", ""))),
                              **({"eat_weight": u["eat_weight"]} if u.get("eat_weight") is not None else {})}
                             for u in food["units"]]
    features["per_100g"] = {key: food[field] for key, field in
        (("carbs", "carbs_per_100g"), ("protein", "protein_per_100g"), ("fat", "fat_per_100g"), ("kcal", "kcal_per_100g"))
        if food.get(field) is not None}
    return {"name": food["name"], "source_ref": source_ref or _food_source(rt, food, row=row), "features": features}


def _available(rt):
    return deepcopy(rt.session.get("food_offer", {}).get("options", []))


def _source_item(rt, item, *, portion_lookup=False, sources=None):
    """Resolve only references returned in this conversation; no meal-pending choice."""
    item = deepcopy(item)
    estimate = item.pop("allow_estimate", True)
    ref = item.get("source_ref", "")
    if item.get("choice") is not None:
        offer = rt.session.get("food_offer", {})
        options = offer.get("options", [])
        n = item["choice"] - 1
        if not offer or offer.get("message_id") == rt.message["message_id"] or not 0 <= n < len(options):
            return None, {**item, "reason": "请使用已展示的营养查询选项或source_ref；记餐候选编号不能用于方案", "candidates": _available(rt)}
        selected = options[n]["source_ref"]
        if ref and ref != selected:
            return None, {**item, "reason": "来源引用与所选营养候选不一致", "candidates": _available(rt)}
        ref = selected
        if item.get("food_query") == offer.get("query"):
            item["food_query"] = ""
    if ref:
        source = (sources or {}).get(ref) or rt.session.get("food_sources", {}).get(ref)
        if not source:
            return None, {**item, "reason": "食物来源引用不可用，请重新查询食物或选择已展示选项", "candidates": _available(rt)}
        food = source["food"]
        if item.get("food_query") and exact_food(item["food_query"], [food], item.get("unit", "")) is None:
            return None, {**item, "reason": "食物名称与来源引用不一致；请使用所引来源的完整原名或省略food_query",
                          "candidates": [_option(rt, food, row=source.get("item"), source_ref=ref)]}
        item["food_query"] = food["name"]
        item.pop("choice", None)
        if source.get("item") and item.get("grams") is None and item.get("quantity") is None:
            resolved = deepcopy(source["item"])
            return {**resolved, "source_ref": ref}, None
        resolved, missing = resolve_item(rt, item, pending={"food": food}, portion_lookup=portion_lookup, estimate=estimate)
    elif not item.get("food_query"):
        return None, {**item, "reason": "请明确食物名称或可用来源引用", "candidates": _available(rt)}
    else:
        resolved, missing = resolve_item(rt, item, portion_lookup=portion_lookup, estimate=estimate)
    if resolved:
        resolved["source_ref"] = ref or _food_source(rt, resolved["snapshot"]["food"])
    if missing:
        missing["candidates"] = [_option(rt, f) for f in missing.get("candidates", [])]
        if missing.get("food"):
            missing["source_ref"] = _food_source(rt, missing["food"])
    return resolved, missing


def _offer(rt, query, options):
    if options:
        rt.session["food_offer_candidate"] = _offer_value(rt.message["message_id"], query, options)


def _offer_value(message_id, query, options):
    identity = json.dumps([message_id, query, options], ensure_ascii=False, sort_keys=True)
    return {"message_id": message_id, "query": query,
            "offer_id": "offer:" + hashlib.sha256(identity.encode()).hexdigest()[:24], "options": deepcopy(options)}


def query_food_nutrition(rt, p):
    if "portion_web" in p:
        web = query_web(rt, p["portion_web"])
        if not (p.get("food_query") or p.get("source_ref")):
            return web
        query = {key: p[key] for key in ("food_query", "source_ref", "grams", "quantity", "unit", "allow_estimate") if key in p}
        if not (query.get("quantity") or query.get("grams")):
            query["grams"] = 100
        nutrition = _query_food_nutrition(rt, query, publish_offer=False)
        return {**nutrition, "data": {**nutrition["data"], **web["data"]}}
    if "queries" not in p:
        return _query_food_nutrition(rt, p)
    results = []
    first_unresolved = False
    pending = False
    for query in p["queries"]:
        rt.check_deadline()
        # A batch shares source refs but has one public numbered offer. Once
        # the first unresolved query is encountered, later results cannot
        # replace its choice identity, including later successful lookups.
        result = _query_food_nutrition(rt, query, publish_offer=False)
        results.append({"query": deepcopy(query), **result})
        pending = pending or result["status"] != "ok"
        if not first_unresolved and (result["status"] != "ok" or len(result.get("data", {}).get("options", [])) > 1):
            first_unresolved = True
            candidate = rt.session.get("food_offer_candidate", {})
            if candidate.get("message_id") == rt.message["message_id"]:
                rt.session.pop("food_offer_candidate", None)
            _offer(rt, query.get("food_query", ""), result.get("data", {}).get("options", []))
    if not first_unresolved and rt.session.get("food_offer_candidate", {}).get("message_id") == rt.message["message_id"]:
        rt.session.pop("food_offer_candidate", None)
    return {"status": "pending" if pending else "ok", "data": {"queries": results}}


def _query_food_nutrition(rt, p, *, publish_offer=True):
    rows = []
    if p.get("record_id"):
        if p["record_id"] not in rt.session.get("refs", []):
            return {"status": "pending", "data": {"items": [], "question": "该摄入引用未在本会话返回，请先查询记录"}}
        rows = (rt.store.intake_by_ids(rt.user_id, [p["record_id"]]) if hasattr(rt.store, "intake_by_ids") else
                [r for r in rt.store.intake_items(rt.user_id) if r["record_id"] == p["record_id"]])
        if not rows:
            return {"status": "pending", "data": {"items": [], "question": "该摄入项已不可用，请重新指定"}}
    elif not p.get("source_ref") and p.get("choice") is None and p.get("grams") is None and p.get("quantity") is None:
        end = (date.fromisoformat(rt.day) + timedelta(days=1)).isoformat()
        rows = [r for r in rt.store.intake_items(rt.user_id, rt.day, end) if r["food_name"] == p.get("food_query")]
    if rows:
        rt.add_refs(rows)
        options = [_option(rt, from_row(r)["snapshot"]["food"], row=r) for r in rows]
        if publish_offer:
            _offer(rt, p.get("food_query", ""), options)
        return {"status": "ok", "data": {"items": [{**r, "source_ref": option["source_ref"]} for r, option in zip(rows, options)],
            "total": totals(rows), "source": "intake_snapshot", "options": options}}
    params = deepcopy(p)
    source_ref = params.get("source_ref")
    if not source_ref and params.get("choice") is not None:
        offer = rt.session.get("food_offer", {})
        n = params["choice"] - 1
        if offer.get("message_id") != rt.message["message_id"] and 0 <= n < len(offer.get("options", [])):
            source_ref = offer["options"][n]["source_ref"]
    source = rt.session.get("food_sources", {}).get(source_ref, {})
    if params.get("grams") is None and params.get("quantity") is None and not source.get("item"):
        params["grams"] = 100
    item, missing = _source_item(rt, params, portion_lookup=True)
    if item:
        options = [_option(rt, item["snapshot"]["food"])]
    else:
        options = (missing or {}).get("candidates", [])
    if publish_offer:
        _offer(rt, p.get("food_query", ""), options)
    return {"status": "pending" if missing else "ok", "data": {"items": [item] if item else [],
        "unfinished": [missing] if missing else [], "options": options}}


def _targets(rt, scope, day):
    # COS's current base_standard includes dated source rows. Daily plans must
    # explicitly exclude them; FakeStore has no dated source rows.
    if hasattr(rt.store, "_get"):
        target = dict(DEFAULT_DAILY_STANDARD)
        source_rows = rt.store._get("source:standards", [])
        for row in sorted(source_rows, key=lambda r: (bool(r.get("date")), bool(r.get("user_id")))):
            if row.get("user_id", "") not in ("", rt.user_id) or (row.get("date") and (scope == "daily" or row["date"] != day)):
                continue
            target.update({k: row[k] for k in NUTRIENTS if k in row})
    else:
        target = {k: rt.store.base_standard(rt.user_id)[k] for k in NUTRIENTS}
    target.update(rt.settings.get("targets", {}))
    if scope != "daily":
        target.update(rt.settings.get("daily_targets", {}).get(day, {}))
    return target


def _replace_items(items, replacements, *, automatic=False):
    items = deepcopy(items)
    by_id = {i["item_id"]: i for i in items}
    seen = set()
    for change in replacements:
        key = change.get("item_id")
        if key not in by_id or key in seen:
            return None, "要替换的餐项不在当前草案或重复指定"
        seen.add(key)
        prior = by_id[key]
        if automatic and prior.get("fixed"):
            return None, "固定餐不能在此调整中修改，请保留固定餐及份数"
        if automatic and prior.get("portion_basis", "user_given") != "suggested":
            return None, "只有建议份量可在备选中调整，请保留用户给定量和外卖组成假设"
        if automatic and change.get("fixed"):
            return None, "替换不能改变原餐项的固定标记"
        if automatic and "group" in change and change["group"] != prior.get("group", ""):
            return None, "替换须保留原餐次分组"
        if automatic and prior.get("max_grams") is not None and "max_grams" in change and change["max_grams"] != prior["max_grams"]:
            return None, "替换须保留用户明确的份量上限"
        if change.get("food_query") and change["food_query"] != prior.get("food_query"):
            prior.pop("source_ref", None)
            prior.pop("choice", None)
        if change.get("source_ref") and change["source_ref"] != prior.get("source_ref") and not change.get("food_query"):
            prior["food_query"] = ""
        if change.get("grams") is not None:
            for field in ("quantity", "unit", "quantity_text"):
                prior.pop(field, None)
        elif change.get("quantity") is not None or change.get("unit"):
            prior.pop("grams", None)
        prior.update(deepcopy(change))
    return items, None


def _draft_inputs(rt, p):
    source_ref = p.get("draft_ref")
    if not (p.get("draft_id") or source_ref):
        if p.get("replacements"):
            return None, {"status": "pending", "data": {"question": "请指定最近已展示草案的draft_id和要替换的item_id"}}
        return deepcopy(p), None
    if source_ref:
        if p.get("draft_id"):
            return None, {"status": "pending", "data": {"question": "请只指定一个源草案。"}}
        item, reason = memory.get(rt.store, rt.user_id, source_ref)
        draft = item.get("draft", {}) if reason == "ok" and item["status"] == "delivered" else {}
        if (not draft.get("calculation", {}).get("complete") or not draft.get("items")
                or any(part.get("source_ref") not in draft.get("food_sources", {}) for part in draft["items"])):
            return None, {"status": "pending", "data": {"reason": "source_draft_incomplete" if draft else reason,
                    "question": "无法核对这份已交付菜单的完整计算依据。请选另一份菜单或提供缺少的餐项。"}}
    else:
        draft = rt.session.get("meal_draft", {})
        if draft.get("draft_id") != p["draft_id"]:
            return None, {"status": "pending", "data": {"question": "原草案已失效或尚未展示，请从最近已展示草案继续", "draft_id": draft.get("draft_id", "")}}
        if draft.get("memory_ref") and memory.get(rt.store, rt.user_id, draft["memory_ref"])[1] == "withdrawn":
            return None, {"status": "pending", "data": {"question": "这份草案已撤回，请选择其他菜单。"}}
    if p.get("items"):
        return None, {"status": "pending", "data": {"question": "修改草案请通过replacements指定餐项，保留其余组合"}}
    if p.get("adjustment"):
        return None, {"status": "pending", "data": {"question": "改稿只替换指定项并保留原范围；需要扩大调整范围请先明确新计划"}}
    params = {k: deepcopy(draft[k]) for k in ("items", "scope", "date", "constraints", "tolerance_kcal", "portion_preference") if k in draft}
    params.update({k: deepcopy(v) for k, v in p.items() if k not in {"items", "replacements", "constraints"}})
    params["constraints"] = list(dict.fromkeys(draft.get("base_constraints", draft.get("constraints", [])) + p.get("constraints", [])))
    params["date"] = resolve_date(p.get("date"), rt.day)
    params["source_draft_id"] = draft["draft_id"]
    params["parent_ref"] = source_ref or draft.get("memory_ref", "")
    params["source_foods"] = draft.get("food_sources", {})
    params["inherited_conditions"] = draft.get("condition_refs", [])
    replacements = deepcopy(p.get("replacements", []))
    for change in replacements:
        if (change.get("grams") is not None or change.get("quantity") is not None) and "portion_basis" not in change:
            change["portion_basis"] = "user_given"
    items, problem = _replace_items(draft["items"], replacements)
    if problem:
        return None, {"status": "pending", "data": {"question": problem, "draft_id": draft["draft_id"]}}
    view = memory.state(rt.store, rt.user_id)
    for item in items:
        if item.get("assumption"):
            item["assumption"] = memory.redact_text(view, item["assumption"])
    params["items"] = items
    return params, None


def _calculate_items(rt, entries, sources=None):
    items, unfinished, canonical = [], [], []
    for entry in entries:
        key = entry["item_id"]
        resolved, missing = _source_item(rt, entry, sources=sources)
        servings = positive_grams(entry.get("servings", 1))
        basis = entry.get("portion_basis", "user_given")
        if resolved:
            if entry.get("max_grams") is not None and resolved["grams"] > entry["max_grams"]:
                unfinished.append({**entry, "reason": "每份可食克重超过用户明确上限"})
                canonical.append(entry)
                continue
            per_serving = deepcopy(resolved)
            planned = scale_item(resolved, servings)
            estimated = basis != "user_given" or bool(entry.get("assumption")) or bool(resolved["snapshot"].get("estimated"))
            planned["snapshot"].update(estimated=estimated, portion_basis=basis, assumption=entry.get("assumption", ""))
            planned.update(item_id=key, group=entry.get("group", ""), servings=servings, portion_basis=basis,
                           fixed=bool(entry.get("fixed")), assumption=entry.get("assumption", ""), per_serving={"grams": per_serving["grams"], "nutrition": per_serving["nutrition"]})
            items.append(planned)
            canonical.append({**entry, "food_query": resolved["matched_name"], "source_ref": resolved["source_ref"],
                              "grams": per_serving["grams"], "servings": servings, "portion_basis": basis})
        else:
            unfinished.append({**missing, "item_id": key, "group": entry.get("group", ""), "servings": servings,
                               "portion_basis": basis, "assumption": entry.get("assumption", "")})
            canonical.append(entry)
    return {"items": items, "unfinished": unfinished, "canonical": canonical, "total": totals(items),
            "complete": bool(canonical) and not unfinished}


def _comparison(calculation, target, consumed, tolerance, preference):
    diff = {k: round_number(target[k] - consumed[k] - calculation["total"][k]) for k in NUTRIENTS}
    relative = {k: abs(diff[k]) / target[k] for k in ("carbs", "protein", "fat") if target[k] > 0}
    adjustable = [i for i in calculation["items"] if not i.get("fixed")]
    return {"items": deepcopy(calculation["items"]), "total": calculation["total"],
            "unfinished": deepcopy(calculation["unfinished"]), "complete": calculation["complete"],
            "budget_difference": diff,
            "within_tolerance": calculation["complete"] and tolerance is not None and abs(diff["kcal"]) <= tolerance,
            "macro_relative_deviation": relative, "worst_macro_relative_deviation": max(relative.values(), default=0),
            "portion_preference": ([max((i["per_serving"]["grams"] for i in adjustable), default=0), len(adjustable)]
                                   if preference == "smaller_portions" else [])}


def _select(initial, adjustment):
    if initial["complete"] != adjustment["complete"]:
        return (1 if adjustment["complete"] else 0), "complete_plan"
    if not initial["complete"]:
        return 0, "incomplete_keep_initial"
    if initial["within_tolerance"] != adjustment["within_tolerance"]:
        return (1 if adjustment["within_tolerance"] else 0), "calorie_tolerance"
    if initial["within_tolerance"]:
        a = {k: abs(initial["budget_difference"][k]) for k in ("carbs", "protein", "fat")}
        b = {k: abs(adjustment["budget_difference"][k]) for k in a}
        if all(b[k] <= a[k] for k in a) and any(b[k] < a[k] for k in a):
            return 1, "macro_pareto"
        if all(a[k] <= b[k] for k in a) and any(a[k] < b[k] for k in a):
            return 0, "macro_pareto"
        if initial["worst_macro_relative_deviation"] != adjustment["worst_macro_relative_deviation"]:
            return int(adjustment["worst_macro_relative_deviation"] < initial["worst_macro_relative_deviation"]), "worst_macro_relative_deviation"
    else:
        a, b = abs(initial["budget_difference"]["kcal"]), abs(adjustment["budget_difference"]["kcal"])
        if a != b:
            return int(b < a), "closest_calories"
    if initial["portion_preference"] != adjustment["portion_preference"]:
        return int(adjustment["portion_preference"] < initial["portion_preference"]), "portion_preference"
    return 0, "tie_keep_initial"


def _portion_options(entry, item, lower_delta, upper_delta):
    """Source units, or ten-gram weighing steps; never invent a unit weight."""
    food = item["snapshot"]["food"]
    original = item["per_serving"]["grams"]
    per_100 = calculate_food_intake(food, 100)
    per_gram = per_100["kcal"] * item["servings"] / 100
    if per_gram <= 0:
        return [(original, item["nutrition"], {})]
    # Keep the source unit already used by a suggestion, including grams that
    # exactly encode a whole daily unit (e.g. 105.6 g = two medium eggs).
    unit = next((u for u in food.get("units", []) if float(u.get("eat_weight") or 0) > 1
                 and ((entry.get("unit") and entry["unit"] == u.get("unit_name", u.get("unit", u.get("name", ""))))
                      or (not entry.get("unit") and round(original / float(u["eat_weight"])) > 0
                          and abs(original - round(original / float(u["eat_weight"])) * float(u["eat_weight"])) <= 1))), None)
    step = float(unit["eat_weight"]) if unit else 10
    lower = max(step, math.floor((original + lower_delta / per_gram) / step + 1e-9) * step)
    ceiling = math.ceil((original + upper_delta / per_gram) / step - 1e-9) * step
    if entry.get("max_grams") is not None:
        ceiling = min(ceiling, entry["max_grams"])
    values = {round_number(n * step) for n in range(max(1, math.floor(lower / step + 1e-9)), math.floor(ceiling / step + 1e-9) + 1)}
    if not unit and ceiling < step and math.floor(ceiling) > 0:
        values.add(math.floor(ceiling))
    values.add(original)
    return [(g, calculate_food_intake(food, round_number(g * item["servings"])),
             ({"quantity": round(g / step), "unit": unit.get("unit_name", unit.get("unit", unit.get("name", "")))} if unit else {}))
            for g in sorted(values, key=lambda g: (abs(g - original), g))]


def _correct_portions(calculation, target, consumed, tolerance, preference, allowed, check_deadline):
    """Search at most two existing suggested portions using resolved facts only."""
    if not calculation["complete"]:
        return None, {"status": "incomplete", "eligible_item_ids": []}
    by_id = {i["item_id"]: i for i in calculation["items"]}
    eligible = [e for e in calculation["canonical"] if not e.get("fixed") and e.get("portion_basis") == "suggested"
                and (allowed is None or e["item_id"] in allowed)]
    info = {"eligible_item_ids": [e["item_id"] for e in eligible], "max_changed_items": 2,
            "gram_step": 10, "limits": ["固定餐、用户给量、外卖组成假设及未指定改稿项保持", "份数、来源、做法与每份上限保持"],
            "locked_item_ids": [e["item_id"] for e in calculation["canonical"] if e not in eligible],
            "max_grams": {e["item_id"]: e["max_grams"] for e in eligible if e.get("max_grams") is not None}}
    if not eligible:
        return None, {**info, "status": "no_adjustable_items"}
    base = _comparison(calculation, target, consumed, tolerance, preference)
    gap = base["budget_difference"]["kcal"]
    needed = math.copysign(max(0, abs(gap) - tolerance), gap)
    # Search from the current quantities to the nearest source-unit/gram step
    # that enters tolerance. Complementary moves may only offset the other
    # item's step overshoot; no global macro optimization or march to zero.
    options = [(e["item_id"], _portion_options(e, by_id[e["item_id"]], min(0, needed), max(0, needed))) for e in eligible]
    info["search_scope"] = "nearest_tolerance_boundary_and_complementary_step_overshoot"
    front = []

    def consider(changes):
        total = {k: round_number(calculation["total"][k] + sum(n[k] - by_id[key]["nutrition"][k] for key, (_, n, _) in changes)) for k in NUTRIENTS}
        diff = {k: round_number(target[k] - consumed[k] - total[k]) for k in NUTRIENTS}
        relative = {k: abs(diff[k]) / target[k] for k in ("carbs", "protein", "fat") if target[k] > 0}
        changed_grams = {key: value[0] for key, value in changes}
        score = {"total": total, "complete": True, "budget_difference": diff, "within_tolerance": abs(diff["kcal"]) <= tolerance,
                 "macro_relative_deviation": relative, "worst_macro_relative_deviation": max(relative.values(), default=0),
                 "portion_preference": ([max(changed_grams.get(i["item_id"], i["per_serving"]["grams"]) for i in calculation["items"] if not i.get("fixed")),
                                         sum(not i.get("fixed") for i in calculation["items"])] if preference == "smaller_portions" else []),
                 "changes": changes}
        rank = (not score["within_tolerance"], score["worst_macro_relative_deviation"] if score["within_tolerance"] else abs(diff["kcal"]))
        if front:
            prior = front[0]
            prior_rank = (not prior["within_tolerance"], prior["worst_macro_relative_deviation"] if prior["within_tolerance"] else abs(prior["budget_difference"]["kcal"]))
            if rank > prior_rank:
                return score
            if rank < prior_rank:
                front.clear()
        # At the minimum worst relative error, discard all dominated macro
        # points before using portion preferences. This also handles zero targets.
        if score["within_tolerance"]:
            def dominates(a, b):
                x, y = a["budget_difference"], b["budget_difference"]
                return all(abs(x[k]) <= abs(y[k]) for k in ("carbs", "protein", "fat")) and any(abs(x[k]) < abs(y[k]) for k in ("carbs", "protein", "fat"))
            if any(dominates(p, score) for p in front):
                return score
            front[:] = [p for p in front if not dominates(score, p)]
        same = next((p for p in front if p["budget_difference"] == score["budget_difference"]), None)
        if same is None:
            front.append(score)
        elif score["portion_preference"] < same["portion_preference"]:
            front[front.index(same)] = score
        return score

    safe_single = False
    for key, values in options:
        check_deadline()
        for value in values:
            if value[0] == by_id[key]["per_serving"]["grams"]:
                continue
            score = consider([(key, value)])
            safe_single |= score["within_tolerance"] and all(abs(score["budget_difference"][k]) <= abs(base["budget_difference"][k]) for k in ("carbs", "protein", "fat"))
    if not safe_single:
        single_deltas = {key: [value[1]["kcal"] - by_id[key]["nutrition"]["kcal"] for value in values] for key, values in options}
        paired = []
        for entry in eligible:
            key = entry["item_id"]
            other = [delta for other_key, deltas in single_deltas.items() if other_key != key for delta in deltas]
            paired.append((key, _portion_options(entry, by_id[key], min(0, needed - max(other, default=0)), max(0, needed - min(other, default=0)))))
        options = paired
        for n, (first, left) in enumerate(options):
            for second, right in options[n + 1:]:
                for a in left:
                    check_deadline()
                    if a[0] == by_id[first]["per_serving"]["grams"]:
                        continue
                    for b in right:
                        if b[0] != by_id[second]["per_serving"]["grams"]:
                            consider([(first, a), (second, b)])
    if not front:
        return None, {**info, "status": "kept_original"}
    best = min(front, key=lambda v: v["portion_preference"])
    changed = deepcopy(calculation)
    entries = {e["item_id"]: e for e in changed["canonical"]}
    items = {i["item_id"]: i for i in changed["items"]}
    changes = []
    for key, (grams, nutrition, unit) in best["changes"]:
        entry, item = entries[key], items[key]
        assumption = entry.get("assumption", "")
        original_assumption = assumption
        # Rewrite only quantities proven by the same source. Cooking and
        # composition clauses remain verbatim; the old wording stays in audit.
        assumption = re.sub(r"(?<![\d.])" + re.escape(f"{entry['grams']:g}") + r"(?=\s*克)", f"{grams:g}", assumption)
        if unit:
            source_unit = re.split(r"[（(]", unit["unit"])[0]
            old_quantity = entry["grams"] / (grams / unit["quantity"])
            if abs(entry["grams"] - round(old_quantity) * (grams / unit["quantity"])) <= 1:
                old_quantity = round(old_quantity)
            assumption = re.sub(r"(?<![\d.])" + re.escape(f"{old_quantity:g}") + r"(?=\s*" + re.escape(source_unit) + ")", f"{unit['quantity']:g}", assumption)
        changes.append({"item_id": key, "original_grams": entry["grams"], "selected_grams": grams,
                        "original_assumption": original_assumption, "selected_assumption": assumption, **unit})
        for field in ("quantity", "unit", "quantity_text"):
            entry.pop(field, None)
            item["snapshot"].pop(field, None)
        entry.update(grams=grams, assumption=assumption, **unit)
        item.update(grams=round_number(grams * item["servings"]), nutrition=nutrition,
                    assumption=assumption, per_serving={"grams": grams, "nutrition": calculate_food_intake(item["snapshot"]["food"], grams)})
        item["snapshot"].update(basis="boohee_eat_weight" if unit else "user_edible_grams", assumption=assumption, **unit)
    changed["total"] = totals(changed["items"])
    return changed, {**info, "status": "adjusted", "searched_items": 2 if not safe_single and len(options) > 1 else 1, "changes": changes,
                     "original_budget_difference": base["budget_difference"], "selected_budget_difference": best["budget_difference"]}


def _plan_memory(rt, params, day):
    view = memory.state(rt.store, rt.user_id)
    explicit = set(params.get("condition_refs", []))
    inherited = params.get("inherited_conditions", [])
    draft_id = params.get("source_draft_id")
    base = [text for text in params.get("constraints", []) if memory.redact_text(view, text) == text]
    conditions, explanations = [], []
    for ref in dict.fromkeys([*inherited, *params.get("condition_refs", [])]):
        item, status = memory.get(rt.store, rt.user_id, ref, view=view)
        if item and item["status"] == "superseded":
            current = view.get("current", {}).get(memory.ref_id(ref), ref)
            item, status = memory.get(rt.store, rt.user_id, current, view=view)
        if status != "ok" or item["kind"] != "condition" or not memory.applicable(item, day, draft_id):
            continue
        if params.get("draft_ref") and not item.get("ends") and ref not in explicit:
            return None, {"status": "pending", "data": {"question": f"原菜单的条件「{item['text']}」现在仍适用吗？"}}
        if item.get("source_only") and not draft_id and item["turn_id"] != rt.human_id:
            continue
        conditions.append(item["ref"])
        explanations.append(item["text"])
    experiences = [ref for ref in dict.fromkeys(params.get("experience_refs", []))
                   if ref.startswith("note:") and memory.dependencies_valid(rt.store, rt.user_id, [ref], day, view=view)]
    return (base, conditions, explanations, experiences), None


def _choose_plan(rt, ref):
    draft = rt.session.get("meal_plan_choices", {}).get(ref)
    if not draft or draft["message_id"] != rt.message["message_id"]:
        return {"status": "pending", "data": {"unmet_conditions": [{"code": "candidate_unavailable", "ref": ref}]}}
    data = draft["calculation"]
    day, scope = draft["date"], draft["scope"]
    rt.settings = rt.store.load_document("user_settings", rt.user_id)
    view = memory.state(rt.store, rt.user_id)
    expected = [*draft.get("condition_refs", []), *draft.get("experience_refs", [])]
    if draft.get("parent_ref"):
        expected.append(draft["parent_ref"])
    rows = rt.store.intake_items(rt.user_id, day, (date.fromisoformat(day) + timedelta(days=1)).isoformat()) if scope == "remaining" else []
    if (not memory.dependencies_valid(rt.store, rt.user_id, expected, day, view=view, draft_id=draft.get("source_draft_id"))
            or _targets(rt, scope, day) != data["targets"] or totals(rows) != data["consumed"]
            or {row["record_id"] for row in rows} != set(data["consumed_scope"]["record_ids"])):
        return {"status": "pending", "data": {"unmet_conditions": [{"code": "candidate_inputs_changed", "ref": ref}]}}
    rt.session["meal_draft_candidate"] = deepcopy(draft)
    return {"status": "ok" if data["complete"] else "pending", "data": deepcopy(data)}


def calculate_plan(rt, p):
    if p.get("candidate_ref"):
        return _choose_plan(rt, p["candidate_ref"])
    params, invalid = _draft_inputs(rt, p)
    if invalid:
        return invalid
    scope = params.get("scope", "remaining")
    day = resolve_date(params.get("date"), rt.day)
    memory_inputs, invalid = _plan_memory(rt, params, day)
    if invalid:
        return invalid
    base_constraints, conditions, explanations, experiences = memory_inputs
    entries = [{**entry, "item_id": entry.get("item_id") or f"I{n}"} for n, entry in enumerate(params.get("items", []), 1)]
    keys = [e["item_id"] for e in entries]
    if not keys:
        return {"status": "pending", "data": {"items": [], "question": "请给出菜单食物与每份份量，或指定最近已展示草案继续修改"}}
    if len(keys) != len(set(keys)):
        return {"status": "pending", "data": {"question": "菜单餐项item_id重复，请分别指定"}}
    # Resolve fixed portions first, then keep those exact source calculations in
    # both candidates. Intake and target budgets are each read only once.
    sources = params.get("source_foods", {})
    fixed = _calculate_items(rt, [e for e in entries if e.get("fixed")], sources)
    supplement = _calculate_items(rt, [e for e in entries if not e.get("fixed")], sources)
    def combine(extra, order):
        combined = {key: fixed[key] + extra[key] for key in ("items", "unfinished", "canonical")}
        positions = {e["item_id"]: n for n, e in enumerate(order)}
        for key in combined:
            combined[key].sort(key=lambda e: positions[e["item_id"]])
        combined.update(total=totals(combined["items"]), complete=bool(order) and not combined["unfinished"])
        return combined
    calculations = [combine(supplement, entries)]
    target = _targets(rt, scope, day)
    consumed_rows = []
    if scope == "remaining":
        consumed_rows = rt.store.intake_items(rt.user_id, day, (date.fromisoformat(day) + timedelta(days=1)).isoformat())
    consumed = totals(consumed_rows)
    tolerance = params.get("tolerance_kcal")
    preference = params.get("portion_preference")
    compared = [{"variant": "initial", **_comparison(calculations[0], target, consumed, tolerance, preference)}]
    selected, reason, rejected = 0, "only_initial", None
    adjustment = params.get("adjustment")
    if adjustment:
        changed, rejected = _replace_items(calculations[0]["canonical"], adjustment.get("replacements", []), automatic=True)
        additions = adjustment.get("additions", [])
        if not rejected and (any(not i.get("item_id") or i.get("fixed") for i in additions)
                             or len(set(keys + [i.get("item_id") for i in additions])) != len(keys) + len(additions)):
            rejected = "补充项须有新的唯一item_id，且不能增加固定餐"
        if not rejected:
            changed += deepcopy(additions)
            alternative = _calculate_items(rt, [e for e in changed if not e.get("fixed")])
            calculations.append(combine(alternative, changed))
            compared.append({"variant": "adjustment", **_comparison(calculations[1], target, consumed, tolerance, preference)})
            selected, reason = _select(*compared)
    calibration = {"status": "no_tolerance" if tolerance is None else "already_within_tolerance"}
    if p.get("use_default_calibration", True) and tolerance is not None and not any(c["within_tolerance"] for c in compared):
        allowed = {e["item_id"] for e in p.get("replacements", [])} if p.get("draft_id") or p.get("draft_ref") else None
        corrected, correction = None, None
        for n in [selected] + [i for i in range(len(calculations)) if i != selected]:
            calculation = calculations[n]
            candidate, info = _correct_portions(calculation, target, consumed, tolerance, preference, allowed, rt.check_deadline)
            if candidate is None:
                if correction is None:
                    correction = info
                continue
            score = _comparison(candidate, target, consumed, tolerance, preference)
            if corrected is None or _select(corrected[1], score)[0]:
                corrected = (candidate, score)
                correction = {**info, "starting_variant": compared[n]["variant"]}
            if score["within_tolerance"]:
                break
        calibration = correction or {"status": "no_legal_portions"}
        if corrected:
            calculations.append(corrected[0])
            compared.append({"variant": "portion_adjustment", **corrected[1]})
            picked, final_reason = _select(compared[selected], compared[-1])
            if picked:
                selected, reason = len(compared) - 1, final_reason
            else:
                calibration["status"] = "kept_original"
    compare_only = p.get("selection_mode") == "compare"
    if compare_only:
        for index, option in enumerate(compared):
            digest = hashlib.sha256(f"{rt.action_id}:{index}".encode()).hexdigest()[:24]
            option["candidate_ref"] = f"meal-candidate:{digest}"

    def draft_at(index, selection_reason):
        chosen = calculations[index]
        items, canonical, unfinished, total, complete = (chosen[k] for k in ("items", "canonical", "unfinished", "total", "complete"))
        diff = compared[index]["budget_difference"]
        draft_id = "draft:" + hashlib.sha256((rt.action_id + json.dumps(canonical, sort_keys=True, ensure_ascii=False)).encode()).hexdigest()[:24]
        constraints = list(dict.fromkeys([*base_constraints, *explanations]))
        food_sources = {**rt.session.get("food_sources", {}), **sources}
        draft = {"draft_id": draft_id, "message_id": rt.message["message_id"], "items": canonical,
                 "scope": scope, "date": day, "received_date": rt.day, "constraints": constraints, "unfinished": unfinished,
                 "base_constraints": [value for value in base_constraints if value not in explanations],
                 "condition_refs": conditions, "experience_refs": experiences, "parent_ref": params.get("parent_ref", ""),
                 "source_draft_id": params.get("source_draft_id"),
                 "food_sources": {entry["source_ref"]: food_sources[entry["source_ref"]] for entry in canonical
                                  if entry.get("source_ref") in food_sources}}
        if tolerance is not None:
            draft["tolerance_kcal"] = tolerance
        if preference:
            draft["portion_preference"] = preference
        data = {
            "items": items, "total": total, "unfinished": unfinished, "date": day, "scope": scope,
            "targets": target, "consumed": consumed, "consumed_count": len(consumed_rows),
            "consumed_scope": {"date": day, "included": scope == "remaining", "record_ids": [r["record_id"] for r in consumed_rows]},
            "budget_difference": diff, "complete": complete, "estimated": any(i["snapshot"].get("estimated") for i in items),
            "within_tolerance": complete and tolerance is not None and abs(diff["kcal"]) <= tolerance,
            "tolerance_kcal": tolerance, "constraints": draft["constraints"], "draft_id": draft_id}
        data.update(fixed_subtotal=fixed["total"], fixed_complete=not fixed["unfinished"],
                    supplement_gap={k: round_number(target[k] - consumed[k] - fixed["total"][k]) for k in NUTRIENTS},
                    selection={"selected": compared[index]["variant"], "reason": selection_reason, "candidates": compared, "calibration": calibration})
        if rejected:
            data["selection"]["rejected_adjustment"] = rejected
        draft["calculation"] = deepcopy(data)
        return draft

    if compare_only:
        rt.session.pop("meal_draft_candidate", None)
        rt.session["meal_plan_choices"] = {
            option["candidate_ref"]: draft_at(index, "model_choice") for index, option in enumerate(compared)}
        return {"status": "ok", "data": {"scope": scope, "date": day, "targets": target, "consumed": consumed,
                "constraints": list(dict.fromkeys([*base_constraints, *explanations])),
                "selection": {"candidates": compared, "default_ref": compared[selected]["candidate_ref"],
                              "calibration": calibration, "awaiting_choice": True}}}
    draft = draft_at(selected, reason)
    rt.session["meal_draft_candidate"] = deepcopy(draft)
    data = draft["calculation"]
    return {"status": "ok" if data["complete"] else "pending", "data": data}


def candidate_presentations(session, message_id, *, facts=()):
    """A complete public rendering makes delivered candidate identity unambiguous."""
    sections, presented = [], {}
    offer = session.get("food_offer_candidate", {})
    draft = session.get("meal_draft_candidate", {})
    draft = draft if draft.get("message_id") == message_id else {}
    first_missing = next(iter(draft.get("calculation", {}).get("unfinished", [])), {})
    if first_missing.get("candidates"):
        # A direct calculate can discover ambiguity without a prior nutrition
        # query. Bind exactly the source options actually displayed below.
        offer = _offer_value(message_id, first_missing.get("food_query", ""), first_missing["candidates"])
        session["food_offer_candidate"] = deepcopy(offer)
    unfinished_refs = {option.get("source_ref") for item in draft.get("calculation", {}).get("unfinished", []) for option in item.get("candidates", [])}
    show_offer = not draft or bool(unfinished_refs.intersection(option.get("source_ref") for option in offer.get("options", [])))
    nutrition = next((f for f in reversed(facts) if f.get("tool") == "query_food_nutrition"), {})
    needs_choice = bool(first_missing.get("candidates")) or nutrition.get("status") == "pending" or len(offer.get("options", [])) > 1
    if show_offer and needs_choice and offer.get("message_id") == message_id:
        lines = []
        for n, option in enumerate(offer["options"], 1):
            features = option.get("features", {})
            traits = [str(features[k]) for k in ("brand", "category", "type") if features.get(k)]
            if features.get("date"):
                traits.append(str(features["date"]))
            if features.get("meal_type"):
                traits.append({"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐", "snack": "加餐", "unknown": "未指定餐次"}[features["meal_type"]])
            if features.get("grams") is not None:
                traits.append(f"可食{features['grams']:g}克")
            if features.get("nutrition", {}).get("kcal") is not None:
                traits.append(f"{features['nutrition']['kcal']:g}千卡")
            for unit in features.get("units", []):
                label = str(unit["unit"])
                if unit.get("eat_weight") is not None:
                    label += f"（每单位可食{float(unit['eat_weight']):g}克）"
                traits.append("单位：" + label)
            if features.get("source"):
                traits.append("薄荷食物资料" if features["source"] == "boohee" else str(features["source"]))
            elif features.get("boohee_id"):
                traits.append("薄荷食物资料")
            label = f"{n}. {option['name']}"
            lines.append(label + ("（" + "，".join(traits) + "）" if traits else "（本地食物资料）"))
        sections.append("营养查询选项：\n" + "；".join(lines))
        presented["food_offer_candidate"] = offer["offer_id"]
    if draft:
        d = draft["calculation"]
        scope = {"daily": "日常完整菜单", "full_day": "当日完整菜单", "remaining": "当日剩余安排"}[draft["scope"]]
        lines = [f"{scope}（{'长期目标，' if draft['scope'] == 'daily' else ''}{draft['date']}）："]
        labels = {"user_given": "用户给定份量", "suggested": "建议份量", "assumed_takeout": "外卖组成假设"}
        positions = {item["item_id"]: n for n, item in enumerate(draft["items"], 1)}
        groups = {}
        for item in d["items"]:
            groups.setdefault(item.get("group") or "餐项", []).append(item)
        for group, entries in groups.items():
            lines.append(group + "：")
            for item in entries:
                note = ("固定餐，组成估算" if item.get("fixed") and item.get("assumption") else
                        ("固定餐，" if item.get("fixed") else "") + labels[item["portion_basis"]])
                lines.append(f"- {item['matched_name']}，每份可食{item['per_serving']['grams']:g}克×{item['servings']:g}份（{note}），{item['nutrition']['kcal']:g}千卡。")
        # Free-text quantities can refer to another item or a pre-calibration
        # amount. Public quantities come only from the resolved menu above;
        # preserve the original complete assumption in durable tool facts.
        quantitative = re.compile(r"\d|[零〇一二两三四五六七八九十百千万半]+\s*(?:千卡|卡路里|克|毫克|公斤|斤|个|只|份|成|倍|%|％)|百分之")
        assumptions = list(dict.fromkeys(clause.strip() for item in d["items"]
            for clause in re.split(r"[，,；;。]", item.get("assumption", ""))
            if clause.strip() and not quantitative.search(clause)))
        if assumptions:
            lines.append("估算前提：" + "；".join(assumptions) + "。")
        if any(item.get("fixed") and (item.get("assumption") or item.get("portion_basis") == "assumed_takeout") for item in d["items"]):
            lines.append("固定餐组成按所列来源和可食份量估算，不代表店铺已核实配方。")
        if draft["constraints"]:
            lines.append("保留条件：" + "；".join(draft["constraints"]) + "。")
        qualifier = "估算" if d["estimated"] else ""
        aggregate = "菜单合计" if d["complete"] else "已知小计"
        total = d["total"]
        lines.append(f"{aggregate}{qualifier}{total['kcal']:g}千卡，碳水{total['carbs']:g}克、蛋白质{total['protein']:g}克、脂肪{total['fat']:g}克。")
        target = d["targets"]
        lines.append(f"适用目标：{target['kcal']:g}千卡，碳水{target['carbs']:g}克、蛋白质{target['protein']:g}克、脂肪{target['fat']:g}克。")
        if draft["scope"] == "remaining":
            lines.append(f"已扣除该日有效摄入{d['consumed']['kcal']:g}千卡（仅扣一次）。")
        diff = d["budget_difference"]
        lines.append(f"差额：热量{diff['kcal']:g}千卡，碳水{diff['carbs']:g}克，蛋白质{diff['protein']:g}克，脂肪{diff['fat']:g}克。")
        if d["unfinished"]:
            lines.append("尚未计入：" + "；".join((i.get("group") or f"第{positions[i['item_id']]}项") + " " + i.get("food_query", "") + "：" + i["reason"] for i in d["unfinished"]) + "。差额仅基于已知小计。")
            first = d["unfinished"][0]
            lines.append("请先补充" + first.get("food_query", "首个未计入项目") + "的明确来源或可食份量。")
        if d["tolerance_kcal"] is not None:
            lines.append("估算方案满足热量容差。" if d["within_tolerance"] else "当前不能确认方案满足热量容差。")
            if d["complete"] and not d["within_tolerance"]:
                calibration = d.get("selection", {}).get("calibration", {})
                if calibration.get("status") == "no_adjustable_items":
                    lines.append("本次范围内没有可调整的建议份量，保留指定项及其余餐项。")
                elif calibration.get("max_grams"):
                    names = {i["item_id"]: i["matched_name"] for i in d["items"]}
                    lines.append("保留每份可食上限：" + "；".join(f"{names[k]}{v:g}克" for k, v in calibration["max_grams"].items()) + "；其余固定量及调整范围保持。")
        reason = d.get("selection", {}).get("reason")
        explanation = {"macro_pareto": "选择了热量容差内宏量偏差更小的搭配。", "worst_macro_relative_deviation": "按三项宏量中最差相对偏差较小者选择。", "portion_preference": "宏量取舍相当，按较小单项份量选择。", "closest_calories": "两版均超出热量容差，保留更接近热量目标的完整搭配。"}.get(reason)
        if explanation:
            lines.append(explanation)
        sections.append("\n".join(lines))
        presented["meal_draft_candidate"] = draft["draft_id"]
    return {"text": "\n\n".join(sections), "presented_candidates": presented}
