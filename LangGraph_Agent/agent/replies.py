"""Natural replies, compact model facts, and explicit proposal presentation."""
import json
import re
from copy import deepcopy
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

LABELS = {"carbs":"碳水", "protein":"蛋白质", "fat":"脂肪", "kcal":"热量"}

def settings_view(settings):
    return {**{k:v for k,v in settings.items() if k not in {"favorites","portions","feishu_chat_id"}},
            "favorites": list(settings.get("favorites", {}))}

def model_view(fact):
    def compact(value):
        if isinstance(value,list): return [compact(v) for v in value]
        if not isinstance(value,dict): return value
        result = {k:compact(v) for k,v in value.items() if k not in {"snapshot","created_at","message_id","action_id","display","record_id_internal","meal_id","food_id","score"}}
        snap=value.get("snapshot",{})
        if snap:
            result["estimated"]=snap.get("estimated",False)
            result["source"]=snap.get("food",{}).get("source",snap.get("basis",""))
        if "units" in result: result["units"]=[{k:u[k] for k in ("unit_name","eat_weight") if k in u} for u in value["units"]]
        return result
    result=compact(fact)
    if fact.get("tool")=="query_today" and fact.get("status")=="ok":
        data=result["data"];detail=data.get("detail","calories")
        if detail=="calories":result["data"]={"date":data["date"],"count":data["count"],"total":{"kcal":data["total"]["kcal"]},"detail":detail}
        elif detail=="summary":data.pop("items",None)
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
        elif isinstance(message, AIMessage):
            scope = message.additional_kwargs.get("fact_scope")
            if business_turn or scope == "business":
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
