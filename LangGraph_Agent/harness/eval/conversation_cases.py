"""Six conversation acceptance groups sharing the existing runner and fixtures."""
from copy import deepcopy
from dataclasses import replace
import json
import re
from pathlib import Path
from langchain_core.messages import AIMessage,ToolMessage
from agent import run
from agent.model import DeepSeekModel
from agent.graph import mark_reply_presented
from agent.replies import model_view,render
from harness.eval.personal_cases import setup,CONFIG,MEAL,NativeFake,call
REAL=False
TRANSCRIPT=[]

def answer(messages):
    facts=[json.loads(m.content) for m in messages if isinstance(m,ToolMessage)]
    f=facts[-1] if facts else {};d=f.get('data',{});tool=f.get('tool')
    if tool=='record_meal':
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

def send(s,text,steps,mid,delivered=True,config=CONFIG):
    model=DeepSeekModel(model_name='deepseek-v4-flash') if REAL else NativeFake(responses=steps)
    r=run(dict(message_id=mid,user_id='user',chat_id='test',text=text),store=s,config=replace(config,model='deepseek-v4-flash') if REAL else config,model=model)
    TRANSCRIPT.append({'id':mid,'user':text,'result':r})
    assert r['status']=='ok',r['errors']
    assert not r['guard_result']['fallback_used'],r
    if delivered:mark_reply_presented(s,'user','test',r)
    return r

def kcal(r,value):
    assert re.search(r'(?<!\d)'+str(value)+r'(?:\.0)?\s*(?:kcal|千卡|大卡)',r['reply'],re.I),r['reply']

def E01():
    """整餐、改量常用餐、半份复用、只问今日热量；独立数值与必要调用。"""
    s=setup()
    r=send(s,'记录午餐：夹具A200克，夹具B2个。',[call('record_meal',MEAL),answer],'e1a');kcal(r,270)
    assert len(s.intake)==2 and [i['grams'] for i in s.intake]==[200,100]
    r=send(s,'刚才夹具A改为100克，夹具B不变，把这两项保存为常用餐“午餐20261101”。',[
        call('revise_intake',{'food_query':'夹具A','grams':100}),call('update_user_settings',{'kind':'favorite','values':{'name':'午餐20261101'}},'c2'),
        lambda m:AIMessage(content='已改好并保存为午餐20261101，这餐210千卡，夹具B按单位换算，为估算。')],'e1b');kcal(r,210)
    r=send(s,'今天加餐记录半份常用餐“午餐20261101”。',[call('record_meal',{'reuse':{'favorite':'午餐20261101','ratio':.5},'meal_type':'snack'}),answer],'e1c');kcal(r,105)
    r=send(s,'今天摄入合计多少热量？',[call('query_today',{}),answer],'e1d');kcal(r,315)
    assert s.today_summary('user')['totals']=={'carbs':15,'protein':21,'fat':16.5,'kcal':315}
    assert len(s.intake)==4
    assert '估算' in TRANSCRIPT[-3]['result']['reply']
    for t in TRANSCRIPT[-4:]:
        assert len(t['result']['reply'])<200
        assert not all(k in t['result']['reply'] for k in ('碳水','蛋白','脂肪'))
    for t in (TRANSCRIPT[-4],TRANSCRIPT[-2],TRANSCRIPT[-1]):assert len(t['result']['native_calls'])==1 and t['result']['model_calls']<=2
    assert len(TRANSCRIPT[-3]['result']['native_calls'])==2 and TRANSCRIPT[-3]['result']['model_calls']<=3

def E02():
    """缺项合并询问、换话题保留信息、跨日候选补全。"""
    from agent.storage import boohee_client
    from harness.eval.personal_cases import transport
    s=setup();r=send(s,'记录夹具A200克、鱼100克、夹具B。',[call('record_meal',{'items':[{'food_query':'夹具A','grams':200},{'food_query':'鱼','grams':100},{'food_query':'夹具B'}]}),answer],'e2a')
    assert len(s.intake)==1
    assert '1.' in r['reply'] and '鱼（生）' in r['reply'],r['reply']
    pending=s.load_session('user','test')['unfinished'];assert len(pending)==2
    saved=deepcopy(pending);send(s,'先不聊这个了。',[AIMessage(content='好，先放一放。')],'e2b');assert s.load_session('user','test')['unfinished']==saved
    fish=next(v for v in pending.values() if v['food_query']=='鱼');b=next(v for v in pending.values() if v['food_query']=='夹具B')
    def detail(url,headers):
        if 'code=fish' in url:return {'code':0,'data':{'code':'fish','name':'鱼（生）','carbohydrate':0,'protein':20,'fat':2,'calories':110,'units':[]}}
        return transport(url,headers)
    boohee_client.set_transport(detail);s.today='2026-11-01'
    r=send(s,'夹具B是2个，鱼选候选1。',[call('record_meal',{'items':[{'food_query':'夹具B','pending_id':b['pending_id'],'quantity':2,'unit':'个'},{'food_query':'鱼','pending_id':fish['pending_id'],'choice':1}]}),answer],'e2c');kcal(r,380)
    assert len(s.intake)==3 and {i['date'] for i in s.intake}=={'2026-10-31'}
    assert not s.load_session('user','test')['unfinished']
    # A completed item retains its original meal when later reweighed/recounted.
    r=send(s,'刚才夹具B改为3个。',[call('revise_intake',{'food_query':'夹具B','quantity':3,'unit':'个'}),answer],'e2d');kcal(r,455)
    assert '估算' in r['reply'],r['reply']
    meal=r['tool_results'][0]['data']['meal_totals'][0]
    assert meal['count']==3 and meal['date']=='2026-10-31' and meal['estimated']
    r=send(s,'夹具B实际称重可食150克。',[call('revise_intake',{'food_query':'夹具B','grams':150}),answer],'e2e');kcal(r,455)
    assert '估算' not in r['reply'],r['reply']
    meal=r['tool_results'][0]['data']['meal_totals'][0]
    row=next(i for i in s.intake if i['food_name']=='夹具B')
    assert meal['count']==3 and not meal['estimated']
    assert row['snapshot']['basis']=='user_edible_grams' and row['snapshot']['quantity'] is None and row['snapshot']['unit']==''

def E03():
    """知识综合、无直接证据的一般解释、按需来源；无业务写入。"""
    import agent.tools.reads as reads
    old=reads.build_knowledge_retriever
    def retrieve(query,entries):
        if '火星' in query:return {'status':'not_found','entries':[],'evidence':''}
        if '全谷' in query:return {'status':'ok','entries':[{'entry_id':'fixture-k2','answer':'主食种类可以轮换。来源：轮换资料2024，第7段','source':'轮换资料2024，第7段'}],'evidence':'主食种类可以轮换。'}
        return {'status':'ok','entries':[{'entry_id':'fixture-k1','answer':'主食可以与蔬菜搭配，结合整天的饮食安排。来源：验收资料2022，第2段','source':'验收资料2022，第2段'}],'evidence':'主食可以与蔬菜搭配，结合整天的饮食安排。'}
    reads.build_knowledge_retriever=lambda kind:retrieve
    try:
        s=setup();r=send(s,'主食怎么搭配？给个简单解释和依据。',[call('query_knowledge',{'query':'主食搭配'}),answer],'e3a');assert '蔬菜' in r['reply']
        r=send(s,'火星饮食法有什么道理？',[call('query_knowledge',{'query':'火星饮食法'}),answer],'e3b');assert r['reply'] and not s.intake
        r=send(s,'把刚才主食搭配的来源给我看看。',[call('query_knowledge',{'query':'主食搭配'}),lambda m:AIMessage(content='原文是“主食可以与蔬菜搭配，结合整天的饮食安排。”来源：验收资料2022，第2段。[K101]')],'e3c');assert '2022' in r['reply']
        assert not s.load_document('user_settings','user')
        if not REAL:
            r=send(s,'分别查主食搭配和全谷轮换的依据。',[call('query_knowledge',{'query':'主食搭配'}),call('query_knowledge',{'query':'全谷轮换'},'c2'),AIMessage(content='两条依据分别见[K101]与[K201]。')],'e3multi')
            ids=[c['data']['entries'][0]['source_id'] for c in r['tool_results']]
            assert ids==['K101','K201'] and len(r['response_adjustments'])==2
            assert '分别见〔验收资料2022，第2段〕与〔轮换资料2024，第7段〕' in r['reply']

    finally:reads.build_knowledge_retriever=old

def E04():
    """两种自然提案解释、交付绑定、重建确认、取消及明确请求。"""
    for variant in ('我建议采用这个安排。','这个目标可以作为起点。'):
        s=setup();r=send(s,'先建议将长期热量设为1900千卡，等我确认后再生效。',[call('update_user_settings',{'mode':'propose','kind':'targets','values':{'kcal':1900}}),AIMessage(content=variant)],'e4a',False)
        assert s.get_standard('user')['kcal']!=1900
        assert not s.load_session('user','test')['proposal']['presented']
        mark_reply_presented(s,'user','test',r);s=deepcopy(s)
        send(s,'好的',[call('update_user_settings',{'mode':'confirm','kind':'targets','values':{'kcal':999}}),answer],'e4b');assert s.get_standard('user')['kcal']==1900
        send(s,'再建议设为1800，先问我。',[call('update_user_settings',{'mode':'propose','kind':'targets','values':{'kcal':1800}}),answer],'e4c')
        send(s,'取消',[AIMessage(content='好，沿用当前目标。')],'e4d')
        send(s,'好的',[AIMessage(content='好的。')],'e4e');assert s.get_standard('user')['kcal']==1900
        send(s,'再建议设为1800，先问我。',[call('update_user_settings',{'mode':'propose','kind':'targets','values':{'kcal':1800}}),answer],'e4g')
        send(s,'先不讨论目标了。',[AIMessage(content='好，先放下这件事。')],'e4h')
        send(s,'好的',[call('update_user_settings',{'mode':'confirm','kind':'targets','values':{'kcal':999}}),answer],'e4i')
        assert s.get_standard('user')['kcal']==1900
        send(s,'把长期热量改为2000千卡。' ,[call('update_user_settings',{'kind':'targets','values':{'kcal':2000}}),answer],'e4f');assert s.get_standard('user')['kcal']==2000
        assert all('update_user_settings' not in t['result']['reply'] for t in TRANSCRIPT[-9:])

def E05():
    """正常数字/名字不误拦、六行视图减半、完整结果不变、明细按需。"""
    error={'tool':'query_today','status':'limit','data':{},'error':'工具调用次数已达上限'}
    assert model_view(error)==error
    s=setup()
    for n in range(3):send(s,'记录夹具A200克和夹具B2个。',[call('record_meal',MEAL),answer],f'e5{n}')
    r=send(s,'今天合计多少热量？',[call('query_today',{}),lambda m:AIMessage(content='今天6项合计810千卡。')],'e5q');kcal(r,810)
    f=r['tool_results'][0];view=model_view(f);assert len(json.dumps(view,ensure_ascii=False))<=len(json.dumps(f,ensure_ascii=False))*.5
    assert 'snapshot' in f['data']['items'][0]
    assert render('已修改，常用餐20261101，今天6项，指南2022。',[])[0].startswith('已修改')
    r=send(s,'把今天每项的克重和碳水、蛋白质、脂肪都列给我。',[call('query_today',{'detail':'items'}),lambda m:AIMessage(content='夹具A200克：碳水20克、蛋白质4克、脂肪2克；夹具B100克：碳水0克、蛋白质12克、脂肪10克。今天三次记录的同款午餐各有上述两项。')],'e5detail')
    assert all(k in r['reply'] for k in ('碳水','蛋白','脂肪'))

def E06():
    """写后中断、固定两动作恢复、压缩和只读、发送防重。"""
    from harness.eval import personal_cases as old
    original=old.answer
    old.answer=answer
    try:old.U04();old.U07()
    finally:old.answer=original
    s=setup();original=s.save_execution
    class Death(BaseException):pass
    def crash(task,payload,**kwargs):
        if s.intake and any('result' in c for c in payload.get('calls',[])):raise Death()
        original(task,payload,**kwargs)
    s.save_execution=crash
    try:send(s,'记夹具A200克，并把长期热量目标设为1900千卡。',[AIMessage(content='',tool_calls=[{'name':'record_meal','args':{'items':[{'food_query':'夹具A','grams':200}]},'id':'c1','type':'tool_call'},{'name':'update_user_settings','args':{'kind':'targets','values':{'kcal':1900}},'id':'c2','type':'tool_call'}]),answer],'e6a')
    except Death:pass
    else:raise AssertionError('crash missing')
    s.save_execution=original;s.today='2026-11-01'
    recovered=send(s,'记夹具A200克，并把长期热量目标设为1900千卡。',[answer],'e6a')
    def verify_receipt(text):
        clauses=re.split('[，。；\n]',text)
        assert any('夹具A' in c and re.search('已记|记好|记入',c) and not re.search('未记|没记|没有记',c) for c in clauses),text
        assert not any('夹具B' in c and re.search('已记|记好|记入',c) for c in clauses),text
        assert any('目标' in c and '1900' in c for c in clauses),text
        kcal({'reply':text},120)
    verify_receipt(recovered['reply'])
    wrong='夹具B已记录，这餐120千卡；夹具A未记录，长期目标1900千卡。'
    try:verify_receipt(wrong)
    except AssertionError:pass
    else:raise AssertionError('wrong object/status accepted')
    assert len(s.intake)==1 and s.intake[0]['nutrition']['kcal']==120 and s.intake[0]['date']=='2026-10-31'
    assert s.get_standard('user')['kcal']==1900

    # Invoke the actual CLI/DSH adapters with a controlled native model.
    import cli,os
    from unittest.mock import patch
    def entry_run(message,**kw):
        steps=[call('update_user_settings',{'mode':'confirm','kind':'targets','values':{'kcal':999}}),answer] if message['text']=='好的' else [call('update_user_settings',{'mode':'propose','kind':'targets','values':{'kcal':1900}}),AIMessage(content='这个目标可以先试试。')]
        return run(message,model=NativeFake(responses=steps),**kw)
    local=setup()
    with patch.dict(os.environ,{'USER_ID_ALIASES':'local_user:user'}), patch.multiple(cli,FakeStore=lambda:local,load_config=lambda:CONFIG,run=entry_run,_interactive=lambda:iter(['先建议1900','好的'])):
        assert cli.main([])==0
    assert local.get_standard('user')['kcal']==1900
    import console.server as server
    dsh=setup();key='dsh:conversation-e6'
    with patch.multiple(server,run=entry_run,_console_user_id=lambda store:'user'):
        server._SESSIONS[key]={'store':dsh,'history':[]}
        try:
            for n,text in enumerate(('先建议1900','好的')):
                result=server.api_agent({'text':text,'session_id':'conversation-e6','message_id':str(n),'preset':'shadow_fakewrite'})
                assert result['status']=='ok',result
            assert dsh.get_standard('user')['kcal']==1900
        finally:server._SESSIONS.pop(key,None)

CHECKS={f'E{i:02}':f for i,f in enumerate((E01,E02,E03,E04,E05,E06),1)}
