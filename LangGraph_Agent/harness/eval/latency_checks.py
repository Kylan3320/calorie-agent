"""Independent model transport, retrieval and execution-budget checks."""
import collections
from copy import deepcopy
import json
import re
import urllib.parse
import time
from unittest.mock import patch

def rag_loop_check():
    import asyncio,sys,types,httpx
    from agent.storage import knowledge_retriever as module
    base=types.ModuleType('lightrag');base.__path__=[]
    utils=types.ModuleType('lightrag.utils')
    base.LightRAG=lambda **kw:types.SimpleNamespace(**kw)
    utils.EmbeddingFunc=lambda **kw:types.SimpleNamespace(**kw)
    calls=[]
    def transport(request):
        body=json.loads(request.content);calls.append(body)
        return httpx.Response(200,json={'choices':[{'message':{'content':'fixture-keywords'}}]})
    with patch.dict(sys.modules,{'lightrag':base,'lightrag.utils':utils}),patch.object(module,'_writable_index_dir',lambda:'fixture'),httpx.Client(transport=httpx.MockTransport(transport)) as client,patch.object(module,'_HTTP_CLIENT',client):
        rag=module._build_lightrag()
        assert asyncio.run(rag.llm_model_func('first'))=='fixture-keywords'
        assert asyncio.run(rag.llm_model_func('second'))=='fixture-keywords'
    assert len(calls)==2 and all('thinking' not in c for c in calls)

def evaluate_filter(formula,fields):
    """Small independent evaluator for the supported Bitable formula subset."""
    from datetime import datetime,timezone,timedelta
    def args(text):
        result=[];start=0;depth=0;quoted=False;escaped=False
        for i,c in enumerate(text):
            if escaped:escaped=False;continue
            if c=='\\' and quoted:escaped=True;continue
            if c=='"':quoted=not quoted
            if quoted:continue
            if c=='(':depth+=1
            elif c==')':depth-=1
            elif c==',' and depth==0:result.append(text[start:i]);start=i+1
        return result+[text[start:]]
    for fn,combine in (('AND',all),('OR',any)):
        if formula.startswith(fn+'('):return combine(evaluate_filter(a,fields) for a in args(formula[len(fn)+1:-1]))
    if formula=='TRUE()':return True
    match=re.fullmatch(r'CurrentValue\.\[([^]]+)\]\s*(>=|<|=)\s*(.*)',formula)
    assert match,formula
    name,op,literal=match.groups();actual=fields.get(name,'')
    if literal.startswith('TODATE('):
        day=json.loads(literal[7:-1]);expected=datetime.fromisoformat(day).replace(tzinfo=timezone(timedelta(hours=8))).timestamp()*1000
    else:expected=json.loads(literal)
    if actual in (None,'') and op!='=':return False
    return actual==expected if op=='=' else (actual>=expected if op=='>=' else actual<expected)

def finish_and_budget_check():
    import time
    from agent import run
    from agent.storage.timeutil import PROCESSING_DEADLINE
    from harness.eval.personal_cases import setup,CONFIG,NativeFake,call
    from harness.eval.conversation_cases import answer
    store=setup();message={'message_id':'finish-boundary','user_id':'user','chat_id':'boundary','text':'记录夹具A200克'}
    save=store.save_session
    class Death(BaseException): pass
    def after_session(*args,**kwargs):
        save(*args,**kwargs)
        raise Death()
    store.save_session=after_session
    try:
        run(message,store=store,config=CONFIG,model=NativeFake(responses=[call('record_meal',{'items':[{'food_query':'夹具A','grams':200}]}),answer]))
    except Death:pass
    else:raise AssertionError('session failure was not injected')
    store.save_session=save
    store.today='2026-11-01'
    replay=NativeFake(responses=[])
    saved_reply=store.load_execution(message)["payload"]["result"]["reply"]
    result=run(message,store=store,config=CONFIG,model=replay)
    assert not replay.requests and result["reply"]==saved_reply
    assert len(store.intake)==1 and store.intake[0]['nutrition']['kcal']==120 and store.intake[0]['date']=='2026-10-31'
    assert result['status']=='ok' and '120' in result['reply']
    blocked=NativeFake(responses=[call('record_meal',{'items':[{'food_query':'夹具A','grams':200}]})])
    token=PROCESSING_DEADLINE.set(time.monotonic()+4)
    try:
        result=run({**message,'message_id':'budget-boundary'},store=store,config=CONFIG,model=blocked)
    finally:PROCESSING_DEADLINE.reset(token)
    assert result['status']=='error' and not blocked.requests and len(store.intake)==1
    # A source read crosses the work cutoff after tool entry, before actual write.
    from agent.tools.registry import CURRENT_RUNTIME
    source_read=store.load_foods
    def late_foods():
        result=source_read()
        CURRENT_RUNTIME.get().deadline=time.monotonic()-1
        return result
    with patch.object(store,'load_foods',late_foods):
        result=run({**message,'message_id':'crossed-before-write'},store=store,config=CONFIG,
                   model=NativeFake(responses=[call('record_meal',{'items':[{'food_query':'夹具A','grams':200}]})]))
    assert result['status']=='error' and len(store.intake)==1
    # Audit persistence crossing the cutoff must not start the provider request.
    save_execution=store.save_execution
    def late_audit(task,payload,**kw):
        save_execution(task,payload,**kw)
        rt=CURRENT_RUNTIME.get(None)
        if rt is not None and payload.get('model_calls')==1:rt.deadline=time.monotonic()-1
    provider=NativeFake(responses=[])
    with patch.object(store,'save_execution',late_audit):
        result=run({**message,'message_id':'crossed-before-provider'},store=store,config=CONFIG,model=provider)
    assert result['status']=='error' and not provider.requests
    # Final proposal reply is durable before the first session write.
    second=setup()
    proposed={**message,'message_id':'proposal-before-session','chat_id':'proposal-boundary','text':'建议长期目标1900，等我确认'}
    def before_session(*args,**kw):raise Death()
    with patch.object(second,'save_session',before_session):
        try:
            run(proposed,store=second,config=CONFIG,model=NativeFake(responses=[call('update_user_settings',{'mode':'propose','kind':'targets','values':{'kcal':1900}}),answer]))
        except Death:pass
        else:raise AssertionError('pre-session death missing')
    saved=second.load_execution(proposed)['payload']['result']['reply']
    replay=NativeFake(responses=[])
    result=run(proposed,store=second,config=CONFIG,model=replay)
    assert not replay.requests and result['reply']==saved and '1900' in saved
    session=second.load_session('user','proposal-boundary')
    assert session['messages'][-1]['data']['content']==saved and not session['proposal']['presented']

def model_protocol_check():
    import httpx
    import agent.model as model_module
    from langchain_core.messages import HumanMessage, ToolMessage
    requests=[]
    def transport(request):
        requests.append(json.loads(request.content))
        message=({'content':'','reasoning_content':'fixture-reasoning','tool_calls':[{'id':'native-1','type':'function','function':{'name':'query_today','arguments':'{}'}}]}
                 if len(requests)==1 else {'content':'今天0千卡。'})
        return httpx.Response(200,json={'model':'deepseek-v4-flash','choices':[{'message':message}],
            'usage':{'prompt_tokens':123,'completion_tokens':17,'total_tokens':140}})
    with httpx.Client(transport=httpx.MockTransport(transport)) as client,patch.object(model_module,'_CLIENT',client),patch.dict('os.environ',{'DEEPSEEK_API_KEY':'fixture'}):
        model=model_module.DeepSeekModel()
        first=model.invoke([HumanMessage(content='查今日')],tools=[{'type':'function','function':{'name':'query_today','parameters':{'type':'object','properties':{}}}}])
        model.invoke([HumanMessage(content='查今日'),first,ToolMessage(content='{"kcal":0}',tool_call_id='native-1')])
        assert first.usage_metadata=={'input_tokens':123,'output_tokens':17,'total_tokens':140}
        assert first.response_metadata['model_provider']==model._get_ls_params()['ls_provider']
        assert requests[1]['messages'][1]['reasoning_content']=='fixture-reasoning'
        assert requests[0]['parallel_tool_calls'] is False
        assert all(r['model']=='deepseek-v4-flash' and 'thinking' not in r for r in requests)
        # Verify the real create_agent path, rather than only direct adapter calls.
        from dataclasses import replace
        from agent import run
        from agent.graph import SYSTEM
        from harness.eval.personal_cases import setup,CONFIG
        requests.clear()
        run({'message_id':'system-wire','user_id':'user','chat_id':'system-wire','text':'查今日热量'},store=setup(),config=replace(CONFIG,model='deepseek-v4-flash'),model=model)
        assert len(requests)==2
        assert requests[0]['messages'][0]['role']=='system'
        assert requests[0]['messages'][0]['content'].startswith(SYSTEM)
        assert '可信会话事实' in requests[0]['messages'][0]['content']

CHECKS = {"LT01": rag_loop_check, "LT02": model_protocol_check, "LT03": finish_and_budget_check}
