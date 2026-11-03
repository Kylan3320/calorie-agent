"""Natural replies, compact model facts, and explicit proposal presentation."""
import json
import re
import math
from copy import deepcopy
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

LABELS = {"carbs":"碳水", "protein":"蛋白质", "fat":"脂肪", "kcal":"热量"}

def settings_view(settings):
    result = {k:deepcopy(settings[k]) for k in ("preferences", "targets") if k in settings}
    result["favorites"] = list(settings.get("favorites", {}))
    reminders = {key:{k:deepcopy(rule[k]) for k in ("rule_id", "enabled", "meal_type", "time", "weekdays") if k in rule}
                 for key,rule in settings.get("reminders", {}).items() if rule.get("enabled")}
    if reminders:
        result["reminders"] = reminders
    return result


def _food_choice_view(food):
    """Preserve candidate identity and distinguishing traits, not nutrition caches."""
    result = {k:deepcopy(food[k]) for k in ("food_id", "boohee_id", "name", "aliases", "brand", "category", "type", "source") if k in food}
    if food.get("units"):
        result["units"] = [{"unit_name":unit.get("unit_name", unit.get("unit", unit.get("name", ""))),
                            **({"eat_weight":unit["eat_weight"]} if "eat_weight" in unit else {})} for unit in food["units"]]
    return result


def _unfinished_view(item, pending_id=None):
    result = {k:deepcopy(item[k]) for k in ("pending_id", "food_query", "date", "meal_type", "reason", "grams", "quantity", "unit", "quantity_text", "choice", "offered_message_id") if k in item}
    if pending_id is not None:
        result["pending_id"] = pending_id
    if item.get("food"):
        result["food"] = _food_choice_view(item["food"])
    if item.get("candidates"):
        result["candidates"] = [{**_food_choice_view(candidate), "choice": n} for n,candidate in enumerate(item["candidates"], 1)]
    return result


def state_view(rt):
    """Rebuild current actionable state without copying historical identity sets."""
    session = rt.session
    unfinished = {pending_id:_unfinished_view(item, pending_id) for pending_id,item in session.get("unfinished", {}).items()}
    proposal = session.get("proposal")
    proposal = {k:deepcopy(proposal[k]) for k in ("proposal_id", "tool", "args", "target", "target_version", "target_ratio", "response_id", "presented") if k in proposal} if proposal else None
    last_ids = session.get("last_meal", [])
    proposal_ids = []
    if proposal:
        args = proposal.get("args", {})
        proposal_ids = ([args["record_id"]] if args.get("record_id") else []) + args.get("reuse", {}).get("record_ids", [])
        if proposal.get("tool") == "update_user_settings":
            proposal_ids += args.get("values", {}).get("record_ids", [])
    # Last meal and fixed proposal targets remain complete even when unusually
    # large. Only incidental recent references have a bounded tail.
    ids = list(dict.fromkeys(session.get("refs", [])[-12:] + last_ids + proposal_ids))
    rows = []
    if ids:
        rows = (rt.store.intake_by_ids(rt.user_id, ids) if hasattr(rt.store, "intake_by_ids")
                else [row for row in rt.store.intake_items(rt.user_id) if row["record_id"] in ids])
    by_id = {row["record_id"]:row for row in rows}
    refs = [{k:by_id[record_id][k] for k in ("record_id", "food_name", "date", "meal_type") if k in by_id[record_id]}
            for record_id in ids if record_id in by_id]
    dates = {rt.day, *(item.get("date") for item in unfinished.values()), *(row.get("date") for row in rows)}
    if proposal:
        dates.update((proposal.get("target", {}).get("date"), proposal.get("args", {}).get("date")))
    settings = settings_view(rt.settings)
    relevant_targets = {day:deepcopy(target) for day,target in rt.settings.get("daily_targets", {}).items() if day in dates}
    if relevant_targets:
        settings["daily_targets"] = relevant_targets
    return {"received_date": rt.day, "user_settings": settings, "unfinished": unfinished, "refs": refs,
            "last_meal": [record_id for record_id in last_ids if record_id in by_id], "proposal": proposal}

def model_view(fact):
    def compact(value):
        if isinstance(value,list): return [compact(v) for v in value]
        if not isinstance(value,dict): return value
        result = {k:compact(v) for k,v in value.items() if k not in {"snapshot","created_at","message_id","action_id","display","record_id_internal","food_id","score"}}
        snap=value.get("snapshot",{})
        if snap:
            result["estimated"]=snap.get("estimated",False)
            result["source"]=snap.get("food",{}).get("source",snap.get("basis",""))
            if snap.get("meal_id"):
                result["meal_id"] = snap["meal_id"]
        if "units" in result: result["units"]=[{k:u[k] for k in ("unit_name","eat_weight") if k in u} for u in value["units"]]
        return result
    source = fact
    if "unfinished" in fact.get("data", {}):
        source = {**fact, "data": {**fact["data"], "unfinished": [_unfinished_view(item) for item in fact["data"]["unfinished"]]}}
    if fact.get("tool") == "query_history" and fact.get("status") == "ok":
        data = fact["data"]
        detail = data.get("detail", "summary")
        fields = ("start_date", "end_date", "count", "total", "detail") + (("daily",) if detail=="daily" else ("items",) if detail=="items" else ())
        source = {**fact, "data": {**{key:data[key] for key in fields if key in data}, "detail": detail}}
    result=compact(source)
    if fact.get("tool")=="query_today" and fact.get("status")=="ok":
        data=result["data"];detail=data.get("detail","calories")
        if detail=="calories":result["data"]={"date":data["date"],"count":data["count"],"total":{"kcal":data["total"]["kcal"]},"detail":detail}
        elif detail=="summary":data.pop("items",None)
    if fact.get("tool")=="query_knowledge":
        data = result.get("data", {})
        if any(entry.get("answer") for entry in data.get("entries", [])):
            data.pop("evidence", None)
    if fact.get("tool") in {"record_meal","revise_intake"}:
        def calories(value):
            if isinstance(value,list):return [calories(v) for v in value]
            if not isinstance(value,dict):return value
            return {k:({"kcal":v["kcal"]} if k in {"nutrition","total"} and isinstance(v,dict) and "kcal" in v else calories(v)) for k,v in value.items()}
        result=calories(result)
    return result

def project_messages(messages):
    result=[]
    for message in messages:
        if isinstance(message,ToolMessage):
            value=json.loads(message.content)
            message=message.model_copy(update={"content":json.dumps(model_view(value),ensure_ascii=False)})
        result.append(message)
    return result


def history_messages(messages):
    """Keep dialogue and operation references; re-query mutable business facts."""
    result = []
    business_turn = False
    knowledge_turn = False
    for message in messages:
        if message.additional_kwargs.get("lc_source") == "summarization":
            # Older summaries can contain ledger values from before migration or
            # cleanup. Unfinished items, references and settings live separately.
            if message.additional_kwargs.get("fact_scope") != "dialogue":
                continue
        elif isinstance(message, HumanMessage):
            business_turn = False
            knowledge_turn = False
        if isinstance(message, AIMessage) and "reasoning_content" in message.additional_kwargs:
            message = message.model_copy(update={"additional_kwargs": {**message.additional_kwargs, "reasoning_content": ""}})
        if isinstance(message, AIMessage) and message.tool_calls:
            business_turn |= any(c["name"] != "query_knowledge" for c in message.tool_calls)
            knowledge_turn |= any(c["name"] == "query_knowledge" for c in message.tool_calls)
            if business_turn:
                message = message.model_copy(update={"content": ""})
        elif isinstance(message, ToolMessage):
            value = json.loads(message.content)
            if value.get("tool", message.name) != "query_knowledge":
                business_turn = True
                references = []
                def collect(value):
                    if isinstance(value, list):
                        for child in value: collect(child)
                    elif isinstance(value, dict):
                        if value.get("record_id"):
                            references.append({k: value[k] for k in ("record_id", "date", "food_name", "matched_name", "meal_type") if k in value})
                        else:
                            for child in value.values(): collect(child)
                collect(value.get("data", {}))
                historical = {k: value[k] for k in ("tool", "call_id", "action_id") if k in value}
                historical.update(status="historical", data={"references": references})
                message = message.model_copy(update={"content": json.dumps(historical, ensure_ascii=False)})
            else:
                knowledge_turn = True
                message = message.model_copy(update={"content": json.dumps(model_view(value), ensure_ascii=False)})
        elif isinstance(message, AIMessage):
            scope = message.additional_kwargs.get("fact_scope")
            if (business_turn or scope == "business") and not (scope == "dialogue" and message.additional_kwargs.get("execution_terminated") is True):
                continue
            if not knowledge_turn and scope not in {"dialogue", "knowledge"}:
                continue
        result.append(message)
    return result

def render(text,facts):
    known={e.get("source_id"):e.get("source", "现有知识资料") for f in facts if f["tool"]=="query_knowledge" for e in f.get("data",{}).get("entries",[])}
    adjustments=[]
    def cite(m):
        if m[1] in known:
            adjustments.append({"reason":"source_reference_resolved","reference":m[1]})
            return "〔"+known[m[1]]+"〕"
        adjustments.append({"reason":"unknown_source_reference","reference":m[1]})
        return ""
    return re.sub(r"\[(K\d+)\]",cite,text),adjustments

def proposal_prompt(proposal):
    args=proposal["args"];tool=proposal["tool"]
    if tool=="update_user_settings":
        values=args.get("values",{});kind=args["kind"]
        if kind=="targets":
            detail="、".join(f"{LABELS[k]}{v:g}{'千卡' if k=='kcal' else '克'}" for k,v in values.items())
            action=("今天" if args.get("scope")=="today" else "长期")+"目标改为"+detail
        elif kind=="favorite": action=("移除" if values.get("remove") else "保存")+"常用餐“"+str(values.get("name",""))+"”"
        elif kind=="preferences":action="保存偏好："+"、".join(str(v) for v in values.values())
        elif kind=="portion":action=f"保存{values.get('food_query','')}每{values.get('unit','份')}可食重{values.get('eat_weight','')}克"
        else:
            meals={"breakfast":"早餐","lunch":"午餐","dinner":"晚餐","snack":"加餐"}
            days="、".join("周"+"一二三四五六日"[d] for d in values.get("weekdays",range(7)))
            action=("开启" if values.get("enabled") else "关闭")+meals.get(values.get("meal_type"),"")+"记餐提醒"+("，时间"+values["time"] if values.get("time") else "")+"，"+days
    elif tool=="revise_intake":
        target=proposal.get("target",{})
        name=target.get("date","")+target.get("food_name","所选摄入")
        quantity=str(args["grams"])+"克" if args.get("grams") else (str(args["ratio"])+"倍" if args.get("ratio") else str(args.get("quantity",""))+args.get("unit",""))
        action="撤销"+name if args.get("delete") else "把"+name+"改为"+quantity
    else:
        items=args.get("items",[])
        action="记录"+"、".join(str(i.get("food_query",""))+(f"{i['grams']:g}克" if i.get("grams") else f"{i.get('quantity','')}{i.get('unit','')}") for i in items)
        if args.get("reuse"):
            reuse=args["reuse"];action="记录"+str(reuse.get("ratio",1))+"份"+("常用餐“"+reuse["favorite"]+"”" if reuse.get("favorite") else "所选历史餐")
    return "要"+action+"吗？回复确认即可。"

def progress_reply(facts):
    parts=[]
    latest_meals={}
    latest_today=None
    for f in facts:
        data=f.get("data",{});status=f.get("status")
        if status=="ok" and f["tool"] in {"record_meal","revise_intake"}:
            latest_today=None
            parts.append("已撤销。" if data.get("operation")=="deleted" else ("已修改。" if f["tool"]=="revise_intake" else "已记好。"))
            for meal in data.get("meal_totals",[]):latest_meals[meal['meal_id']]=meal
        elif status=="ok" and f["tool"]=="update_user_settings":parts.append("设置已保存。")
        elif f["tool"]=="query_today" and status=="ok":latest_today=data['total']['kcal']
        for item in data.get("unfinished",[]):parts.append(item.get("food_query","")+"："+item.get("reason","请补充份量"))
        if data.get("question"):parts.append(data["question"])
    for meal in latest_meals.values():
        parts.append(f"这餐合计{meal['total']['kcal']:g}千卡。"+("份量为估算。" if meal.get('estimated') else ""))
    if latest_today is not None:parts.append(f"今天合计{latest_today:g}千卡。")
    return "\n".join(dict.fromkeys(parts)) or "这次处理遇到了问题，请稍后重试。"


_LEDGER_TOOLS = {"record_meal", "revise_intake", "query_today", "query_history"}
_MEAL_NAMES = {"unknown": "", "breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐", "snack": "加餐"}
_RECEIPT_NUTRIENTS = ("kcal", "carbs", "protein", "fat")


class _IncompleteReceipt(ValueError):
    """The persisted result does not contain everything this renderer needs."""


def _receipt_require(condition):
    if not condition:
        raise _IncompleteReceipt()


def _receipt_number(value):
    _receipt_require(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value))
    return f"{value:g}"


def _receipt_nutrition(value, *, calories_only=False):
    _receipt_require(isinstance(value, dict))
    keys = ("kcal",) if calories_only else _RECEIPT_NUTRIENTS
    return "、".join(LABELS[key] + _receipt_number(value.get(key)) + ("千卡" if key == "kcal" else "克") for key in keys)


def _receipt_place(value, day=None):
    day = value.get("date", day)
    meal = value.get("meal_type", "unknown")
    _receipt_require(isinstance(day, str) and bool(day) and meal in _MEAL_NAMES)
    return day + _MEAL_NAMES[meal]


def _receipt_quantity(value, *, pending=False):
    snap = value.get("snapshot", {})
    _receipt_require(isinstance(snap, dict))
    # Scaling preserves the source snapshot's original unit count. The tool's
    # current grams remain authoritative; do not repeat that old count as new.
    source_quantity = {} if "reuse_ratio" in snap else snap
    quantity = value.get("quantity", source_quantity.get("quantity"))
    unit = value.get("unit", source_quantity.get("unit", ""))
    grams = value.get("grams")
    text = value.get("quantity_text", source_quantity.get("quantity_text", ""))
    known = []
    if quantity is not None:
        _receipt_require(isinstance(unit, str) and (bool(unit) or pending))
        known.append((_receipt_number(quantity) + unit) if unit else "数量" + _receipt_number(quantity))
    if grams is not None:
        known.append(("可食" if known else "") + _receipt_number(grams) + "克")
    if text and text not in known:
        known.append(str(text))
    _receipt_require(pending or bool(known))
    return "（" + "，".join(known) + "）" if known else ""


def _receipt_item(value, *, day=None, nutrients=False):
    _receipt_require(isinstance(value, dict))
    name = value.get("food_name", value.get("matched_name"))
    _receipt_require(isinstance(name, str) and bool(name))
    text = _receipt_place(value, day) + " " + name + _receipt_quantity(value)
    if nutrients:
        _receipt_require(isinstance(value.get("snapshot"), dict) and isinstance(value["snapshot"].get("estimated"), bool))
        text += "：" + _receipt_nutrition(value.get("nutrition"))
        if value["snapshot"]["estimated"]:
            text += "（份量为估算）"
    return text


def _receipt_unfinished(value, *, cancelled=False, revising=False):
    _receipt_require(isinstance(value, dict) and bool(value.get("food_query")))
    text = _receipt_place(value) + " " + value["food_query"] + _receipt_quantity(value, pending=True)
    if cancelled:
        return "已取消未完成项：" + text + "。"
    reason = value.get("reason")
    _receipt_require(isinstance(reason, str) and bool(reason))
    text = ("尚未修改：" if revising else "尚未记录：") + text + "；" + reason + "。"
    candidates = value.get("candidates", [])
    _receipt_require(isinstance(candidates, list))
    for index, candidate in enumerate(candidates, 1):
        _receipt_require(isinstance(candidate, dict) and bool(candidate.get("name")))
        traits = [str(candidate[key]) for key in ("brand", "category", "type", "source") if candidate.get(key)]
        if candidate.get("aliases"):
            traits.append("别名：" + "、".join(map(str, candidate["aliases"])))
        for unit in candidate.get("units", []):
            _receipt_require(isinstance(unit, dict))
            name = unit.get("unit_name", unit.get("unit", unit.get("name")))
            _receipt_require(bool(name))
            traits.append(str(name) + ("可食" + _receipt_number(unit["eat_weight"]) + "克" if unit.get("eat_weight") is not None else ""))
        text += "\n" + str(index) + ". " + candidate["name"] + ("（" + "；".join(traits) + "）" if traits else "")
    return text


def _receipt_estimated(data):
    if isinstance(data.get("estimated"), bool):
        return data["estimated"]
    items = data.get("items")
    _receipt_require(isinstance(items, list) and len(items) == data.get("count"))
    _receipt_require(all(isinstance(item, dict) and isinstance(item.get("snapshot"), dict)
                         and isinstance(item["snapshot"].get("estimated"), bool) for item in items))
    return any(item["snapshot"]["estimated"] for item in items)


def _receipt_query(tool, data):
    count = data.get("count")
    _receipt_require(isinstance(count, int) and not isinstance(count, bool) and count >= 0)
    # Full persisted results always retain every item, even for summary mode.
    items = data.get("items")
    _receipt_require(isinstance(items, list) and len(items) == count and all(isinstance(item, dict) for item in items))
    detail = data.get("detail")
    if tool == "query_today":
        _receipt_require(detail in {"calories", "summary", "items"})
        scope = _receipt_place({"date": data.get("date")})
    else:
        _receipt_require(detail in {"summary", "daily", "items"})
        start, end = data.get("start_date"), data.get("end_date")
        _receipt_require(isinstance(start, str) and bool(start) and isinstance(end, str) and bool(end))
        scope = start + " 至 " + end + "（不含尾日）"
        if data.get("meal_type"):
            _receipt_require(data["meal_type"] in _MEAL_NAMES)
            scope += " " + (_MEAL_NAMES[data["meal_type"]] or "未指定餐次")
        if data.get("food_query"):
            scope += "，食物名称包含“" + data["food_query"] + "”"
    if count == 0:
        # A zero ledger total does not prove that the user did not eat.
        parts = [scope + "未记录。"]
    else:
        nutrition = _receipt_nutrition(data.get("total"), calories_only=detail == "calories")
        parts = [scope + f"已记录{count}项，合计" + nutrition + ("（含估算份量）" if _receipt_estimated(data) else "") + "。"]
    if tool == "query_today" and detail in {"summary", "items"}:
        parts += ["目标：" + _receipt_nutrition(data.get("targets")) + "。", "剩余：" + _receipt_nutrition(data.get("remaining")) + "。"]
    if detail == "items":
        parts.extend(_receipt_item(item, day=data.get("date"), nutrients=True) + "。" for item in items)
    if detail == "daily":
        daily = data.get("daily")
        _receipt_require(isinstance(daily, list) and bool(daily) and all(isinstance(day, dict) and isinstance(day.get("count"), int) for day in daily))
        _receipt_require(sum(day.get("count", -1) for day in daily) == count)
        for day in daily:
            place = _receipt_place(day)
            if day.get("status") == "unrecorded" and day.get("count") == 0:
                parts.append(place + "未记录。")
            else:
                _receipt_require(day.get("status") == "recorded" and isinstance(day.get("count"), int) and day["count"] > 0)
                selected = [item for item in items if item.get("date") == day["date"]]
                estimated = _receipt_estimated({"items": selected, "count": day["count"]})
                parts.append(place + f"已记录{day['count']}项，合计" + _receipt_nutrition(day.get("total")) + ("（含估算份量）" if estimated else "") + "。")
    return parts


def tool_facts_reply(facts) -> str | None:
    """Render this logical message's ordered, complete persisted tool results.

    Completion declarations belong to the native runtime. This function checks
    only supported result shapes and fact freshness, never user-text keywords.
    """
    if not isinstance(facts, list) or not facts:
        return None
    try:
        parts, latest_meals, queries = [], {}, []
        record_pending, other_pending = [], []
        for fact in facts:
            _receipt_require(isinstance(fact, dict) and isinstance(fact.get("tool"), str) and fact["tool"] in _LEDGER_TOOLS)
            _receipt_require(isinstance(fact.get("status"), str) and fact["status"] in {"ok", "pending"} and not fact.get("error"))
            tool, data, status = fact["tool"], fact.get("data"), fact["status"]
            _receipt_require(isinstance(data, dict))
            if tool in {"query_today", "query_history"}:
                _receipt_require(status == "ok")
                parts.extend(_receipt_query(tool, data))
                queries.append((tool, data))
                continue
            written = data.get("items", []) if tool == "record_meal" else ([data["item"]] if data.get("item") else [])
            _receipt_require(isinstance(written, list))
            choices = written if status == "pending" and data.get("question") else []
            if choices:
                written = []
            meals = data.get("meal_totals", [])
            _receipt_require(isinstance(meals, list))
            if written:
                _receipt_require(status == "ok" and bool(meals))
                # A prior ambiguous revision/selection cannot be assumed to
                # have been resolved merely because some later write succeeded.
                _receipt_require(not other_pending)
                if tool == "record_meal":
                    _receipt_require(data.get("completed_count") == len(written) and data.get("write_status") in {"created", "duplicate_skipped"})
                    action = "已记录："
                else:
                    _receipt_require(data.get("operation") in {"revised", "deleted"})
                    action = "已撤销：" if data["operation"] == "deleted" else "已修改："
                parts.append(action + "、".join(_receipt_item(item) for item in written) + "。")
                for meal in meals:
                    _receipt_require(isinstance(meal, dict) and isinstance(meal.get("meal_id"), str) and bool(meal["meal_id"]) and isinstance(meal.get("estimated"), bool))
                    _receipt_place(meal)
                    _receipt_nutrition(meal.get("total"), calories_only=True)
                    latest_meals[meal["meal_id"]] = meal
                affected = {item.get("snapshot", {}).get("meal_id", item.get("message_id")) for item in written}
                _receipt_require(affected == {meal["meal_id"] for meal in meals})
                dates = {meal["date"] for meal in meals}
                for query_tool, query in queries:
                    stale = (query.get("date") in dates if query_tool == "query_today" else
                             any(query["start_date"] <= day < query["end_date"] for day in dates))
                    _receipt_require(not stale)
            else:
                _receipt_require(not meals and (tool != "record_meal" or data.get("completed_count", 0) == 0))
            cancelled = data.get("cancelled", [])
            _receipt_require(isinstance(cancelled, list) and all(isinstance(ident, str) for ident in cancelled))
            if cancelled:
                cancelled_items = data.get("cancelled_items")
                _receipt_require(isinstance(cancelled_items, list) and all(isinstance(item, dict) for item in cancelled_items)
                                 and {item.get("pending_id") for item in cancelled_items} == set(cancelled))
                parts.extend(_receipt_unfinished(item, cancelled=True) for item in cancelled_items)
            unfinished = data.get("unfinished", [])
            _receipt_require(isinstance(unfinished, list))
            missing = [_receipt_unfinished(item, revising=tool == "revise_intake") for item in unfinished]
            if tool == "record_meal" and "unfinished" in data:
                # record_meal returns the full active pending snapshot; a later
                # completion or cancellation supersedes earlier missing rows.
                record_pending = missing
            else:
                other_pending.extend(missing)
            question = data.get("question")
            if question:
                _receipt_require(isinstance(question, str))
                other_pending.append(question)
                other_pending.extend(str(index) + ". " + _receipt_item(item) for index, item in enumerate(choices, 1))
            _receipt_require(bool(written or cancelled or unfinished or question))
        for index, meal in enumerate(latest_meals.values(), 1):
            place = _receipt_place(meal)
            label = (f"第{index}餐（{place}）" if len(latest_meals) > 1 else place + "这餐")
            parts.append(label + "当前合计" + _receipt_number(meal["total"]["kcal"]) + "千卡。" + ("份量含估算。" if meal["estimated"] else ""))
        parts.extend(record_pending + other_pending)
        return "\n".join(parts) or None
    except _IncompleteReceipt:
        return None
