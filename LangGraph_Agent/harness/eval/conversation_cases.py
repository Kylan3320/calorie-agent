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
from agent.replies import model_view,render,state_view,history_messages
from harness.eval.personal_cases import setup,CONFIG,MEAL,NativeFake,call,answer
REAL=False
TRANSCRIPT=[]


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
    ordered_items=s.intake_items
    s.intake_items=lambda *args,**kw:list(reversed(ordered_items(*args,**kw)))
    r=send(s,'记录午餐：夹具A200克，夹具B2个。',[call('record_meal',{**MEAL,'finish_after_tools':True}),answer],'e1a');kcal(r,270)
    assert len(s.intake)==2 and [i['grams'] for i in s.intake]==[200,100]
    stored={row['food_name']:row['record_id'] for row in s.intake}
    assert all(item['record_id']==stored[item['matched_name']] for item in r['tool_results'][0]['data']['items'])
    r=send(s,'刚才夹具A改为100克，夹具B不变，把这两项保存为常用餐“午餐20261101”。',[
        call('revise_intake',{'food_query':'夹具A','grams':100}),call('update_user_settings',{'kind':'favorite','values':{'name':'午餐20261101'}},'c2'),
        lambda m:AIMessage(content='已改好并保存为午餐20261101，这餐210千卡，夹具B按单位换算，为估算。')],'e1b');kcal(r,210)
    r=send(s,'今天加餐记录半份常用餐“午餐20261101”。',[call('record_meal',{'reuse':{'favorite':'午餐20261101','ratio':.5},'meal_type':'snack','finish_after_tools':True}),answer],'e1c');kcal(r,105)
    r=send(s,'今天摄入合计多少热量？',[call('query_today',{'finish_after_tools':True}),answer],'e1d');kcal(r,315)
    assert s.today_summary('user')['totals']=={'carbs':15,'protein':21,'fat':16.5,'kcal':315}
    assert len(s.intake)==4
    assert re.search(r'估算|约\s*\d',TRANSCRIPT[-3]['result']['reply'])
    for t in TRANSCRIPT[-4:]:
        assert len(t['result']['reply'])<200
        assert not all(k in t['result']['reply'] for k in ('碳水','蛋白','脂肪'))
    for t in (TRANSCRIPT[-4],TRANSCRIPT[-2],TRANSCRIPT[-1]):
        assert len(t['result']['native_calls'])==1 and t['result']['model_calls']==1
        assert t['result']['reply_source']=='tool_facts' and t['result']['model_reply']==''
    assert len(TRANSCRIPT[-3]['result']['native_calls'])==2 and TRANSCRIPT[-3]['result']['model_calls']<=3

def E02():
    """缺项合并询问、换话题保留信息、跨日候选补全。"""
    from agent.storage import boohee_client
    from harness.eval.personal_cases import transport
    if not REAL:
        from unittest.mock import patch
        refreshed=setup()
        with patch.object(boohee_client,'search_food',return_value=[]):
            send(refreshed,'记录鱼100克。',[call('record_meal',{'items':[{'food_query':'鱼','grams':100}]}),answer],'e2-refresh-empty')
        pid=next(iter(refreshed.load_session('user','test')['unfinished']))
        candidates=[{'food_id':'F1','name':'鱼（生）','carbs_per_100g':0,'protein_per_100g':20,'fat_per_100g':2,'kcal_per_100g':110},
                    {'food_id':'F2','name':'鱼（熟）','carbs_per_100g':0,'protein_per_100g':25,'fat_per_100g':3,'kcal_per_100g':127}]
        with patch.object(boohee_client,'search_food',return_value=candidates):
            send(refreshed,'再查一下鱼的候选。',[call('record_meal',{'items':[{'food_query':'鱼','pending_id':pid}]}),
                 call('record_meal',{'items':[{'food_query':'鱼','pending_id':pid,'choice':1}]},'c2'),answer],'e2-refresh-offer')
        assert not refreshed.intake
        current=refreshed.load_session('user','test')['unfinished'][pid]
        assert current['offered_message_id']=='e2-refresh-offer' and len(current['candidates'])==2
    def pending_state(messages):
        states=[json.loads(m.content.split('可信会话事实：',1)[1]) for m in messages if m.type=='system' and m.content.startswith('可信会话事实：')]
        assert len(states)==1
        return states[0]
    def verify_new_pending(messages):
        state=pending_state(messages)
        fact=json.loads(next(m.content for m in reversed(messages) if isinstance(m,ToolMessage)))
        assert set(state['unfinished'])=={item['pending_id'] for item in fact['data']['unfinished']}
        assert len(state['unfinished'])==2
        return answer(messages)
    def verify_completed(messages):
        assert not pending_state(messages)['unfinished']
        return answer(messages)
    s=setup();r=send(s,'记录夹具A200克、鱼100克、夹具B。',[call('record_meal',{'items':[{'food_query':'夹具A','grams':200},{'food_query':'鱼','grams':100},{'food_query':'夹具B'}]}),verify_new_pending],'e2a')
    assert len(s.intake)==1
    assert '鱼（生）' in r['reply'] and '夹具B' in r['reply'],r['reply']
    pending=s.load_session('user','test')['unfinished'];assert len(pending)==2
    if not REAL:
        from types import SimpleNamespace
        projected=state_view(SimpleNamespace(session=s.load_session('user','test'),settings={},day=s.today,user_id='user',store=s))
        assert set(projected['unfinished'])==set(pending)
        for pid,item in pending.items():
            view=projected['unfinished'][pid]
            assert view['pending_id']==pid and view['date']==item['date'] and view.get('grams')==item.get('grams')
            assert [c['choice'] for c in view.get('candidates',[])]==list(range(1,len(item.get('candidates',[]))+1))
            assert [c['name'] for c in view.get('candidates',[])]==[c['name'] for c in item.get('candidates',[])]
    saved=deepcopy(pending);send(s,'先不聊这个了。',[AIMessage(content='好，先放一放。')],'e2b');assert s.load_session('user','test')['unfinished']==saved
    fish=next(v for v in pending.values() if v['food_query']=='鱼');b=next(v for v in pending.values() if v['food_query']=='夹具B')
    def detail(url,headers):
        if 'code=fish' in url:return {'code':0,'data':{'code':'fish','name':'鱼（生）','carbohydrate':0,'protein':20,'fat':2,'calories':110,'units':[]}}
        return transport(url,headers)
    boohee_client.set_transport(detail);s.today='2026-11-01'
    r=send(s,'夹具B是2个，鱼选候选1。',[call('record_meal',{'items':[{'food_query':'夹具B','pending_id':b['pending_id'],'quantity':2,'unit':'个'},{'food_query':'鱼','pending_id':fish['pending_id'],'choice':1}]}),verify_completed],'e2c');kcal(r,380)
    assert len(s.intake)==3 and {i['date'] for i in s.intake}=={'2026-10-31'}
    assert not s.load_session('user','test')['unfinished']
    # A completed item retains its original meal when later reweighed/recounted.
    r=send(s,'刚才夹具B改为3个。',[call('revise_intake',{'food_query':'夹具B','quantity':3,'unit':'个'}),answer],'e2d');kcal(r,455)
    assert re.search(r'估算|约\s*\d',r['reply']),r['reply']
    meal=r['tool_results'][0]['data']['meal_totals'][0]
    assert meal['count']==3 and meal['date']=='2026-10-31' and meal['estimated']
    r=send(s,'夹具B实际称重可食150克。',[call('revise_intake',{'food_query':'夹具B','grams':150}),answer],'e2e');kcal(r,455)
    # Explicitly clearing an estimate is valid natural prose, too.
    assert '估算' not in re.sub(r'(?:不再是|不再按|已非|不是)[^，。；\n]{0,8}估算(?:值)?','',r['reply']),r['reply']
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
        if '全谷' in query and '主食' not in query:return {'status':'ok','entries':[{'entry_id':'fixture-k2','answer':'主食种类可以轮换。来源：轮换资料2024，第7段','source':'轮换资料2024，第7段'}],'evidence':'主食种类可以轮换。'}
        return {'status':'ok','entries':[{'entry_id':'fixture-k1','answer':'主食可以与蔬菜搭配，结合整天的饮食安排。来源：验收资料2022，第2段','source':'验收资料2022，第2段'}],'evidence':'主食可以与蔬菜搭配，结合整天的饮食安排。'}
    reads.build_knowledge_retriever=lambda kind:retrieve
    try:
        s=setup();r=send(s,'主食怎么搭配？给个简单解释和依据。',[call('query_knowledge',{'query':'主食搭配'}),answer],'e3a');assert '蔬菜' in r['reply']
        fact=r['tool_results'][0];saved=deepcopy(fact);view=model_view(fact)
        assert fact==saved and fact['data']['evidence']
        assert 'evidence' not in view['data'] and view['data']['entries'][0]['source_id']=='K101'
        assert view['data']['entries'][0]['answer']==fact['data']['entries'][0]['answer']
        assert view['data']['entries'][0]['source']==fact['data']['entries'][0]['source']
        assert json.dumps(view,ensure_ascii=False).count(fact['data']['evidence'])==1
        historical=history_messages([ToolMessage(content=json.dumps(fact,ensure_ascii=False),tool_call_id='knowledge',name='query_knowledge')])
        assert json.loads(historical[0].content)==view and model_view(view)==view
        assert not re.search(r'一半|半碗|半盘|一小碗|[一二三四五六七八九十百\d]+分之[一二三四五六七八九十百\d]+|\d+(?:\.\d+)?\s*(?:克|g|%|％)',r['reply']),r['reply']
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
        assert len(re.findall('[？?]',r['reply']))==1,r['reply']
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
    """分级历史统计一致、完整定位、万行投影有界、动态待办与来源不丢失。"""
    error={'tool':'query_today','status':'limit','data':{},'error':'工具调用次数已达上限'}
    assert model_view(error)==error
    s=setup()
    from harness.eval.personal_cases import A,B
    for n in range(3):
        s.write_intake({'message_id':f'e5-fixture-{n}','user_id':'user','chat_id':'test','text':'独立六行夹具'},[
            {'food_id':'A','matched_name':'夹具A','grams':200,'nutrition':{'carbs':20,'protein':4,'fat':2,'kcal':120},'snapshot':{'food':deepcopy(A),'estimated':False}},
            {'food_id':'B','matched_name':'夹具B','grams':100,'nutrition':{'carbs':0,'protein':12,'fat':10,'kcal':150},'snapshot':{'food':deepcopy(B),'estimated':True}}], 'lunch')
    # Earlier verbose replies must not broaden the current calorie-only request.
    from langchain_core.messages import HumanMessage,messages_to_dict
    s.save_session('user','test',{'messages':messages_to_dict([HumanMessage(content='今天摄入合计多少热量？'),AIMessage(content='今天已记录6项，合计810千卡，碳水60克、蛋白质48克、脂肪36克。需要每项明细也可以告诉我。')])})
    r=send(s,'今天合计多少热量？',[call('query_today',{}),lambda m:AIMessage(content='今天6项合计810千卡。')],'e5q');kcal(r,810)
    assert len(r['native_calls'])==1 and r['model_calls']<=2,r['native_calls']
    assert r['tool_results'][0]['data'].get('detail','calories')=='calories'
    assert not all(word in r['reply'] for word in ('碳水','蛋白','脂肪')),r['reply']
    f=r['tool_results'][0];view=model_view(f)
    assert view['data']=={'date':s.today,'count':6,'total':{'kcal':810},'detail':'calories'}
    assert len(f['data']['items'])==6
    assert 'targets' not in f['data'] and 'remaining' not in f['data']
    assert 'snapshot' not in f['data']['items'][0]
    assert render('已修改，常用餐20261101，今天6项，指南2022。',[])[0].startswith('已修改')
    r=send(s,'把今天每项的克重和碳水、蛋白质、脂肪都列给我。',[call('query_today',{'detail':'items'}),lambda m:AIMessage(content='夹具A200克：碳水20克、蛋白质4克、脂肪2克；夹具B100克：碳水0克、蛋白质12克、脂肪10克。今天三次记录的同款午餐各有上述两项。')],'e5detail')
    assert all(k in r['reply'] for k in ('碳水','蛋白','脂肪'))
    if not REAL:
        _history_projection_checks()


def _history_projection_checks():
    from types import SimpleNamespace
    from unittest.mock import patch
    from agent import FakeStore
    from agent.tools.registry import History, TOOL_REGISTRY, query_history
    from agent.tools.meals import select_rows
    from harness.eval.personal_cases import A
    assert History().detail=='summary' and len(TOOL_REGISTRY)==8
    sizes=[]
    for n in (1000,10000):
        seeds=[{'date':'2026-10-01' if i%2==0 else '2026-10-02','food_name':'夹具A' if i%2==0 else '夹具B',
                'meal_type':'lunch' if i%2==0 else 'dinner','user_id':'user','grams':100,
                'carbs':10,'protein':2,'fat':1,'kcal':60} for i in range(n)]
        seeds.append({'date':'2026-10-31','food_name':'固定当日餐','meal_type':'snack','user_id':'user','grams':100,'kcal':77})
        s=FakeStore(foods=deepcopy([A]),today='2026-10-31',intake_seed=seeds)
        current=s.intake[-1]['record_id']
        candidates=[{**deepcopy(A),'food_id':f'choice-{i}','name':f'原日候选{i}'} for i in range(1,21)]
        session={'refs':[row['record_id'] for row in s.intake], 'last_meal':[current], 'unfinished':{
            'pending-old':{'pending_id':'pending-old','date':'2026-10-01','meal_type':'lunch','food_query':'原日食物','grams':125,
                           'reason':'请选择食物候选','offered_message_id':'original-offer','candidates':candidates},
            'pending-quantity':{'pending_id':'pending-quantity','date':'2026-10-02','food_query':'另一个未完食物','quantity':2,'unit':'个','reason':'缺份量资料'}}}
        settings={'preferences':{'cooking':'少油'},'targets':{'kcal':1900},'favorites':{'常用午餐':[{'large':'完整快照'}]},
                  'daily_targets':{'2026-10-01':{'kcal':1800},'2026-10-31':{'kcal':2000},'2020-01-01':{'kcal':999}},
                  'portions':{'large':{'eat_weight':100}},'reminders':{'on':{'enabled':True,'time':'12:00','meal_type':'lunch'},'off':{'enabled':False}},'feishu_chat_id':'private-routing'}
        rt=SimpleNamespace(session=session,settings=settings,day=s.today,user_id='user',store=s)
        rt.add_refs=lambda rows:session.update(refs=list(dict.fromkeys(session['refs']+[row['record_id'] for row in rows])))
        original_session,original_settings=deepcopy(session),deepcopy(settings)
        before=state_view(rt)
        assert session==original_session and settings==original_settings
        assert set(before['unfinished'])==set(session['unfinished'])
        assert len(before['unfinished']['pending-old']['candidates'])==20
        assert before['unfinished']['pending-old']['candidates'][-1]['choice']==20
        assert before['unfinished']['pending-old']['grams']==125 and before['unfinished']['pending-quantity']['quantity']==2
        pending_fact={'tool':'record_meal','status':'pending','data':{'unfinished':list(session['unfinished'].values())}}
        projected_pending=model_view(pending_fact)['data']['unfinished']
        assert len(projected_pending[0]['candidates'])==20 and projected_pending[0]['candidates'][-1]['choice']==20
        assert not any(key.endswith('_per_100g') for key in projected_pending[0]['candidates'][0])
        assert len(before['refs'])<=13 and len(before['refs'])<len(session['refs'])
        assert all(set(ref)=={'record_id','food_name','date','meal_type'} for ref in before['refs'])
        assert before['last_meal']==[current] and before['user_settings']['favorites']==['常用午餐']
        assert set(before['user_settings']['daily_targets'])=={'2026-10-01','2026-10-31'}
        assert set(before['user_settings']['reminders'])=={'on'} and 'portions' not in before['user_settings']
        params={'start_date':'2026-10-01','end_date':'2026-10-04','meal_type':'lunch','food_query':'夹具A'}
        facts=[{'tool':'query_history',**query_history(rt,{**params,'detail':detail})} for detail in ('summary','daily','items')]
        expected={'carbs':n*5,'protein':n,'fat':n/2,'kcal':n*30}
        for fact in facts:
            assert fact['data']['count']==n//2 and fact['data']['total']==expected
            assert len(fact['data']['items'])==n//2
        summary,daily,items=map(model_view,facts)
        assert 'items' not in summary['data'] and 'items' not in daily['data']
        assert [d['date'] for d in daily['data']['daily']]==['2026-10-01','2026-10-02','2026-10-03']
        assert [d['status'] for d in daily['data']['daily']]==['recorded','unrecorded','unrecorded']
        assert daily['data']['daily'][1]['count']==0 and daily['data']['daily'][1]['total']['kcal']==0
        assert len(items['data']['items'])==n//2
        last=items['data']['items'][-1]
        assert select_rows(rt,[last['record_id']])[0]['record_id']==last['record_id']
        # The direct-ID store path must not load the historical ledger again.
        indexed={row['record_id']:row for row in s.intake}
        with patch.object(s,'intake_by_ids',lambda user,ids:[deepcopy(indexed[key]) for key in ids],create=True), patch.object(s,'intake_items',side_effect=AssertionError('state_view must use fixed IDs')):
            assert state_view(rt)==before
        # A rebuilt view immediately reflects completed/cancelled pending state
        # and the new active proposal, without copying complete target snapshots.
        session['unfinished'].pop('pending-old')
        session['proposal']={'proposal_id':'fixed-proposal','tool':'revise_intake','args':{'record_id':current,'grams':50},
                             'target':{'date':s.today,'food_name':'固定当日餐','grams':100},'target_version':'v1','response_id':'reply-id','presented':True}
        after=state_view(rt)
        assert set(after['unfinished'])=={'pending-quantity'} and after['proposal']==session['proposal']
        session.pop('proposal')
        assert state_view(rt)['proposal'] is None
        sizes.append((len(json.dumps(summary,ensure_ascii=False)),len(json.dumps(before,ensure_ascii=False))))
    assert sizes[1][0]-sizes[0][0]<32 and sizes[1][1]-sizes[0][1]<64,sizes

def E06():
    """写后中断、固定两动作跨日恢复、异常事实口径及CLI/DSH提案展示。"""
    from agent.replies import progress_reply
    progress=progress_reply([{'tool':'revise_intake','status':'ok','data':{'meal_totals':[{'meal_id':'same-meal','total':{'kcal':270},'estimated':True}]}},
                             {'tool':'query_today','status':'ok','data':{'total':{'kcal':633.8}}},
                             {'tool':'revise_intake','status':'ok','data':{'meal_totals':[{'meal_id':'same-meal','total':{'kcal':210},'estimated':True}]}}])
    assert '210' in progress and '270' not in progress and '633.8' not in progress and '估算' in progress,progress
    assert '今天合计525.6' in progress_reply([{'tool':'query_today','status':'ok','data':{'total':{'kcal':525.6}}}])
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
        assert any('夹具A' in c and re.search('已(?:经)?记|记好|记入',c) and not re.search('未记|没记|没有记',c) for c in clauses),text
        assert not any('夹具B' in c and re.search('已(?:经)?记|记好|记入',c) for c in clauses),text
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
