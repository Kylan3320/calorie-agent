"""Stage C evidence uses production Store/Agent paths with controlled transports."""
from copy import deepcopy
from dataclasses import replace
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import hashlib
import json
import threading
import time
from unittest.mock import patch
from agent.storage.cos_journal import Journal, encode, CommitUnavailable
from agent.storage.cos_store import CosStore
from agent.storage.execution_pipeline import Pipeline
from agent.storage import memory, tasks
from agent.storage.generations import GenerationControl, Maintenance, MaintenancePaused, DOMAINS
from agent.storage.retention import preflight, AUDIT_SECONDS
from agent.tools.general import source
from harness.eval.production_store_cases import Objects as BaseObjects, open_store
from harness.eval.personal_cases import CONFIG, A, MEAL, NativeFake, call
from harness.eval.conversation_cases import answer

EVIDENCE = {}
LEDGER_FIXTURE = Path(__file__).resolve().parents[2] / 'reports/r11-cloud-ledger-20261104.json'

class Objects(BaseObjects):
    def __init__(self):
        super().__init__(); self.ops=[]
    def get(self,key):
        value = super().get(key); self.ops.append(('get',key,len(value or b''))); return value
    def create(self,key,body):
        result = super().create(key,body); self.ops.append(('create',key,len(body))); return result
    def list(self,prefix,marker='',limit=None):
        rows = super().list(prefix,marker,limit); self.ops.append(('list',prefix,len(rows))); return rows
    def delete(self,key): self.data.pop(key,None); self.ops.append(('delete',key,0))
    def stats(self):
        return {kind:{'requests':sum(k==kind for k,_,_ in self.ops),
                     'bytes_or_keys':sum(n for k,_,n in self.ops if k==kind)} for kind in ('get','create','list','delete')}


def growth():
    """S1: page-crossing histories, bounded incremental writes, checkpoint and cold point recovery."""
    evidence=[]
    for count in (72,360):
        objects=Objects(); store=CosStore(objects,'isolated/stage-c/growth')
        def seed():
            for n in range(count):
                store.save_document('memory_object','user',f'old:{n}',{'ref':f'old:{n}','text':'清洁历史 '+('来源正文 '*60)})
                store._save({'archive:food:old'+str(n):{'kind':'food','id':'old'+str(n),'revision':1,'value':{'name':'历史来源'}}})
                store._save({'day:user:'+f'{2000+n//300:04d}-{n//30%10+1:02d}-{n%28+1:02d}':[]})
            catalog={'items':{f'old:{n}':{'kind':'turn','ref':f'old:{n}','text':'元数据'} for n in range(count)}}
            store.save_document('memory_index','user','',catalog)
            store.save_document('worker_attempt','user','one',{'status':'running'})
            return {'status':'ok'}
        store.commit_action('seed',seed)
        objects.ops=[]
        catalog=store.load_document('memory_index','user'); catalog['items']['old:0']['text']='更新一条元数据'
        store.save_document('memory_index','user','',catalog)
        store.save_document('worker_attempt','user','one',{'status':'completed'})
        memory_stats=objects.stats()
        assert not any('/documents/' in key and size>16000 for kind,key,size in objects.ops if kind=='create')
        from agent.storage.cos_archive import archive_pending
        progress=Journal(objects,store.prefix+'sync/')
        progress.commit({**{'item:archive:food:old'+str(n):{'ack_revision':1,'status':'confirmed'} for n in range(count)},
            'archive_scan':{'generation':store.prefix,'revision':store.journal.revision}})
        objects.ops=[]
        store.write_intake({'message_id':'new','user_id':'user','text':'独立新增'},[
            {'food_id':'old','matched_name':'夹具','grams':100,'date':'2026-11-13','nutrition':{'kcal':60,'carbs':10,'protein':2,'fat':1}}])
        assert store.intake_items('user','2026-11-13','2026-11-14')[0]['nutrition']['kcal']==60
        ledger_stats=objects.stats()
        class Gateway:
            def prepare(self,change):return change
            def apply_and_readback(self,request):return {'record_id':'controlled-'+request['id']}
        objects.ops=[]
        archived=archive_pending(store,gateway=Gateway(),progress=progress)
        assert len(archived['confirmed'])==1 and not archived['failed']
        archive_stats=objects.stats()
        objects.ops=[]
        fresh=CosStore(objects,'isolated/stage-c/growth',readonly=True)
        assert fresh.load_document('worker_attempt','user','one')['status']=='completed'
        cold=objects.stats()
        assert cold['get']['requests']<60, cold
        commit=json.loads(objects.data[store.journal.key(store.journal.revision)])
        assert commit['schema']=='calorie.cos.v2' and 'root' in commit and 'index' not in commit
        evidence.append({'history':count,'memory_worker':memory_stats,'ledger':ledger_stats,'incremental_archive':archive_stats,'cold_checkpoint_point_read':cold,
                         'checkpoint_bytes':len(encode(commit))})
    assert evidence[1]['memory_worker']['create']['bytes_or_keys'] < evidence[0]['memory_worker']['create']['bytes_or_keys']*2.5
    assert evidence[1]['incremental_archive']['get']['requests'] < evidence[0]['incremental_archive']['get']['requests']*2.5+10
    EVIDENCE['S1']=evidence


def old_state(objects,prefix='isolated/stage-c/old/'):
    """Build actual v1 envelopes with old pending, index, ID and receipt shapes."""
    from agent.storage.bitable_adapter import stable_uuid
    now=time.time()
    receipt={'receipt_id':'r-old','sequence':1,'source':'cli','kind':'message','received_at':now-40*86400,
             'message':{'message_id':'old','user_id':'user','chat_id':'chat','text':'旧输入'},'intake_date':'2026-10-01'}
    row={'record_id':'rec-existing-feishu-id','user_id':'user','date':'2026-10-01','grams':100,
         'nutrition':{'kcal':60,'carbs':10,'protein':2,'fat':1},'snapshot':{'origin':'old'},'status':'deleted',
         'created_at':1,'message_id':'old:agent:1','food_name':'夹具','meal_type':'breakfast','agent_revision':2}
    exec_id=stable_uuid('calorie-agent:pending:old:message_execution:')
    execution={'pending_id':exec_id,'record_id':exec_id,'intent':'message_execution','source_message_id':'old','user_id':'user','chat_id':'chat',
        'status':'completed','completed_at':now-31*86400,'payload':{'result':{'status':'ok','reply':'原最终回执','business_status':'committed'},
        'model_requests':[{'body':'过程 '*3000}], 'calls':[{'action_id':'old:agent:1','result':{'status':'ok'}}]}}
    states={'ingress':{'receipt:r-old':receipt,'next_sequence':1,'session:legacy':[{'receipt_id':'r-old','sequence':1}]},
        'message':{'item:'+row['record_id']:row,'day:user:2026-10-01':[row['record_id']], 'pending:'+exec_id:execution,
            'action:old:agent:1':{'entry':{'result':{'status':'ok'}},'session':{'large_audit':'重复 '*3000}},
            'intake_action:old:agent:1':[{'record_id':row['record_id']}]},'sync':{'item:archive:intake:old':{'status':'pending','inflight':{'fixed':True},'ack_revision':0}}}
    retained=json.loads(LEDGER_FIXTURE.read_text(encoding='utf-8'))
    for actual in retained['baseline_rows']:
        states['message']['item:'+actual['record_id']]=deepcopy(actual)
        states['message'].setdefault('day:'+actual['user_id']+':'+actual['date'],[]).append(actual['record_id'])
    receipts={
        'r-old':{'status':'completed','receipt_id':'r-old','completed_at':now-31*86400,'delivery':{'presented':True,'uuid':'fixed-uuid'},'result':{'status':'ok','reply':'原最终回执','tool_events':['process '*1000]}},
        'r-boundary':{'status':'completed','completed_at':now-AUDIT_SECONDS+1,'result':{'status':'ok','reply':'保留一天边界'}},
        'r-await':{'status':'awaiting_presentation','completed_at':now-45*86400,'result':{'status':'ok','reply':'等展示'},'delivery':{'presented':False}},
        'r-attention':{'status':'completed','work_status':'needs_attention','completed_at':now-45*86400,'result':{'status':'ok'}},
    }
    for rid,payload in receipts.items():
        key='document:'+stable_uuid('receipt_state:system:'+rid)
        states['message'][key]={'intent':'receipt_state','user_id':'system','chat_id':rid,'payload':payload}
    for role,values in states.items():
        index={}
        for key,value in values.items():
            raw=encode(value); ref=hashlib.sha256(raw).hexdigest(); objects.create(prefix+role+'/documents/'+ref+'.json',raw); index[key]=ref
        objects.create(prefix+role+'/commits/000000000000.json',encode({'schema':'calorie.cos.v1','revision':0,'previous':-1,'changes':index,'index':index}))
    return prefix,now


def retention_cutover():
    """S2→S3: old v1, 30-day boundary, pending delivery/inflight, cutover/delete/restart and new archive."""
    objects=Objects(); prefix,now=old_state(objects)
    late = Pipeline(objects,prefix).accept({'message_id':'late-delivery','user_id':'user','chat_id':'late','text':'旧请求'},
                                           source='cli',received_at=now-45*86400)
    late_store=CosStore(objects,prefix)
    execution=late_store.begin_execution(late['message'],{'day':'2026-10-01'})
    late_store.save_execution(execution,{'result':{'status':'ok','reply':'送达较迟'},'model_requests':[{'body':'过程 '*1200}]},completed=True)
    ident='pending:'+execution['pending_id']
    execution=late_store.journal.get(ident)
    late_store.journal.commit({ident:{**execution,'completed_at':now-40*86400}})
    late_store.save_document('receipt_state','system',late['receipt_id'],{'status':'completed','completed_at':now-40*86400,
        'delivery':{'presented':True,'presented_at':now-86400},'result':{'status':'ok','reply':'送达较迟','tool_events':['保留']}})
    journals={role:Journal(objects,prefix+role+'/',readonly=True) for role in DOMAINS}
    before=deepcopy(objects.data)
    selected,report=preflight(journals,now)
    assert objects.data==before and report['compact_count']==2, report
    maint=Maintenance(objects,prefix)
    state=maint.prepare('g1',now=now,compact=True)
    assert all(k in objects.data for k in before), 'S1/S2 must not delete'
    maint.request_pause('g1')
    ctl=maint.control;epoch=state['epoch']
    ctl.create(ctl.key(epoch,'started'),{'started_at':now,'decision_deadline':now+40,'resume_deadline':now+60})
    for role,j in journals.items():ctl.acknowledge(role,epoch,j.revision)
    decision=maint.finish('g1',now=now+1)
    assert decision['choice']=='enable'
    current=CosStore(objects,prefix,readonly=True)
    assert current.load_document('receipt_state','system','r-old')['result']['audit_status']=='unavailable'
    assert current.load_document('receipt_state','system','r-await')['delivery']['presented'] is False
    assert current.load_document('receipt_state','system',late['receipt_id'])['result']['tool_events']==['保留']
    assert current.load_execution(late['message'])['payload']['model_requests'][0]['body'].startswith('过程')
    assert current.intake_by_ids('user',['rec-existing-feishu-id'],True)[0]['status']=='deleted'
    denied={role:{'status':403,'source':prefix,'transport':'controlled'} for role in DOMAINS}
    try: maint.collect('g1',restart_verified=True,denied_roles={})
    except PermissionError:pass
    else:raise AssertionError('delete without permission proof')
    plan=maint.collection_plan('g1')
    deleted=maint.collect('g1',restart_verified=True,denied_roles=denied,limit=2)
    assert deleted['remaining']>0
    while deleted['remaining']:
        deleted=Maintenance(objects,prefix).collect('g1',restart_verified=True,denied_roles=denied,limit=100)
    assert not any(k.startswith(prefix+role+'/') for k in objects.data for role in DOMAINS)
    store=CosStore(objects,prefix)
    assert store.committed_action('old:agent:1')['committed']
    assert store.load_document('receipt_state','system','r-old')['result']['reply']=='原最终回执'
    store.write_intake({'message_id':'new','user_id':'user','text':'恢复后新增'},[
        {'food_id':'test','matched_name':'夹具','grams':50,'date':'2026-11-13','nutrition':{'kcal':30,'carbs':5,'protein':1,'fat':.5}}])
    reboot=CosStore(objects,prefix,readonly=True)
    assert len(reboot.intake_items('user','2026-11-13','2026-11-14'))==1
    assert reboot.intake_items('user','2026-10-01','2026-10-02')==[]
    for actual in json.loads(LEDGER_FIXTURE.read_text(encoding='utf-8'))['baseline_rows']:
        assert reboot.intake_by_ids(actual['user_id'],[actual['record_id']],True)[0]['nutrition']==actual['nutrition']
    EVIDENCE['S2_S3']={'legacy_source':'reports/r11-cloud-ledger-20261104.json baseline_rows; retained real IDs/nutrition in reconstructed v1 envelopes','preflight':report,'source':prefix,'target':state['target'],'deleted_objects':len(plan['keys']),
        'deleted_bytes':plan['bytes'],'after_restart_new_intake_kcal':30,'permission_evidence':'controlled_only'}


def watchdog():
    """S3: no external wakeup cancellation, unknown decision fail-closed and one delayed decision."""
    objects=Objects();ctl=GenerationControl(objects,'isolated/stage-c/watchdog')
    now=[1000.]
    pause={'epoch':0,'source':ctl.root,'target':ctl.root+'generations/g1/'}
    ctl.create(ctl.key(0,'pause'),pause)
    def wait(seconds):now[0]+=seconds
    decision=ctl.message_wait(0,0,clock=lambda:now[0],wait=wait)
    assert decision['choice']=='cancel' and 1040<=now[0]<=1060
    assert ctl.location()==(1,ctl.root)
    # A delayed enable and cancel race cannot both create the decision key.
    frozen=ctl.create(ctl.key(0,'decision'),{**decision,'choice':'enable'})
    assert frozen==decision
    objects.hidden.add(ctl.key(0,'decision'))
    try:GenerationControl(objects,ctl.root).location()
    except MaintenancePaused:pass
    else:raise AssertionError('unreadable decision resumed writes')
    EVIDENCE['watchdog']={'pause_seconds':now[0]-1000,'decision':'cancel','no_external_wakeup':True,'unknown_decision':'writes_closed'}


def delegation():
    """C1: two native Workers overlap, foreground query, scoped control, duplicate and cancelled late candidate."""
    from agent.storage.task_runtime import dispatch_pending, worker_handler, writer
    from agent.tools.general import validate_claims
    objects=Objects();store=CosStore(objects,'isolated/stage-c/tasks')
    config=replace(CONFIG,preset='live')
    text='请持续查证活动和交通，交付清单；到2027年1月1日结束。'
    s1=source('活动于2026-12-01 09:00开始；地点上海展馆。',url='https://www.shanghai.gov.cn/',title='受控活动资料')
    s2=source('公开交通资料：地铁到站后步行15分钟。',url='https://www.shmetro.com/',title='受控交通资料')
    rt=SimpleNamespace(store=store,user_id='user',action_id='request:agent:1',day='2026-11-13',goal_run=None,
        message={'text':text,'message_id':'request','chat_id':'chat'},journal={'model_calls':1,'calls':[{}]},session={'general_sources':{s1['ref']:s1,s2['ref']:s2}})
    started=store.commit_action(rt.action_id,lambda:tasks.manage(rt,{'operation':'start','user_quote':text,
        'spec':{'objective':'查证活动和交通','done_when':'带出处清单与未决','ends':'2027-01-01T00:00:00+08:00'}}))
    task=started['data']['task'];ident=task['id'];rt.action_id='request:agent:2';rt.journal['calls'].append({})
    delegated=store.commit_action(rt.action_id,lambda:tasks.delegate(rt,{'task_id':ident,'revision':1,'jobs':[
        {'objective':'核对活动','done_when':'活动时间与地点','source_refs':[s1['ref']]},
        {'objective':'核对交通','done_when':'交通条件','source_refs':[s2['ref']]}]}))
    ids=delegated['data']['workers'];lock=threading.RLock();barrier=threading.Barrier(3);release=threading.Event();intervals={}
    dispatches=[]
    events={}
    def dispatch(event, *, target):
        assert target == 'compute'
        dispatches.append(event['worker_id'])
        events[event['worker_id']]=event
    def sign(attempt, expires):
        return {'input_url':'fixture-input','candidate_url':'fixture-candidate'}
    assert not dispatch_pending(store,dispatch=dispatch,sign=sign,user_id='another-user')
    assert dispatch_pending(store,dispatch=dispatch,sign=sign,user_id='user')==ids
    assert not dispatch_pending(store,dispatch=dispatch,sign=sign)
    assert dispatches==ids
    class Blocking(NativeFake):
        def _generate(self,*a,**kw):
            ident=threading.current_thread().name
            if ident not in intervals:
                intervals[ident]=[time.monotonic(),None];barrier.wait(timeout=15);release.wait(timeout=15)
            try:return super()._generate(*a,**kw)
            finally:intervals[ident][1]=time.monotonic()
    candidates={}
    def one(index):
        wid=ids[index];snapshot=store.load_document('task_input','user',wid);attempt=store.load_document('worker_attempt','user',wid)
        ev=events[wid]
        src=(s1,s2)[index]
        params={'outcome':'complete','items':[{'text':src['text'],'evidence':[{'ref':src['ref'],'quote':src['text']}]}]}
        def claim(q):
            with lock:return writer(store,{'operation':'claim',**q},config)
        def control(q):
            with lock:return tasks.control(store,q)
        def put(raw):
            candidates[wid]=json.loads(raw);objects.create(attempt['generation']+attempt['candidate_key'],raw)
        return worker_handler(ev,get=lambda:encode(snapshot),put=put,claim=claim,control=control,report=lambda q:None,
            model=Blocking(responses=[call('query_sources',{'operation':'read','ref':src['ref']}),call('submit_task_result',params,cid='c2')]))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(one,i) for i in range(2)]
        barrier.wait(timeout=15)
        queried=tasks.query(rt,{'operation':'status','task_id':ident})
        assert all(w['status']=='running' for w in queried['data']['task']['cycle']['workers'])
        release.set()
        results=[f.result(timeout=30) for f in futures]
    assert all(r['status']=='candidate_saved' for r in results),results
    assert max(v[0] for v in intervals.values())<min(v[1] for v in intervals.values())
    rt.action_id='control:1'
    store.commit_action(rt.action_id,lambda:tasks.manage(rt,{'operation':'stop_worker','user_quote':text,'task_id':ident,'revision':1,'worker_id':ids[1]}))
    first=tasks.publish(store,{'user_id':'user','task_id':ident,'worker_id':ids[0],'candidate':candidates[ids[0]]})
    duplicate=tasks.publish(store,{'user_id':'user','task_id':ident,'worker_id':ids[0],'candidate':candidates[ids[0]]})
    late=tasks.publish(store,{'user_id':'user','task_id':ident,'worker_id':ids[1],'candidate':candidates[ids[1]]})
    assert first['status']=='completed' and duplicate['status']=='duplicate' and late['status']=='rejected'
    # Router sees the valid part and genuinely missing transport, never erases it.
    parent={'outcome':'partial','items':candidates[ids[0]]['items'],'unresolved':['交通分项已停止，待用户决定是否继续']}
    router_id=dispatch_pending(store,dispatch=dispatch,sign=sign)[0]
    router=store.load_document('worker_attempt','user',router_id)
    snapshot=store.load_document('task_input','user',router_id)
    result=worker_handler(events[router_id],get=lambda:encode(snapshot),
        put=lambda raw:objects.create(router['generation']+router['candidate_key'],raw),
        claim=lambda q:writer(store,{'operation':'claim',**q},config),control=lambda q:tasks.control(store,q),
        report=lambda q:writer(store,{'operation':'report',**q},config),
        model=NativeFake(responses=[call('submit_task_result',parent)]))
    assert result['status']=='candidate_saved', result
    final=tasks.load(store,'user',ident)
    artifact=store.load_document('task_artifact','user',final['latest'])
    assert artifact['outcome']=='partial' and len(artifact['items'])==1 and final['status']=='active', artifact
    assert not tasks.control(store,{'user_id':'user','task_id':ident,'worker_id':ids[0],'instance_id':'other','control_token':'bad'})['allowed']
    EVIDENCE['C1']={'task_id':ident,'worker_ids':ids,'overlap_seconds':min(v[1] for v in intervals.values())-max(v[0] for v in intervals.values()),
        'foreground_query':'both running','duplicate':duplicate['status'],'late':late['status'],'outcome':artifact['outcome'],
        'transport':'controlled','model':'native injected model','sources':'controlled public-document text'}

CHECKS={'CS01':growth,'CS02':retention_cutover,'CS03':watchdog,'CG01':delegation}


def general_sources_prompt():
    """C0/C1: direct known URL, real native decisions, bounded arithmetic and no forced delegation."""
    from agent import run
    from agent.storage.fake_store import FakeStore
    from agent.storage import deepseek_web
    from langchain_core.messages import AIMessage, ToolMessage
    calls=[]
    def fetch(url,*,deadline,reserve):
        reserve();calls.append(url)
        return {'title':'受控公开活动资料','text':'活动上午09:00开始；两段交通各15分钟和20分钟。'}
    def compute(messages):
        facts=[json.loads(m.content) for m in messages if isinstance(m,ToolMessage)]
        ref=facts[-1]['data']['source']['ref']
        return call('calculate_values',{'operation':'add','values':['15','20'],'source_refs':[ref,ref],'basis':'两段交通分钟相加'},cid='calc')
    store=FakeStore()
    config=replace(CONFIG,preset='shadow_fakewrite')
    with patch.object(deepseek_web,'fetch',fetch),patch.object(deepseek_web,'search',side_effect=AssertionError('known URL forced search')):
        result=run({'message_id':'general-simple','user_id':'user','chat_id':'simple','text':'请直接核对这个链接的活动和交通时间 https://www.shanghai.gov.cn/。两段交通合计多少分钟？'},
            store=store,config=config,model=NativeFake(responses=[call('query_sources',{'operation':'fetch','url':'https://www.shanghai.gov.cn/'}),compute,AIMessage(content='两段交通合计35分钟。')]))
    assert result['status']=='ok' and calls==['https://www.shanghai.gov.cn/']
    assert result['tool_results'][1]['data']['calculation']['result']=='35'
    assert not store.load_document('task_active','system') and not store.load_document('user_settings','user')
    # Explicit counterevidence uses the settings interface's feedback operation;
    # ordinary feedback remains prose/review material without settings changes.
    from harness.eval.memory_cases import feedback_fixture,send
    from agent.storage import experience
    store,snapshot,candidate=feedback_fixture()
    with patch('agent.storage.experience.local_day',return_value=store.today):
        experience.apply_candidate(store,{'candidate':candidate},config)
    view=memory.state(store,'user');theme=memory.get(store,'user',view['entries'][0])[0];ref=theme['links'][0]
    send(store,'plain-evaluation','f','分开打包的建议我看过了。',AIMessage(content='了解。'))
    assert memory.get(store,'user',ref)[0]['status']=='usable'
    send(store,'counterexample','f','分开打包反而让我更麻烦，不再采用这个经验。',
        call('update_user_settings',{'kind':'feedback','values':{'ref':ref,'signal':'reject','quote':'分开打包反而让我更麻烦'}}))
    assert not memory.context_view(store,'user',store.today,'next',{},'带午饭并在用餐处加热').get('experiences')
    assert not store.load_document('user_settings','user')
    EVIDENCE['C0']={'known_url_fetches':calls,'searches':0,'calculation_minutes':'35','forced_workers':0,
        'explicit_counterevidence':'disabled_same_turn','ordinary_evaluation':'settings_unchanged','model':'native injected model'}


def cutover_delta_and_lost_ack():
    """S3: moving delta, old ingress event, lost enable response, stale warm writer, interrupted delete and target-only restart."""
    objects=Objects();prefix,now=old_state(objects,'isolated/stage-c/delta/')
    pipeline=Pipeline(objects,prefix)
    receipt=pipeline.accept({'message_id':'queued-before-cutover','user_id':'user','chat_id':'new','text':'查状态'},source='cli')
    old=CosStore(objects,prefix)
    old.save_document('memory_index','user','',{'items':{'old':{'text':'旧索引'}}})
    maint=Maintenance(objects,prefix);state=maint.prepare('g1',now=now,compact=True)
    # Real changes after preparation, before each sole writer freezes.
    queued=pipeline.accept({'message_id':'queued-during-preparation','user_id':'user','chat_id':'new','text':'查询最新'},source='cli')
    from agent.storage.execution_pipeline import receipt_id
    sid=receipt_id('user','new')
    old.save_document('ingress_cursor','system',sid,{'sequence':receipt['sequence'],'position':-1})
    old.save_document('memory_index','user','',{'items':{'new':{'text':'新索引'}}})
    old.write_intake({'message_id':'delta','user_id':'user','text':'增量'},[
        {'matched_name':'增量','grams':10,'date':'2026-11-13','nutrition':{'kcal':6,'carbs':1,'protein':.2,'fat':.1}}])
    maint.request_pause('g1');ctl=maint.control
    ctl.create(ctl.key(0,'started'),{'started_at':now,'decision_deadline':now+40,'resume_deadline':now+60})
    for role in DOMAINS:
        journal=Journal(objects,prefix+role+'/')
        ctl.acknowledge(role,0,journal.revision)
    original=objects.create;lost=[False]
    def uncertain(key,body):
        result=original(key,body)
        if key==ctl.key(0,'decision') and not lost[0]:lost[0]=True;raise OSError('enable response lost')
        return result
    objects.create=uncertain
    assert maint.finish('g1')['choice']=='enable'
    objects.create=original
    assert ctl.translate_ingress(prefix,receipt['ingress_revision']) is not None
    reboot_pipeline=Pipeline(objects,prefix)
    assert reboot_pipeline.readonly_store().load_document('ingress_cursor','system',sid)['position']==0
    index = reboot_pipeline.readonly_store().load_document('memory_index','user')['items']
    assert dict(index)=={'new':{'text':'新索引'}} and hasattr(index,'persist')
    assert reboot_pipeline.inbox(readonly=True).get('receipt:'+queued['receipt_id'])['session_position']==1
    seen=[]
    def execute(msg,**kwargs):
        seen.append(msg['message_id'])
        return {'status':'ok','reply':'已读取本条状态。','tool_results':[],'native_calls':[]}
    def event(item):
        return {**{key:item[key] for key in ('receipt_id','ingress_revision','ingress_generation')},
                **{key:item['message'][key] for key in ('message_id','user_id','chat_id')}}
    result=reboot_pipeline.worker(event(queued),execute=execute)
    assert result['status']=='awaiting_presentation'
    assert seen==['queued-during-preparation']
    pause={'type':'calorie_maintenance_pause','epoch':0,'task':'g1'}
    assert reboot_pipeline.worker(pause)['status']=='obsolete_pause'
    assert ctl.read(ctl.key(1,'started')) is None
    for n in range(4):
        new=reboot_pipeline.accept({'message_id':f'new-after-cutover-{n}','user_id':'user','chat_id':'new','text':'查询'},source='cli')
    wakes=[]
    reboot_pipeline.recover(wake=wakes.append)
    recovered=next(item for item in wakes if item['receipt_id']==new['receipt_id'])
    assert recovered['ingress_generation']==state['target']
    assert recovered['ingress_revision']>ctl.read(ctl.key(0,'verified'))['versions']['ingress']
    assert reboot_pipeline.worker(event(recovered),execute=execute)['status']=='awaiting_presentation'
    objects.stale=True
    try:old.save_document('user_settings','user','',{'targets':{'kcal':999}})
    except MaintenancePaused:pass
    else:raise AssertionError('old warm writer reused retired sequence')
    readonly=CosStore(objects,prefix,readonly=True)
    assert readonly.intake_items('user','2026-11-13','2026-11-14')[0]['nutrition']['kcal']==6
    objects.stale=False
    denied={role:{'status':403,'source':prefix,'transport':'controlled'} for role in DOMAINS}
    failed=[False]
    def delete(key):
        objects.delete(key)
        if not failed[0]:failed[0]=True;raise OSError('delete acknowledgement lost')
    try:maint.collect('g1',restart_verified=True,denied_roles=denied,delete=delete)
    except OSError:pass
    else:raise AssertionError('delete interruption absent')
    result=maint.collect('g1',restart_verified=True,denied_roles=denied,limit=10000)
    assert result['remaining']==0
    final=CosStore(objects,prefix,readonly=True)
    assert final.intake_items('user','2026-11-13','2026-11-14')[0]['nutrition']['kcal']==6
    maint=Maintenance(objects,prefix)
    next_state=maint.prepare('g2',compact=True)
    current=CosStore(objects,prefix)
    index=current.load_document('memory_index','user')
    index['items'].pop('new')
    index['items']['latest']={'text':'第二代增量'}
    current.save_document('memory_index','user','',index)
    current.save_document('dialogue_turn','user','large-after-prepare',{'text':'跨代正文 '*2000})
    maint.request_pause('g2')
    next_epoch=next_state['epoch']
    now=time.time()
    ctl=maint.control
    ctl.create(ctl.key(next_epoch,'started'),{'started_at':now,'decision_deadline':now+40,'resume_deadline':now+60})
    for role in DOMAINS:
        journal=Journal(objects,next_state['source']+role+'/')
        ctl.acknowledge(role,next_epoch,journal.revision)
    assert maint.finish('g2')['choice']=='enable'
    denied={role:{'status':403,'source':next_state['source'],'transport':'controlled'} for role in DOMAINS}
    assert not maint.collect('g2',restart_verified=True,denied_roles=denied,limit=10000)['remaining']
    newest=CosStore(objects,prefix,readonly=True)
    assert dict(newest.load_document('memory_index','user')['items'])=={'latest':{'text':'第二代增量'}}
    assert newest.load_document('dialogue_turn','user','large-after-prepare')['text']=='跨代正文 '*2000
    EVIDENCE['S3_delta']={'lost_enable_ack':True,'old_event':'mapped','old_warm_writer':'rejected',
        'late_legacy_session_append':'position preserved','cutover_recovery':'current generation bound','duplicate_pause':'ignored',
        'second_cutover':'paged updates and artifact body survive old generation collection',
        'stale_list_target_read':'exact_registered_root','delete_interruption':'recovered','target_only_restart_kcal':6}

CHECKS.update({'CG02':general_sources_prompt,'CS04':cutover_delta_and_lost_ack})


def terminal_worker_retention():
    """S2: actual Worker/input/Router shapes expire only after parent closure; ready background candidates stay."""
    objects=Objects();prefix='isolated/stage-c/terminal-workers/'
    store=CosStore(objects,prefix);now=time.time();ended=now-AUDIT_SECONDS-1
    task={'id':'task-fixture','status':'active','cycle':{'id':'cycle-fixture','status':'completed','completed_at':ended,'attempts':['worker-fixture']},
          'latest':'artifact-fixture','parts':{'worker-fixture':'artifact-fixture'}}
    store.save_document('task','user','task-fixture',task)
    store.save_document('task_artifact','user','artifact-fixture',{'ref':'artifact-fixture','items':[{'text':'有效成果保留'}]})
    store.save_document('worker_attempt','user','worker-fixture',{'id':'worker-fixture','task_id':'task-fixture','cycle_id':'cycle-fixture',
        'status':'completed','completed_at':ended,'input_digest':'fixed','artifact_ref':'artifact-fixture','large_audit':'过程 '*3000})
    store.save_document('task_input','user','worker-fixture',{'binding':{'id':'worker-fixture'},'sources':{'text':'过程输入 '*3000}})
    store.save_document('task_router_execution','user','cycle-fixture',{'model_requests':[{'request':'模型过程 '*3000}]})
    progress=Journal(objects,prefix+'sync/')
    progress.commit({'summary:not-applied':{'task_id':'not-applied','status':'ready','candidate':{'text':'尚未应用，保留'}}})
    journals={'ingress':Journal(objects,prefix+'ingress/',readonly=True),'message':store.journal,'sync':progress}
    selected,report=preflight(journals,now)
    assert report['compact_count']==3,report
    artifact_key=next(k for k,v in selected.items() if isinstance(v,dict) and v.get('intent')=='task_artifact')
    assert selected[artifact_key]['payload']['items'][0]['text']=='有效成果保留'
    assert selected['sync/summary:not-applied']['candidate']['text']=='尚未应用，保留'
    # Parent awaiting delivery blocks all three execution/input compactions.
    task['delivery']={'status':'pending'};store.save_document('task','user','task-fixture',task)
    _,pending=preflight(journals,now)
    assert pending['compact_count']==0,pending
    EVIDENCE['terminal_workers']={'closed_parent_compactions':report['compact_count'],'pending_delivery_compactions':pending['compact_count'],
        'artifact':'retained','unapplied_summary_candidate':'retained'}

CHECKS['CS05']=terminal_worker_retention


def compressed_index_updates():
    """S1: compressed-path splits, prefix keys, Unicode, deletions and cold recovery match independent facts."""
    import random
    objects=Objects();journal=Journal(objects,'isolated/stage-c/index/')
    expected={f'root:long-shared-prefix:{i:04d}':{'number':i} for i in range(90)}
    journal.commit(expected)
    randomizer=random.Random(11)
    keys=[*expected,'root:','root','资料:用户:一','','other:branch']
    for n in range(80):
        key=randomizer.choice(keys)
        value=None if n%4==0 else {'number':n,'text':'独立事实'}
        journal.commit({key:value})
        if value is None:expected.pop(key,None)
        else:expected[key]=value
        assert len(journal.index)==len(expected)
        for prefix in ('','root','root:long-shared-prefix:00','资料:','other:'):
            assert set(journal.keys(prefix))=={k for k in expected if k.startswith(prefix)}
    fresh=Journal(objects,journal.prefix,readonly=True)
    assert dict(fresh.state)==expected
    EVIDENCE['compressed_index']={'seed':11,'initial_keys':90,'mutations':80,'prefix_and_cold_recovery':'pass'}

CHECKS['CS06']=compressed_index_updates


def foreground_task_budget():
    """C1: native cross-session controls see the task version and never reset the shared parent budget."""
    from agent import run
    from agent.storage.fake_store import FakeStore
    from langchain_core.messages import AIMessage
    store=FakeStore();config=replace(CONFIG,preset='shadow_fakewrite')
    def execute(mid,text,decision):
        return run({'message_id':mid,'user_id':'user','chat_id':'chat-'+mid,'text':text},store=store,config=config,
            model=NativeFake(responses=[decision,AIMessage(content='已按本次授权处理。')]))
    text='请持续查证公开活动资料，到2027年1月1日结束。'
    result=execute('native-start',text,call('manage_task',{'operation':'start','user_quote':text,
        'spec':{'objective':'公开资料行动清单','done_when':'有出处和未决','ends':'2027-01-01T00:00:00+08:00'}}))
    assert result['status']=='ok',result
    task=tasks.load(store,'user');initial=deepcopy(task['cycle']['parent_used'])
    text='暂停这项通用委托。'
    execute('native-pause',text,call('manage_task',{'operation':'pause','user_quote':text}))
    paused=tasks.load(store,'user');assert paused['status']=='paused' and paused['cycle']['parent_used']==initial
    text='恢复这项委托。'
    execute('native-resume',text,call('manage_task',{'operation':'resume','user_quote':text}))
    resumed=tasks.load(store,'user');assert resumed['cycle']['id']!=task['cycle']['id']
    text='把两个独立的资料核对部分委派出去。'
    execute('native-delegate',text,call('delegate_task',{'task_id':resumed['id'],'revision':resumed['revision'],
        'jobs':[{'objective':'核对第一份公开资料','done_when':'原文与时间条件'}, {'objective':'核对另一份公开资料','done_when':'出处与未决'}]}))
    delegated=tasks.load(store,'user')
    assert delegated['cycle']['parent_used']['decisions']==resumed['cycle']['parent_used']['decisions']+2,delegated
    assert delegated['cycle']['reserved']==tasks.LIMITS and len(delegated['cycle']['attempts'])==2
    text='关闭主动通知。'
    execute('native-notify-off',text,call('manage_task',{'operation':'set_notifications','enabled':False,'user_quote':text}))
    assert tasks.load(store,'user')['cycle']['parent_used']==delegated['cycle']['parent_used']
    EVIDENCE['foreground_task_budget']={'cross_session_version_binding':'pass','pause_preserves_usage':True,
        'new_authorised_resume_cycle':True,'parent_decisions_after_two_messages':delegated['cycle']['parent_used']['decisions'],
        'control_does_not_reset':True}

CHECKS['CG03']=foreground_task_budget


def acquired_sources_survive_foreground_budget():
    """A continued cycle can present source material fetched before its parent budget ended."""
    from agent import run
    from agent.storage import deepseek_web, task_runtime
    from agent.storage.fake_store import FakeStore
    from langchain_core.messages import AIMessage
    store=FakeStore();config=replace(CONFIG,preset='shadow_fakewrite')
    text='持续核对公开活动，到2027年1月1日结束。资料 https://www.shanghai.gov.cn/'
    start=call('manage_task',{'operation':'start','user_quote':text,
        'spec':{'objective':'公开资料行动清单','done_when':'有出处','ends':'2027-01-01T00:00:00+08:00'}})
    def fetch(url,*,deadline,reserve):
        reserve();return {'title':'公开资料','text':'活动在上海举行。'}
    with patch.object(deepseek_web,'fetch',fetch):
        result=run({'message_id':'retain-source','user_id':'user','chat_id':'first','text':text},store=store,config=config,
            model=NativeFake(responses=[start,call('query_sources',{'operation':'fetch','url':'https://www.shanghai.gov.cn/'},cid='fetch'),
                AIMessage(content='资料已取得。')]))
    assert result['status']=='ok'
    task=tasks.load(store,'user');refs=tasks.sources(store,'user',task)
    assert len(refs)==1 and next(iter(refs.values()))['text']=='活动在上海举行。'
    assert task_runtime.RouterRun(store,'user',task).partial_result('待复核')['items'][0]['evidence'][0]['ref'] in refs


CHECKS['CG09']=acquired_sources_survive_foreground_budget


def delegated_handoff_recovery():
    """A failed wake, expired claim and cancelled child do not strand or revive a cycle."""
    from agent.storage import task_runtime
    from agent.storage.scf_dispatch import HandoffRejected
    from agent.storage.fake_store import FakeStore
    from datetime import datetime, timedelta, timezone
    store = FakeStore()
    config = replace(CONFIG,preset='live')
    now = time.time()
    text = '核对公开活动资料。'
    rt = SimpleNamespace(store=store,user_id='user',action_id='wake:1',day=store.today_string(),goal_run=None,
        message={'text':text,'message_id':'wake','chat_id':'chat'},journal={'model_calls':1,'calls':[{}]},session={})
    end = (datetime.now(timezone.utc)+timedelta(hours=1)).isoformat()
    def manage(operation,**args):
        return store.commit_action(rt.action_id,lambda:tasks.manage(rt,{'operation':operation,'user_quote':text,**args}))
    task = manage('start',spec={'objective':text,'done_when':'提供出处','ends':end})['data']['task']
    ident,cycle = task['id'],task['cycle']['id']
    rt.action_id='wake:2'
    child = store.commit_action(rt.action_id,lambda:tasks.delegate(rt,{'task_id':ident,'revision':1,
        'jobs':[{'objective':'核对','done_when':'有原文'}]}))['data']['workers'][0]
    rt.action_id='wake:3'
    manage('stop_worker',task_id=ident,revision=1,worker_id=child)
    assert tasks.load(store,'user',ident)['cycle']['router_pending']
    sent=[]
    def dispatch(event,**kw):
        sent.append(event)
        if len(sent)==1:
            raise HandoffRejected('unknown acknowledgement')
    def sign(*args):return {}
    with patch('time.time',return_value=now+1):
        assert not task_runtime.dispatch_pending(store,dispatch=dispatch,sign=sign)
        assert not task_runtime.dispatch_pending(store,dispatch=dispatch,sign=sign)
    first = sent[0]
    wid = first['worker_id']
    snapshot = store.load_document('task_input','user',wid)
    claim = {'operation':'claim','user_id':'user','task_id':ident,'worker_id':wid,
             'control_token':snapshot['runtime_control_token'],'instance_id':'old',
             'dispatch_id':first['dispatch_id']}
    with patch('time.time',return_value=first['claim_until']+1):
        assert task_runtime.writer(store,claim,config)['status']=='expired_claim'
        assert task_runtime.dispatch_pending(store,dispatch=dispatch,sign=sign)==[wid]
        assert task_runtime.writer(store,claim,config)['status']=='expired_claim'
        assert task_runtime.writer(store,{**claim,'dispatch_id':sent[-1]['dispatch_id'],'instance_id':'new'},config)['status']=='claimed'
    assert store.load_document('worker_attempt','user',wid)['instance_id']=='new'
    rt.action_id='wake:4'
    new_cycle = manage('revise',task_id=ident,revision=1,spec={'objective':'核对修订后的资料'})['data']['task']['cycle']['id']
    assert new_cycle != cycle
    assert task_runtime.writer(store,{'operation':'resume','user_id':'user','task_id':ident,'cycle_id':cycle},config)['status']=='skipped'
    assert not tasks.load(store,'user',ident)['cycle'].get('router_attempt')


CHECKS['CG10']=delegated_handoff_recovery


def cancelled_maintenance_resume():
    """S3: a published cancellation resumes temporary cleanup on the next timer without new threshold candidates."""
    from agent.storage.generations import maintenance_tick
    objects=Objects();prefix,now=old_state(objects,'isolated/stage-c/cancelled/')
    maint=Maintenance(objects,prefix)
    foreign=prefix+'generations/collision/message/owner-private.txt'
    objects.create(foreign,encode({'owner':'unregistered'}))
    try:maint.prepare('collision',now=now,compact=True)
    except ValueError:pass
    else:raise AssertionError('unregistered target was reused')
    assert objects.get(foreign) is not None
    state=maint.prepare('g1',now=now,compact=True);maint.request_pause('g1')
    control=maint.control
    control.create(control.key(0,'started'),{'started_at':now-41,'decision_deadline':now-1,'resume_deadline':now+19})
    control.decide(0,'cancel')
    ledger=Journal(objects,prefix+'control/maintenance/')
    ledger.commit({'active':{'task':'g1','stage':'pause'}})
    pipeline=Pipeline(objects,prefix)
    def no_dispatch(*args,**kwargs):raise AssertionError('published cancellation must not restart preparation')
    assert maintenance_tick(pipeline,dispatch=no_dispatch)['status']=='cancel'
    result=maintenance_tick(pipeline,dispatch=no_dispatch)
    while result.get('remaining',0):result=maintenance_tick(pipeline,dispatch=no_dispatch)
    assert result['status']=='retirement_resumed'
    assert not any(k.startswith(state['target']) for k in objects.data)
    assert maint.control.location()[1]==prefix
    assert CosStore(objects,prefix,readonly=True).intake_by_ids('user',['rec-existing-feishu-id'],True)
    EVIDENCE['cancelled_maintenance']={'published_decision':'cancel','next_tick_cleanup':'resumed','current_source_preserved':True}

CHECKS['CS07']=cancelled_maintenance_resume


def delegation_snapshot_atomicity():
    """C1: an oversized second snapshot leaves no first child, input object or reservation behind."""
    objects=Objects();store=CosStore(objects,'isolated/stage-c/atomic-input/')
    text='持续核对这些资料，到2027年1月1日结束。'
    sources={}
    for n in range(12):
        item=source(('原文资料'+str(n)+' ') * 900,url='https://www.shanghai.gov.cn/'+str(n))
        sources[item['ref']]=item
    rt=SimpleNamespace(store=store,user_id='user',action_id='atomic:agent:1',day='2026-11-13',goal_run=None,
        message={'text':text,'message_id':'atomic','chat_id':'atomic'},journal={'model_calls':1,'calls':[{}]},session={'general_sources':sources})
    first=store.commit_action(rt.action_id,lambda:tasks.manage(rt,{'operation':'start','user_quote':text,
        'spec':{'objective':'资料核对','done_when':'有出处与未决','ends':'2027-01-01T00:00:00+08:00'}}))
    ident=first['data']['task']['id'];rt.action_id='atomic:agent:2';rt.journal['calls'].append({})
    refs=list(sources)
    result=store.commit_action(rt.action_id,lambda:tasks.delegate(rt,{'task_id':ident,'revision':1,'jobs':[
        {'objective':'小输入','done_when':'引文','source_refs':[refs[0]]},
        {'objective':'过大输入','done_when':'引文','source_refs':refs}]}))
    assert result['status']=='pending' and result['data']['reason']=='input_snapshot_too_large',result
    assert not store.list_documents('worker_attempt') and not store.list_documents('task_input')
    cycle=tasks.load(store,'user',ident)['cycle']
    assert not cycle['attempts'] and cycle['reserved']==tasks.CHILD_LIMIT
    EVIDENCE['snapshot_atomicity']={'oversized_input':'pending_with_bound','orphan_workers':0,'orphan_inputs':0,'child_reservations':0}

CHECKS['CG04']=delegation_snapshot_atomicity


def expired_parent_reception():
    """C1/S3: authorisation expiry rejects steps and late results, retaining reception after the parent leaves its active index."""
    from datetime import datetime,timezone
    from agent.storage.task_runtime import dispatch_pending
    objects=Objects();store=CosStore(objects,'isolated/stage-c/expired-parent/');now=time.time()
    text='持续查证这份资料，截止后停止。';item=source('活动公开资料。',url='https://www.shanghai.gov.cn/')
    rt=SimpleNamespace(store=store,user_id='user',action_id='expire:agent:1',day='2026-11-13',goal_run=None,
        message={'text':text,'message_id':'expire','chat_id':'expire'},journal={'model_calls':1,'calls':[{}]},session={'general_sources':{item['ref']:item}})
    started=store.commit_action(rt.action_id,lambda:tasks.manage(rt,{'operation':'start','user_quote':text,
        'spec':{'objective':'资料查证','done_when':'出处与未决','ends':datetime.fromtimestamp(now+30,timezone.utc).isoformat()}}))
    ident=started['data']['task']['id'];rt.action_id='expire:agent:2';rt.journal['calls'].append({})
    delegated=store.commit_action(rt.action_id,lambda:tasks.delegate(rt,{'task_id':ident,'revision':1,'jobs':[
        {'objective':'第一部分','done_when':'引文','source_refs':[item['ref']]}, {'objective':'第二部分','done_when':'引文','source_refs':[item['ref']]}]}))
    ids=delegated['data']['workers'];expired=now+31
    def candidate(wid):
        snapshot=store.load_document('task_input','user',wid)
        return {'schema':'calorie.task-candidate.v1','binding':snapshot['binding'],'outcome':'complete',
            'items':[{'text':item['text'],'evidence':[{'ref':item['ref'],'quote':item['text']}]}],
            'sources':{item['ref']:item},'usage':{'decisions':1,'tools':1,'web':0}}
    attempt=store.load_document('worker_attempt','user',ids[0]);attempt['instance_id']='own-instance'
    store.save_document('worker_attempt','user',ids[0],attempt)
    token=store.load_document('task_input','user',ids[0])['runtime_control_token']
    assert not tasks.control(store,{'user_id':'user','task_id':ident,'worker_id':ids[0],'instance_id':'own-instance','control_token':token},now=expired)['allowed']
    assert tasks.publish(store,{'user_id':'user','task_id':ident,'worker_id':ids[0],'candidate':candidate(ids[0])},now=expired)['status']=='rejected'
    tasks.tick(store,replace(CONFIG,preset='live'),now=expired)
    assert not store.load_document('task_active','system') and ids[1] in store.load_document('worker_active','system')
    attempt=store.load_document('worker_attempt','user',ids[1])
    objects.create(attempt['generation']+attempt['candidate_key'],encode(candidate(ids[1])))
    def forbidden(*a,**kw):raise AssertionError('expired parent must not dispatch computation or delivery')
    with patch('time.time',return_value=expired),patch('agent.storage.scf_dispatch.dispatch_event',side_effect=forbidden):
        Pipeline(objects,store.prefix).worker({'type':'calorie_reminder_check',
            'scheduled_time':datetime.fromtimestamp(expired,timezone.utc).isoformat()})
    store.confirm_current()
    assert not store.load_document('worker_active','system')
    assert not tasks.load(store,'user',ident).get('latest')
    EVIDENCE['expired_parent']={'control_after_authorisation_end':'denied','late_results':'rejected','independent_reception':'closed_after_fixed_key_read','new_artifact':False}

CHECKS['CG05']=expired_parent_reception


def control_transport_privacy():
    """C1: scoped capability travels in a header, never in the control URL or public logs."""
    import contextlib,io,os,httpx,app
    from agent.storage.task_runtime import read_control
    from agent.storage.live_client import LiveClient
    capability='private-fixture-control-capability'
    seen=[];queries=[]
    def http(request):
        seen.append(str(request.url))
        assert capability not in str(request.url)
        assert request.headers['X-Calorie-Worker-Token']==capability
        from harness.eval.production_pipeline_cases import assert_cam_request
        assert_cam_request(request)
        response=app.ingress_handler({'httpMethod':'GET','path':request.url.path,'rawQueryString':request.url.query.decode(),
            'headers':dict(request.headers)})
        return httpx.Response(response['statusCode'],json=json.loads(response['body']))
    pipeline=SimpleNamespace(readonly_store=lambda:None)
    query={'user_id':'user','task_id':'task','worker_id':'worker','instance_id':'instance','control_token':capability}
    output=io.StringIO()
    cam={'CALORIE_SERVICE_URL':'https://fixture','CALORIE_SERVICE_TOKEN':'fixture-service-token','CALORIE_SERVICE_AUTH':'cam',
         'CALORIE_CLOUD_UIN':'100000000001','TENCENTCLOUD_SECRET_ID':'function-role','TENCENTCLOUD_SECRET_KEY':'function-key',
         'TENCENTCLOUD_SESSION_TOKEN':'function-session'}
    with patch.dict(os.environ,cam), patch.object(app,'configured_pipeline',return_value=pipeline),patch.object(tasks,'control',side_effect=lambda store,p:queries.append(p) or {'allowed':True}), httpx.Client(transport=httpx.MockTransport(http)) as transport, contextlib.redirect_stdout(output):
        assert read_control(query,LiveClient(client=transport))['allowed']
    assert queries[0]['control_token']==capability and capability not in output.getvalue()
    from urllib.parse import parse_qs, urlsplit
    from qcloud_cos import CosS3Client
    from agent.storage.cos_transport import CosObjects
    with patch.object(CosS3Client,'get_bucket_versioning',return_value={}):
        objects=CosObjects('calorie-fixture-1234567890','ap-shanghai',credentials=('fixture-id','fixture-key','temporary-session'))
    urls=objects.worker_urls('workers/input.json','workers/candidate.json',30)
    for url in urls.values():
        params=parse_qs(urlsplit(url).query)
        assert params['x-cos-security-token']==['temporary-session']
        assert 'x-cos-security-token' in params['q-url-param-list'][0].split(';')
    assert 'x-cos-forbid-overwrite' in parse_qs(urlsplit(urls['candidate_url']).query)['q-header-list'][0].split(';')
    from agent.storage.task_runtime import worker_handler
    signed='https://fixture/input?q-signature=private-fixture-signature'
    def unavailable():
        request=httpx.Request('GET',signed)
        raise httpx.HTTPStatusError('signed transport failure '+signed,request=request,response=httpx.Response(403,request=request))
    hidden=io.StringIO()
    with contextlib.redirect_stdout(hidden):
        failure=worker_handler({'user_id':'user','task_id':'task','worker_id':'worker','deadline_at':time.time()+30,
                                'claim_until':time.time()+15,'input_digest':'unused'},get=unavailable)
    assert failure['status']=='transport_failed' and 'private-fixture-signature' not in hidden.getvalue()
    EVIDENCE['control_transport']={'capability_location':'header','url_and_public_logs':'no_capability','scoped_identity_fields':4,'signed_transport_error':'redacted'}

CHECKS['CG06']=control_transport_privacy


def sequence_rebuild_append():
    """S1: rebuilding paged source order then appending preserves exact turns, boundaries and withdrawal metadata."""
    objects=Objects();store=CosStore(objects,'isolated/stage-c/sequence/')
    turns=[{'id':'turn-'+str(n),'tokens':n+1,'complete':True} for n in range(40)]
    store.save_document('conversation_context','user','chat',{'version':1,'summary':{},'turns':turns})
    rebuilt=[{'id':'restored-'+str(n),'tokens':1,'complete':True} for n in range(5)]+turns
    store.save_document('conversation_context','user','chat',{'version':1,'summary':{},'turns':rebuilt})
    value=store.load_document('conversation_context','user','chat')
    value['turns'].append({'id':'new-turn','tokens':3,'complete':True})
    store.save_document('conversation_context','user','chat',value)
    quotes=['撤回片段'+str(n) for n in range(70)]
    store.save_document('memory_state','user','',{'redacted_quotes':quotes,'entries':['theme:'+str(n) for n in range(40)]})
    view=store.load_document('memory_state','user');view['redacted_quotes'].append('最后一个撤回片段')
    store.save_document('memory_state','user','',view)
    cold=CosStore(objects,store.prefix,readonly=True)
    assert [turn['id'] for turn in cold.load_document('conversation_context','user','chat')['turns']]==[turn['id'] for turn in rebuilt]+['new-turn']
    assert list(cold.load_document('memory_state','user')['redacted_quotes'])==quotes+['最后一个撤回片段']
    for key in cold.journal.keys('sequence:'):assert len(cold.journal.get(key))<=32
    EVIDENCE['source_sequences']={'rebuild_then_append':'exact_order','withdrawal_fragments':71,'page_capacity':32,'cold_read':'pass'}

CHECKS['CS08']=sequence_rebuild_append


def delegated_memory_versions():
    """C1: delegated memory is usable, invalid quotes get feedback, and later revisions fence candidates and published projections."""
    from agent.storage.task_runtime import dispatch_pending,worker_handler,writer
    objects=Objects();store=CosStore(objects,'isolated/stage-c/memory-version/');day=store.today_string()
    old=memory.save(store,'user','condition','equipment',{'text':'只能用微波炉','status':'active','starts':day,'ends':'2027-01-01'})
    text='持续核对这些个人条件，到2027年1月1日结束。'
    rt=SimpleNamespace(store=store,user_id='user',action_id='memory:agent:1',day=day,goal_run=None,
        message={'text':text,'message_id':'memory','chat_id':'memory'},journal={'model_calls':1,'calls':[{}]},session={})
    goal=store.commit_action(rt.action_id,lambda:tasks.manage(rt,{'operation':'start','user_quote':text,
        'spec':{'objective':'核对个人条件','done_when':'有实际来源的条件','ends':'2027-01-01T00:00:00+08:00'}}))['data']['task']
    def delegate(action):
        rt.action_id=action;rt.journal['calls']=[{},{}]
        result=store.commit_action(action,lambda:tasks.delegate(rt,{'task_id':goal['id'],'revision':1,'jobs':[
            {'objective':'核对已提供的个人来源','done_when':'原文与状态','memory_refs':[old['ref']]}]}))
        return result['data']['workers'][-1]
    wid=delegate('memory:agent:2');attempt=store.load_document('worker_attempt','user',wid);snapshot=store.load_document('task_input','user',wid)
    config=replace(CONFIG,preset='live');saved=[]
    events=[]
    dispatch_pending(store,dispatch=lambda event,**kw:events.append(event),sign=lambda *_:{})
    event=events[0]
    candidate={'outcome':'complete','items':[{'text':old['text'],'evidence':[{'ref':old['ref'],'quote':old['text']}]}]}
    bad=deepcopy(candidate);bad['items'][0]['evidence'][0]['quote']='并不存在的引用'
    result=worker_handler(event,get=lambda:encode(snapshot),put=lambda body:saved.append(json.loads(body)),
        claim=lambda q:writer(store,{'operation':'claim',**q},config),control=lambda q:tasks.control(store,q),report=lambda q:None,
        model=NativeFake(responses=[call('submit_task_result',bad,cid='bad'),call('query_sources',{'operation':'read','ref':old['ref']},cid='read'),call('submit_task_result',candidate,cid='good')]))
    assert result['status']=='candidate_saved' and saved[0]['usage']['decisions']==3
    assert tasks.publish(store,{**event,'candidate':saved[0]})['status']=='completed'
    task=tasks.load(store,'user',goal['id']);assert tasks.apply_result(store,'user',goal['id'],task['cycle']['id'],saved[0])['status']=='published'
    rt.action_id='memory-next:agent:1';rt.message={'message_id':'memory-next','text':'再次核对这些条件。','chat_id':'next'};rt.journal={'model_calls':1,'calls':[{}]}
    store.commit_action(rt.action_id,lambda:tasks.manage(rt,{'operation':'request_run','task_id':goal['id'],'revision':1,'user_quote':rt.message['text']}))
    later=delegate('memory-next:agent:2');snapshot2=store.load_document('task_input','user',later)
    before=memory.redaction_revision(store,'user')
    memory.save(store,'user','condition','equipment',{'text':'现在可以使用其他设备','status':'active','starts':day,'ends':'2027-01-01'},new_version=True)
    assert memory.redaction_revision(store,'user')==before
    late={**saved[0],'binding':snapshot2['binding']}
    assert tasks.publish(store,{'user_id':'user','task_id':goal['id'],'worker_id':later,'candidate':late})['status']=='rejected'
    rt.message['text']='查看最新成果。'
    shown=tasks.query(rt,{'operation':'read_artifact','task_id':goal['id'],'view_quote':rt.message['text']})
    assert shown['data']['artifact']['outcome']=='partial' and not shown['data']['artifact']['items']
    assert old['text'] not in json.dumps(shown['data']['artifact'],ensure_ascii=False)
    EVIDENCE['delegated_memory']={'invalid_quote':'feedback_then_corrected','native_decisions':3,
        'usable_delegated_source':True,'same_redaction_revision_source_change':'late_candidate_rejected','published_stale_content':'filtered'}

CHECKS['CG07']=delegated_memory_versions


def readonly_control_entries():
    """C0: readonly controls, timer, maintenance and compute refuse all external effects."""
    import app
    from agent.storage import task_runtime
    readonly=replace(CONFIG,preset='shadow_readonly')
    forbidden=lambda *a,**k:(_ for _ in ()).throw(AssertionError('readonly accessed external resources'))
    pipeline=Pipeline(None,'isolated/stage-c/readonly/')
    with patch('agent.config.load_config',return_value=readonly),patch.object(app,'load_config',return_value=readonly),patch.object(app,'configured_pipeline',side_effect=forbidden):
        for kind in ('calorie_task','calorie_task_tick','calorie_reminder_check','calorie_reminder_recover','calorie_maintenance_pause'):
            assert pipeline.worker({'type':kind})['status']=='skipped_preset'
            assert app.message_handler({'type':kind})['status']=='skipped_preset'
        assert app.message_handler({'Time':'2026-11-13T04:00:00Z'})['status']=='skipped_preset'
        assert app.ingress_handler({'type':'calorie_maintenance_pause'})['status']=='skipped_preset'
        assert app.maintenance_handler({})['status']=='skipped_preset'
        assert task_runtime.worker_handler({},get=forbidden,put=forbidden)['status']=='skipped_preset'
    EVIDENCE['readonly_controls']={'entries':9,'external_effects':0}

CHECKS['CG08']=readonly_control_entries


def manual_recovery_cursor():
    """S1: expired manual recovery stays independent while later same-chat work proceeds."""
    from harness.eval.production_pipeline_cases import message,event
    from agent.storage.execution_pipeline import receipt_id
    objects=Objects();pipeline=Pipeline(objects,'isolated/stage-c/manual-cursor/')
    old=pipeline.accept(message('old-manual'),received_at=time.time()-1000)
    store=pipeline.store()
    original={'status':'needs_attention','work_status':'needs_attention','attempts':3,'deadline_at':time.time()-10}
    store.save_document('receipt_state','system',old['receipt_id'],original)
    store.save_document('execution','system','original-plan',{'status':'prepared','message_id':'old-manual','fixed_uuid':'original'})
    new=pipeline.accept(message('new-work'))
    seen=[]
    def execute(msg,**kwargs):
        seen.append(msg['message_id']);return {'status':'ok','reply':'新消息完成。','errors':[]}
    with patch('agent.config.load_config',return_value=replace(CONFIG,preset='live')):
        result=pipeline.worker(event(new),execute=execute,send=lambda *a,**k:{'data':{'message_id':'new-sent'}})
    assert result['status']=='completed' and seen==['new-work']
    assert store.load_document('receipt_state','system',old['receipt_id'])==original
    assert store.load_document('execution','system','original-plan')['fixed_uuid']=='original'
    cursor=store.load_document('ingress_cursor','system',receipt_id('user','test'))
    assert cursor['sequence']==new['sequence'] and cursor['position']==new['session_position']
    recovered=pipeline.recover(wake=lambda r:None)
    assert old['receipt_id'] in recovered['blocked']
    EVIDENCE['manual_cursor']={'later_same_chat':'completed','old_fixed_plan':'retained','recovery_index':'independent','cursor_position':cursor['position']}

CHECKS['CS09']=manual_recovery_cursor


def reminders_restart_catchup():
    """S1/S3: empty checks do not journal; fixed retry and fifteen-minute catchup survive restart."""
    from datetime import datetime,timezone,timedelta
    from agent.storage.reminders import check_reminders,recover_deliveries,catch_up
    objects=Objects();prefix='isolated/stage-c/reminders/';store=CosStore(objects,prefix)
    stamp=datetime(2026,11,13,4,tzinfo=timezone.utc)
    def rule(time='12:00',enabled=True):return {'enabled':enabled,'time':time,'meal_type':'lunch','weekdays':list(range(7))}
    store.save_document('user_settings','user','',{'feishu_chat_id':'chat','reminders':{'noon':rule()}})
    before=store.journal.revision
    for n in range(3):assert check_reminders(store,stamp-timedelta(minutes=10-n),lambda *a,**k:None)['sent']==0
    assert store.journal.revision==before
    sent=[]
    def send(chat,text,uuid_key):
        sent.append((chat,text,uuid_key))
        if len(sent)==1:raise ConnectionError('controlled send failure')
        return {'data':{'message_id':'fixed-delivery'}}
    try:check_reminders(store,stamp,send)
    except ConnectionError:pass
    else:raise AssertionError('failure not retained')
    fresh=CosStore(objects,prefix)
    assert recover_deliveries(fresh,send)['sent']==1 and sent[0]==sent[1]
    assert check_reminders(fresh,stamp,send)['sent']==0
    assert not fresh.load_document('reminder_delivery_active','system')
    # Catch up the original due minute once, skip older than fifteen minutes.
    day2=stamp+timedelta(days=1)
    assert catch_up(fresh,day2.timestamp()-30,day2.timestamp()+14*60,send)['sent']==1
    assert catch_up(fresh,day2.timestamp()-30,day2.timestamp()+14*60,send)['sent']==0
    day3=stamp+timedelta(days=2)
    assert catch_up(fresh,day3.timestamp()-30,day3.timestamp()+16*60,send)['sent']==0
    # A prepared failure is independently recoverable, with no new send after disable.
    def fail(*a,**k):raise ConnectionError('controlled')
    try:check_reminders(fresh,day3,fail)
    except ConnectionError:pass
    fresh.save_document('user_settings','user','',{'feishu_chat_id':'chat','reminders':{'noon':rule(enabled=False)}})
    assert recover_deliveries(CosStore(objects,prefix),send)['sent']==0
    assert not CosStore(objects,prefix).load_document('reminder_delivery_active','system')
    # Current recorded meals suppress a due reminder.
    fresh=CosStore(objects,prefix)
    fresh.save_document('user_settings','user','',{'feishu_chat_id':'chat','reminders':{'noon':rule()}})
    day4=stamp+timedelta(days=3)
    fresh.write_intake({'message_id':'already-ate','user_id':'user','text':'午餐'},[{'food_id':'rice','matched_name':'米饭','grams':100,'meal_type':'lunch','date':day4.date().isoformat(),'nutrition':{'kcal':120,'carbs':25,'protein':3,'fat':1}}])
    assert check_reminders(fresh,day4,send)['sent']==0
    EVIDENCE['reminder_recovery']={'empty_checks':3,'extra_journal_commits':0,'restart_retry':'same_uuid','catchup_minutes':14,'older_than_15':'skipped','disabled_and_recorded':'suppressed'}

CHECKS['CS10']=reminders_restart_catchup
