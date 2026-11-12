"""The seven specification acceptance scenarios; native tool-calling fake model."""
from copy import deepcopy
from dataclasses import replace
import json
from typing import Any
from pydantic import Field
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.outputs import ChatResult, ChatGeneration
from langchain_core.utils.function_calling import convert_to_openai_tool
from agent import FakeStore, run
from agent.config import Config, DEFAULT_ALLOWED_TOOLS
from agent.storage import boohee_client

A = {"food_id": "A", "name": "夹具A", "aliases": [], "carbs_per_100g": 10, "protein_per_100g": 2, "fat_per_100g": 1, "kcal_per_100g": 60}
B = {"food_id": "B", "name": "夹具B", "aliases": [], "carbs_per_100g": 0, "protein_per_100g": 12, "fat_per_100g": 10, "kcal_per_100g": 150,
     "boohee_id": "B-code"}
CONFIG = Config("shadow_fakewrite", "deepseek", "fake-native", DEFAULT_ALLOWED_TOOLS, 8)
MEAL = {"items": [{"food_query": "夹具A", "grams": 200}, {"food_query": "夹具B", "quantity": 2, "unit": "个", "quantity_text": "2个"}], "meal_type": "lunch"}

def transport(url, headers):
    if "/detail?" in url:
        assert "code=B-code" in url and "with_units=true" in url
        return {"code": 0, "data": {"code": "B-code", "name": "夹具B", "carbohydrate": 0, "protein": 12, "fat": 10, "calories": 150,
            "units": [{"unit": "个", "weight": 60, "eat_weight": 50}]}}
    return {"code": 0, "data": {"foods": [{"code": "fish", "name": "鱼（生）", "carbohydrate": 0, "protein": 20, "fat": 2, "calories": 110}]}}

class NativeFake(BaseChatModel):
    responses: list[Any] = Field(default_factory=list)
    requests: list[Any] = Field(default_factory=list)
    schemas: list[Any] = Field(default_factory=list)
    @property
    def _llm_type(self): return "native_acceptance_fake"
    def bind_tools(self, tools, **kwargs):
        self.schemas.append([convert_to_openai_tool(t) for t in tools])
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools])
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.requests.append(deepcopy(messages))
        response = self.responses.pop(0) if self.responses else answer
        message = response(messages) if callable(response) else response
        return ChatResult(generations=[ChatGeneration(message=message)])

def call(name, args, cid="c1"):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid, "type": "tool_call"}])

def answer(messages):
    facts=[json.loads(m.content) for m in messages if isinstance(m,ToolMessage)]
    facts=[f for f in facts if f.get('status')!='historical']
    f=facts[-1] if facts else {};d=f.get('data',{});tool=f.get('tool')
    if tool=='record_meal':
        if d.get('cancelled') and not d.get('items'):return AIMessage(content='已取消未完成项。')
        missing=d.get('unfinished',[])
        if missing:return AIMessage(content='已记好能确定的食物；'+ '；'.join(i['food_query']+'：'+i['reason']+'（'+ '；'.join(f"{n+1}. {c['name']}" for n,c in enumerate(i.get('candidates',[])))+'）' for i in missing))
        return AIMessage(content=f"记好了，这餐{d['meal_totals'][0]['total']['kcal']:g}千卡，按单位换算的份量为估算。")
    if tool=='revise_intake':return AIMessage(content=f"已修改，这餐{d['meal_totals'][0]['total']['kcal']:g}千卡。"+('按单位换算的份量为估算。' if d['meal_totals'][0].get('estimated') else ''))
    if tool=='query_today':return AIMessage(content=f"今天合计{d['total']['kcal']:g}千卡。")
    if tool=='update_user_settings':
        if f['status']=='pending':return AIMessage(content='请说一下这次想修改的内容。')
        previous=[x for x in facts if x.get('tool')=='record_meal']
        return AIMessage(content='这个安排可以。' if f['status']=='proposal' else ('夹具A已记录，这餐120千卡；长期热量目标已设为1900千卡。' if previous else '设置已保存。'))
    if tool=='query_knowledge':return AIMessage(content='主食可以与蔬菜搭配，结合整天的安排。[K101]' if d.get('entries') else '可以先从规律吃饭和方便坚持的搭配入手。你说的这个具体情形还缺对应资料。')
    return AIMessage(content='好，先放一放，想聊别的也可以。')

def setup():
    boohee_client.set_transport(transport)
    return FakeStore(foods=deepcopy([A,B]), today="2026-10-31")

def send(store, text, steps, mid="m1", chat="test", config=CONFIG, **kw):
    model=NativeFake(responses=steps)
    result=run(dict(message_id=mid,user_id="user",chat_id=chat,text=text),store=store,config=config,model=model,**kw)
    assert result["status"] == "ok", result["errors"]
    return result,model

def U01():
    """来源热量、可食单位、整餐、实际快照和独立预期。"""
    s=setup()
    # A redundant size qualifier is resolved only by this food's source unit.
    s.foods[1]["units"]=[{"unit":"个(中)","weight":60,"eat_weight":50}]
    s.foods[1]["detail_loaded"]=True
    meal=deepcopy(MEAL)
    meal["items"][1].update(food_query="夹具B（中号）",unit="个(中)")
    r,m=send(s,"记录夹具A200g和夹具B2个（中号）",[call("record_meal",meal),answer])
    assert len(s.intake)==2
    assert [(x["grams"],x["nutrition"]) for x in s.intake] == [(200,{"carbs":20,"protein":4,"fat":2,"kcal":120}),(100,{"carbs":0,"protein":12,"fat":10,"kcal":150})]
    assert s.intake[1]["snapshot"]["estimated"] is True
    assert s.today_summary("user")["totals"] == {"carbs":20,"protein":16,"fat":12,"kcal":270}
    assert "270千卡" in r["reply"] and "估算" in r["reply"],r["reply"]
    assert isinstance(m.requests[1][-1],ToolMessage)
    again,_=send(s,"记录夹具A200g和夹具B2个",[],mid="m1")
    assert again["reply"]==r["reply"] and len(s.intake)==2
    # A requested amount is independent of an existing same-name intake.
    for mid, params, expected in (
        ("nutrition-grams", {"food_query":"夹具A","grams":100}, 60),
        ("nutrition-unit", {"food_query":"夹具B","quantity":1,"unit":"个(中)"}, 75),
        ("nutrition-record", {"record_id":s.intake[0]["record_id"],"grams":100}, 120),
    ):
        result,_=send(s,"查询指定份量的营养",[call("query_food_nutrition",params),AIMessage(content="查询完成。")],mid=mid)
        data=result["tool_results"][0]["data"]
        total=data.get("total") or data["items"][0]["nutrition"]
        assert total["kcal"]==expected
    assert len(s.intake)==2
    send(s,"记录明天夹具A400克",[call("record_meal",{"date":"2026-11-01","items":[{"food_query":"夹具A","grams":400}]}),answer],mid="future-intake")
    result,_=send(s,"查今天夹具A的营养",[call("query_food_nutrition",{"food_query":"夹具A"}),AIMessage(content="查询完成。")],mid="nutrition-today")
    assert result["tool_results"][0]["data"]["total"]["kcal"]==120

def U02():
    """部分成功、合并缺项、候选快照、跨日补全与单结果歧义。"""
    s=setup()
    steps={"items":[{"food_query":"夹具A","grams":200},{"food_query":"鱼","grams":100},{"food_query":"夹具B"}]}
    r,_=send(s,"记录A200g、鱼100g和B",[call("record_meal",steps),answer])
    assert len(s.intake)==1 and len(r["tool_results"][0]["data"]["unfinished"])==2
    pending=s.load_session("user","test")["unfinished"]
    fish=next(v for v in pending.values() if v["food_query"]=="鱼")
    b=next(v for v in pending.values() if v["food_query"]=="夹具B")
    assert len(fish["candidates"]) == 1 and fish["unmet_conditions"][0]["code"] == "food_identity_unverified"
    assert "鱼" in r["reply"] and "夹具B" in r["reply"]
    s.today="2026-11-01"
    def detail(url,headers):
        if "code=fish" in url: return {"code":0,"data":{"code":"fish","name":"鱼（生）","carbohydrate":0,"protein":20,"fat":2,"calories":110,"units":[]}}
        return transport(url,headers)
    boohee_client.set_transport(detail)
    r,_=send(s,"B是2个，鱼选第1个",[call("record_meal",{"items":[
        {"food_query":"夹具B","pending_id":b["pending_id"],"quantity":2,"unit":"个"},
        {"food_query":"鱼","pending_id":fish["pending_id"],"choice":1}]}),answer],mid="m2")
    assert len(s.intake)==3 and all(r["date"]=="2026-10-31" for r in s.intake)
    assert [x["grams"] for x in s.intake]==[200,100,100]
    assert not s.load_session("user","test")["unfinished"]
    send(s,"B是2个，鱼选第1个",[],mid="m2")
    assert len(s.intake)==3
    send(s,"记录夹具A",[call("record_meal",{"items":[{"food_query":"夹具A"}]}),answer],mid="m3")
    pending_id=next(iter(s.load_session("user","test")["unfinished"]))
    cancelled,_=send(s,"取消",[call("record_meal",{"cancel_pending":[pending_id]}),answer],mid="m4")
    assert not s.load_session("user","test")["unfinished"] and "已取消未完成项" in cancelled["reply"]
    assert len(s.intake)==3

    replacement=setup()
    send(replacement,"记录夹具A",[call("record_meal",{"items":[{"food_query":"夹具A"}]}),answer])
    original=next(iter(replacement.load_session("user","test")["unfinished"].values()))
    send(replacement,"刚才那项换成夹具B100克",[call("record_meal",{"items":[{"pending_id":original["pending_id"],"food_query":"夹具B","grams":100}]}),answer],mid="replace-food")
    row=replacement.intake[0]
    assert (row["food_name"],row["grams"],row["nutrition"]["kcal"])==("夹具B",100,150)
    assert row["date"]==original["date"] and row["snapshot"]["meal_id"]==original["meal_id"]

    revised_amount=setup()
    send(revised_amount,"记录夹具100克",[call("record_meal",{"items":[{"food_query":"夹具","grams":100}]}),answer])
    original=next(iter(revised_amount.load_session("user","test")["unfinished"].values()))
    assert original["candidates"][1]["name"]=="夹具B"
    run(dict(message_id="conflicting-choice",user_id="user",chat_id="test",text="选择与食物矛盾的候选"),store=revised_amount,config=CONFIG,
        model=NativeFake(responses=[call("record_meal",{"items":[{"pending_id":original["pending_id"],"food_query":"夹具A","choice":2,"quantity":1,"unit":"个"}]}),AIMessage(content="请重新选择。")]))
    assert not revised_amount.intake and original["pending_id"] in revised_amount.load_session("user","test")["unfinished"]
    send(revised_amount,"选第二个，改为1个",[call("record_meal",{"items":[{"pending_id":original["pending_id"],"food_query":"夹具B","choice":2,"quantity":1,"unit":"个"}]}),answer],mid="replace-amount")
    row=revised_amount.intake[0]
    assert (row["food_name"],row["grams"],row["nutrition"]["kcal"])==("夹具B",50,75)
    assert row["snapshot"]["estimated"] and row["snapshot"]["quantity"]==1

def U03():
    """快照常用餐、半份复用、指定项修改和撤销。"""
    s=setup()
    send(s,"记录整餐并保存常用餐为午餐",[call("record_meal",MEAL),call("update_user_settings",{"kind":"favorite","values":{"name":"午餐"}},"c2"),answer])
    def unavailable(*args): raise AssertionError("reuse must not query supplier")
    boohee_client.set_transport(unavailable)
    r,_=send(s,"记半份午餐",[call("record_meal",{"reuse":{"favorite":"午餐","ratio":0.5}}),answer],mid="m2")
    assert len(s.intake)==4 and [x["nutrition"]["kcal"] for x in s.intake]==[120,150,60,75]
    send(s,"刚才A改50g，撤销刚才B，再查今天",[
        call("revise_intake",{"food_query":"夹具A","grams":50}),
        call("revise_intake",{"food_query":"夹具B","delete":True},"c2"),call("query_today",{},"c3"),answer],mid="m3")
    assert [x["nutrition"]["kcal"] for x in s.intake[:2]]==[120,150]
    assert s.today_summary("user")["totals"]["kcal"]==300 and s.today_summary("user")["count"]==3
    assert s.intake[2]["grams"]==50 and s.intake[2]["nutrition"]["kcal"]==30 and s.intake[3]["status"]=="deleted"
    s.today="2026-11-01"
    result,_=send(s,"再记半份刚才那餐",[call("record_meal",{"reuse":{"latest":True,"ratio":.5}}),answer],mid="midnight-reuse")
    assert len(s.intake)==5 and (s.intake[-1]["date"],s.intake[-1]["grams"],s.intake[-1]["nutrition"]["kcal"])==("2026-11-01",25,15)
    result,_=send(s,"只复用昨天的最近餐",[call("record_meal",{"reuse":{"latest":True,"date":"yesterday"}}),AIMessage(content="日期不匹配，请指定原餐。")],mid="explicit-source-date")
    assert result["tool_results"][0]["status"]=="pending" and len(s.intake)==5

def U04():
    """跨轮知识与待办、Store恢复、工具schema及结果回传、会话隔离和完全只读。"""
    s=setup()
    send(s,"记录夹具A",[call("record_meal",{"items":[{"food_query":"夹具A"}]}),answer])
    pending=next(iter(s.load_session("user","test")["unfinished"].values()))
    send(s,"减脂期间可以吃米饭吗",[call("query_knowledge",{"query":"减脂期间可以吃米饭吗"}),answer],mid="m2")
    r,m=send(s,"先看看今天",[call("query_today",{}),answer],mid="m3")
    assert any("米饭" in str(x.content) for x in m.requests[0])
    assert m.schemas[0][0]["function"]["description"]
    assert {x["function"]["name"] for x in m.schemas[0]}==set(DEFAULT_ALLOWED_TOOLS)
    assert m.schemas[0][0]["function"]["parameters"]["properties"]
    assert any(isinstance(x,ToolMessage) for x in m.requests[-1])
    # Rebuild a new Store object from persisted state; runtime has no conversation cache.
    rebuilt=deepcopy(s)
    r,_=send(rebuilt,"刚才那个是200g",[call("record_meal",{"items":[{"food_query":"夹具A","pending_id":pending["pending_id"],"grams":200}]}),answer],mid="m4")
    assert len(rebuilt.intake)==1 and rebuilt.intake[0]["nutrition"]["kcal"]==120
    send(rebuilt,"那个呢",[call("query_today",{}),answer],mid="other",chat="different")
    assert not rebuilt.load_session("user","different").get("unfinished")
    # Real Bitable serialization must preserve native history beyond one cell's
    # plain-text budget, including old plain JSON rows on read.
    from agent.storage.bitable_adapter import BitableStore, _pending_field
    adapter=object.__new__(BitableStore)
    adapter.pending_table="pending"
    captured={}
    adapter._update_record=lambda table,rid,fields: captured.update(fields)
    durable=deepcopy(rebuilt.load_session("user","test"))
    durable["messages"].append({"type":"human","data":{"content":"食物原文与知识证据"*12000}})
    assert len(json.dumps(durable,ensure_ascii=False).encode())>100000
    adapter.save_execution({"record_id":"fixed"},durable)
    assert len(captured[_pending_field("payload")].encode())<100000
    assert adapter._pending_task("fixed",captured)["payload"]==durable
    assert adapter._pending_task("old",{_pending_field("payload"):json.dumps({"day":"2026-10-31"})})["payload"]=={"day":"2026-10-31"}
    ro=deepcopy(rebuilt)
    def forbidden(*a,**kw): raise AssertionError("readonly wrote persistent state")
    for name in ("save_execution","begin_execution","save_session","save_document","create_pending","append_usage_event","write_intake","cache_food"):
        setattr(ro,name,forbidden)
    r,m=send(ro,"查询今天",[call("query_today",{}),answer],mid="ro",config=replace(CONFIG,preset="shadow_readonly"))
    assert all(t["function"]["name"] not in {"record_meal","revise_intake","update_user_settings"} for t in m.schemas[0])

def U05():
    """直接设置、逐字段目标覆盖、邻接提案确认和计划/食用区分。"""
    s=setup()
    send(s,"长期热量2000，偏好少油",[call("update_user_settings",{"kind":"targets","values":{"kcal":2000}}),
        call("update_user_settings",{"kind":"preferences","values":{"cooking":"少油"}},"c2"),answer])
    assert s.get_standard("user")=={"carbs":250,"protein":150,"fat":70,"kcal":2000,"source":"fake"}
    send(s,"今天蛋白180，长期蛋白160",[call("update_user_settings",{"kind":"targets","scope":"today","values":{"protein":180}}),
        call("update_user_settings",{"kind":"targets","values":{"protein":160}},"c2"),answer],mid="m2")
    assert s.get_standard("user")["protein"]==180 and s.get_standard("user")["kcal"]==2000
    s.today="2026-11-01";assert s.get_standard("user")["protein"]==160
    proposal,_=send(s,"给点目标建议",[call("update_user_settings",{"mode":"propose","kind":"targets","values":{"kcal":1900}}),answer],mid="m3")
    from agent.graph import mark_reply_presented
    mark_reply_presented(s,"user","test",proposal)
    assert s.get_standard("user")["kcal"]==2000
    send(s,"好的",[call("update_user_settings",{"mode":"confirm","kind":"targets","proposal_id":proposal["proposal_id"],"authorization_quote":"好的"}),answer],mid="m4")
    assert s.get_standard("user")["kcal"]==1900
    send(s,"再建议",[call("update_user_settings",{"mode":"propose","kind":"targets","values":{"kcal":1800}}),answer],mid="m5")
    send(s,"换个话题今天吃了什么",[call("query_today",{}),answer],mid="m6")
    send(s,"好的",[call("update_user_settings",{"mode":"confirm","kind":"targets","authorization_quote":"好的"}),answer],mid="m7")
    assert s.get_standard("user")["kcal"]==1900
    r,_=send(s,"帮我计划A200g和B2个",[call("calculate_meal",{"items":MEAL["items"]}),AIMessage(content="这个方案合计270kcal。")],mid="m8")
    assert r["tool_results"][0]["data"]["total"]["kcal"]==270
    assert ("270kcal" in r["reply"] or "270千卡" in r["reply"]) and len(s.intake)==0
    send(s,"采用这个计划",[AIMessage(content="好的，按此方案安排。")],mid="m9")
    assert len(s.intake)==0
    send(s,"吃完了，记录这餐",[call("record_meal",MEAL),answer],mid="m10")
    assert len(s.intake)==2

def U06():
    """假时钟提醒开关、唯一发送、已有餐食与缺失信息；无模型调用。"""
    from datetime import datetime, timezone
    from agent.storage.reminders import check_reminders
    s=setup();sent=[]
    s.save_document("user_settings","user","",{"feishu_chat_id":"own-feishu"})
    tick=datetime(2026,10,31,4,0,tzinfo=timezone.utc)
    sender=lambda chat,text,uuid_key: sent.append((chat,text,uuid_key)) or {"message_id":"sent"}
    assert check_reminders(s,tick,sender)["sent"]==0
    send(s,"每天12点提醒午餐",[call("update_user_settings",{"kind":"reminder","values":{"enabled":True,"time":"12:00","meal_type":"lunch"}}),answer])
    assert check_reminders(s,tick,sender)["sent"]==1 and len(sent)==1 and sent[0][0]=="own-feishu"
    assert check_reminders(s,tick,sender)["sent"]==0 and len(sent)==1
    send(s,"记录午餐",[call("record_meal",MEAL),answer],mid="m2")
    # Reset only the delivery receipt to isolate the already-recorded branch.
    receipt=next(t for t in s.pending if t["intent"]=="reminder_receipt");receipt["payload"]["sent"]=False
    assert check_reminders(s,tick,sender)["sent"]==0
    s2=setup()
    send(s2,"开启提醒",[call("update_user_settings",{"kind":"reminder","values":{"enabled":True}}),answer])
    rules=s2.load_document("user_settings","user")["reminders"]
    assert all(not r["enabled"] for r in rules.values())
    assert check_reminders(s2,tick,sender)["sent"]==0

def U07():
    """写成功/结果未保存时崩溃，恢复连续两动作、原日期及原回复，重放不覆盖新会话。"""
    from agent.graph import run
    s=setup()
    original=s.commit_action
    batch=AIMessage(content="",tool_calls=[{"name":"record_meal","args":{"items":[{"food_query":"夹具A","grams":200}]},"id":"c1","type":"tool_call"},
        {"name":"record_meal","args":{"items":[{"food_query":"夹具B","quantity":2,"unit":"个"}]},"id":"c2","type":"tool_call"}])
    # Force a process death rather than a retryable business error.
    class Death(BaseException): pass
    def dead(action_id,callback):
        result=original(action_id,callback)
        if action_id=='m1:agent:1': raise Death()
        return result
    s.commit_action=dead
    msg=dict(message_id="m1",user_id="user",chat_id="test",text="记录两项")
    try: run(msg,store=s,config=CONFIG,model=NativeFake(responses=[batch,answer]))
    except Death: pass
    else: raise AssertionError("crash was not injected")
    assert len(s.intake)==1
    s.commit_action=original;s.today="2026-11-01"
    r=run(msg,store=s,config=CONFIG,model=NativeFake(responses=[answer]))
    assert r["status"]=="ok",r["errors"]
    assert len(s.intake)==2 and {x["date"] for x in s.intake}=={"2026-10-31"}
    assert [x["nutrition"]["kcal"] for x in s.intake]==[120,150]
    r2=run(msg,store=s,config=CONFIG,model=NativeFake(responses=[]))
    assert len(s.intake)==2 and r2["reply"]==r["reply"]
    send(s,"再记夹具A",[call("record_meal",{"items":[{"food_query":"夹具A"}]}),answer],mid="later")
    latest_session=s.load_session("user","test")
    run(msg,store=s,config=CONFIG,model=NativeFake(responses=[]))
    assert s.load_session("user","test")==latest_session
    assert s.load_execution(msg)["status"]=="completed"
    # Transport recovery now belongs to the production ingress boundary;
    # The P cases exercise production transport recovery independently.



CHECKS={f"U{i:02d}":globals()[f"U{i:02d}"] for i in range(1,8)}
