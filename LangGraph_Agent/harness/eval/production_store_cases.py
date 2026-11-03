"""Production Store acceptance through the runtime and injected object transport."""
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from unittest.mock import patch
from agent import run
from agent.storage.cos_store import CosStore
from agent.storage.cos_journal import CommitUnavailable, Journal
from agent.storage import feishu_client
from harness.eval.personal_cases import A, B, CONFIG, MEAL, NativeFake, call
from harness.eval.conversation_cases import answer


class Objects:
    def __init__(self):
        self.data, self.hidden = {}, set()
        self.stale = False
        self.creates = 0
        self.lose_action = False
        self.interrupt_action = False
        self.fail_document = False
        self.fault_action_prefix = 'action:'

    def get(self, key): return None if key in self.hidden else self.data.get(key)
    def list(self, prefix): return [] if self.stale else [k for k in self.data if k.startswith(prefix)]
    def create(self, key, body):
        self.creates += 1
        if key in self.data: return False
        value = json.loads(body)
        if self.fail_document and '/documents/' in key and isinstance(value,dict) and value.get('intent')=='message_execution' and any('result' in c for c in value.get('payload',{}).get('calls',[])):
            self.fail_document = False
            raise OSError('injected tool result document upload failure')
        action_commit = '/commits/' in key and any(k.startswith(self.fault_action_prefix) and k!='action:baseline-import' for k in value.get('changes',{}))
        if action_commit and self.interrupt_action:
            self.interrupt_action=False
            raise OSError('injected before commit')
        self.data[key]=body
        if action_commit and self.lose_action:
            self.lose_action=False
            raise OSError('injected after commit response loss')
        return True


def open_store(objects=None):
    objects = objects or Objects()
    store = CosStore(objects,'acceptance/production')
    if not store.journal.get('baseline'):
        foods = deepcopy([A,B])
        foods[1].update(units=[{'unit':'个','eat_weight':50,'weight':60}],detail_loaded=True)
        store.import_baseline({'baseline_id':'fixture-v1','foods':foods})
    return store


def send(store, mid, steps, text='记录或查询'):
    model=NativeFake(responses=steps)
    result=run({'message_id':mid,'user_id':'user','chat_id':'test','text':text},store=store,
               config=replace(CONFIG,preset='live'),model=model)
    assert result['status']=='ok', result.get('errors')
    return result,model


def production_business():
    """生产Store：270→210→105→315、同口径、独立提交解码、重投和零Bitable。"""
    store=open_store()
    objects=store.objects
    def forbidden(*args,**kwargs): raise AssertionError('Bitable in message path')
    with patch.object(feishu_client,'http_json_get',forbidden),patch.object(feishu_client,'http_json_post',forbidden):
        first,_=send(store,'p-first',[call('record_meal',MEAL),answer])
        assert store.today_summary('user')['totals']['kcal']==270
        store=open_store(objects)
        send(store,'p-revise',[call('revise_intake',{'food_query':'夹具A','grams':100}),answer])
        assert store.today_summary('user')['totals']['kcal']==210
        send(store,'p-reuse',[call('record_meal',{'reuse':{'latest':True,'ratio':.5},'meal_type':'snack'}),answer])
        for detail in ('calories','summary','items'):
            r,_=send(store,'p-query-'+detail,[call('query_today',{'detail':detail}),answer])
            assert r['tool_results'][0]['data']['total']['kcal']==315
        current=store.load_session('user','test')
        objects.stale=True
        old=open_store(objects)
        replay,model=send(old,'p-first',[])
        assert not model.requests and replay['reply']==first['reply']
        assert old.load_session('user','test')==current
        assert old.today_summary('user')['totals']['kcal']==315
    # Decode the immutable commit chain and document bytes independently.
    refs={}
    for key in sorted(k for k in objects.data if '/message/commits/' in k):
        c=json.loads(objects.data[key])
        for k,v in c['changes'].items():
            if v is None: refs.pop(k,None)
            else: refs[k]=v
    rows=[json.loads(objects.data['acceptance/production/message/documents/'+v+'.json']) for k,v in refs.items() if k.startswith('item:')]
    assert len(rows)==4 and sum(r['nutrition']['kcal'] for r in rows)==315
    count=objects.creates
    readonly=CosStore(objects,'acceptance/production',readonly=True)
    result=run({'message_id':'ro','user_id':'user','chat_id':'test','text':'今日热量'},store=readonly,
               config=replace(CONFIG,preset='shadow_readonly'),model=NativeFake(responses=[call('query_today',{}),answer]))
    assert result['status']=='ok' and objects.creates==count
    assert result['storage_revision']==readonly.journal.revision


def production_recovery():
    """生产提交：写前中断、响应丢失、陈旧列表、已知提交不可见和旧实例重接。"""
    for fault in ('lose_action','interrupt_action','fail_document'):
        store=open_store()
        objects=store.objects
        objects.fault_action_prefix = 'action:p-crash:agent:'
        setattr(objects,fault,True)
        try: send(store,'p-crash',[call('record_meal',MEAL),answer])
        except (CommitUnavailable,OSError): pass
        else: raise AssertionError('unknown commit must fail closed')
        try: store.today_summary('user')
        except CommitUnavailable: pass
        else: raise AssertionError('unknown write served stale facts')
        objects.stale=True
        restored=open_store(objects)
        recovered,model=send(restored,'p-crash',[answer])
        assert restored.today_summary('user')['totals']['kcal']==270
        assert len(restored.intake_items('user'))==2
        # Only the reserved original model call and one final model call.
        execution=restored.load_execution({'message_id':'p-crash'})
        assert execution['payload']['model_calls']==2
    # An old accepted 'today' query retains its original date across midnight.
    from agent.storage.timeutil import BUSINESS_DATE
    store=open_store()
    send(store,'old-date-meal',[call('record_meal',MEAL),answer])
    original_day=store.today_string()
    with patch('agent.storage.cos_store.today_string',return_value='2099-01-01'):
        model=NativeFake(responses=[call('query_today',{}),answer])
        queried=run({'message_id':'old-date-query','user_id':'user','chat_id':'test','text':'今日热量'},
                    store=store,config=replace(CONFIG,preset='live'),model=model,intake_date=original_day)
    assert queried['tool_results'][0]['data']['date']==original_day
    assert queried['tool_results'][0]['data']['total']['kcal']==270
    assert BUSINESS_DATE.get() is None
    # Prepared old actions can resume after a newer read, without changing its session.
    from langchain_core.messages import AIMessage
    class Death(BaseException): pass
    store=open_store()
    original=store.save_document
    def fail_settings(kind,*args,**kwargs):
        if kind=='user_settings': raise Death()
        return original(kind,*args,**kwargs)
    store.save_document=fail_settings
    msg={'message_id':'old-partial','user_id':'user','chat_id':'test','text':'记录并设置目标'}
    batch=AIMessage(content='',tool_calls=[
        {'name':'record_meal','args':MEAL,'id':'c1','type':'tool_call'},
        {'name':'update_user_settings','args':{'kind':'targets','values':{'kcal':1900}},'id':'c2','type':'tool_call'}])
    try: run(msg,store=store,config=replace(CONFIG,preset='live'),model=NativeFake(responses=[batch,answer]))
    except Death: pass
    else: raise AssertionError('missing settings interruption')
    store=open_store(store.objects)
    send(store,'later-read',[call('query_today',{}),answer])
    current=store.load_session('user','test')
    recovered=run(msg,store=store,config=replace(CONFIG,preset='live'),model=NativeFake(responses=[answer]))
    assert recovered['status']=='ok', recovered['errors']
    assert store.get_standard('user')['kcal']==1900
    assert store.load_session('user','test')==current
    old=open_store()
    newer=open_store(old.objects)
    newer.save_document('test','user','',{'value':2})
    key=newer.journal.key(newer.journal.revision)
    old.objects.stale=True
    old.objects.hidden.add(key)
    try: old.confirm_current()
    except CommitUnavailable: pass
    else: raise AssertionError('hidden known successor authorized old state')
    old.objects.hidden.clear()
    old.confirm_current()
    assert old.load_document('test','user')=={'value':2}
    # A previously running instance cannot append over a later confirmed writer.
    another=open_store(old.objects)
    try: old.save_document('test','user','',{'value':1})
    except CommitUnavailable: pass
    else: raise AssertionError('old writer changed a newer version')
    old.confirm_current()
    assert old.load_document('test','user')=={'value':2}


def production_partitioning():
    """按日期/类型读取；旧状态及回滚写入补索引，只读零写，缓存有界可重取。"""
    store=open_store()
    send(store,'partition',[call('record_meal',MEAL),answer])
    keys=list(k for k in store.journal.state if k.startswith('archive:intake:'))
    before={k:store.journal.get(k)['revision'] for k in keys}
    revision=store.journal.revision
    payload=store.load_session('user','test')
    store.save_session('user','test',payload)
    assert store.journal.revision==revision
    store.save_document('delivery','user','x',{'sent':True})
    assert {k:store.journal.get(k)['revision'] for k in keys}==before
    # Make unrelated history payload unreadable; today's query must still work.
    store.write_intake({'message_id':'historical','user_id':'user','text':''},[{'date':'2020-01-01','matched_name':'旧项','grams':1,'nutrition':{'carbs':0,'protein':0,'fat':0,'kcal':1}}])
    for key in store.journal.state:
        if key.startswith('item:') and store.journal.get(key)['date']=='2020-01-01':
            ref=store.journal.index[key]
            store.objects.hidden.add(store.journal.prefix+'documents/'+ref+'.json')
    readonly=CosStore(store.objects,store.prefix,readonly=True)
    assert readonly.today_summary('user')['totals']['kcal']==270

    # Published old states have only hashed document keys, without a kind index.
    # Read-only reconstruction must work without writes; the first writer save
    # publishes coverage so unchanged cold readers skip unrelated payloads.
    from agent.storage.bitable_adapter import stable_uuid
    legacy = CosStore(Objects(), 'partition/legacy-documents/')
    settings = {'targets':{'kcal':1900}}
    old = {}
    for kind,payload in [('user_settings',settings),('agent_session',{'messages':['old history']}),
                         ('receipt_state',{'result':{'reply':'old result'}})]:
        old['document:'+stable_uuid(f'{kind}:user:')] = {'intent':kind,'user_id':'user','chat_id':'','payload':payload}
    legacy.journal.commit(old)
    readonly = CosStore(legacy.objects, legacy.prefix, readonly=True)
    count = legacy.objects.creates
    assert readonly.list_documents('user_settings')[0]['payload'] == settings
    assert readonly.list_documents('receipt_state')[0]['payload']['result']['reply'] == 'old result'
    assert legacy.objects.creates == count and not legacy.journal.get('document_index:ready')
    legacy.save_document('user_settings','user','',settings)
    assert isinstance(legacy.journal.get('document_index:ready'), str)
    kind_ref = legacy.journal.index['document_index:user_settings']
    coverage_ref = legacy.journal.index['document_index:ready']
    legacy.save_document('user_settings','user','',{'targets':{'kcal':2000}})
    assert legacy.journal.index['document_index:user_settings'] == kind_ref
    assert legacy.journal.index['document_index:ready'] == coverage_ref
    # The earlier indexed format used a boolean, which cannot prove coverage.
    legacy.journal.commit({'document_index:ready':True})
    readonly = CosStore(legacy.objects, legacy.prefix, readonly=True)
    count = legacy.objects.creates
    assert readonly.list_documents('user_settings')[0]['payload']['targets']['kcal'] == 2000
    assert legacy.objects.creates == count
    assert legacy.journal.get('document_index:ready') is True
    legacy.save_document('user_settings','user','',{'targets':{'kcal':2000}})
    assert legacy.journal.index['document_index:user_settings'] == kind_ref
    assert legacy.journal.index['document_index:ready'] == coverage_ref
    for key,doc in old.items():
        if doc['intent'] != 'user_settings':
            ref = legacy.journal.index[key]
            legacy.objects.hidden.add(legacy.journal.prefix+'documents/'+ref+'.json')
    readonly = CosStore(legacy.objects, legacy.prefix, readonly=True)
    count = legacy.objects.creates
    assert readonly.list_documents('user_settings')[0]['payload']['targets']['kcal'] == 2000
    assert legacy.objects.creates == count
    revision = legacy.journal.revision
    legacy.save_document('user_settings','another','',{'targets':{'kcal':1800}})
    assert legacy.journal.revision == revision+1
    assert len(legacy.list_documents('user_settings')) == 2
    readonly = CosStore(legacy.objects, legacy.prefix, readonly=True)
    count = legacy.objects.creates
    assert len(readonly.list_documents('user_settings')) == 2
    assert legacy.objects.creates == count

    # Simulate a downgrade: the old writer commits the same hashed document
    # identity, without touching kind indexes or the upgraded coverage marker.
    legacy.objects.hidden.clear()
    old_writer = CosStore(legacy.objects, legacy.prefix)
    indexed_refs = {key:ref for key,ref in old_writer.journal.index.items() if key.startswith('document_index:')}
    reminder_settings = {'feishu_chat_id':'rollback-chat','reminders':{
        'lunch':{'enabled':True,'time':'12:00','meal_type':'lunch'}}}
    rollback_key = 'document:'+stable_uuid('user_settings:rollback-user:')
    old_writer.journal.commit({rollback_key:{'intent':'user_settings','user_id':'rollback-user',
        'chat_id':'','payload':reminder_settings}})
    assert {key:ref for key,ref in old_writer.journal.index.items() if key.startswith('document_index:')} == indexed_refs
    readonly = CosStore(legacy.objects, legacy.prefix, readonly=True)
    count = legacy.objects.creates
    assert {doc['user_id'] for doc in readonly.list_documents('user_settings')} == {'user','another','rollback-user'}
    with patch.object(readonly, '_get', wraps=readonly._get) as reads:
        assert len(readonly.list_documents('user_settings')) == 3
        unrelated = {key for key,doc in old.items() if doc['intent'] != 'user_settings'}
        assert all(c.args[0] not in unrelated for c in reads.call_args_list)
    assert legacy.objects.creates == count
    upgraded = CosStore(legacy.objects, legacy.prefix)
    upgraded.save_document('user_settings','user','',{'targets':{'kcal':2000}})
    assert upgraded.journal.index['document_index:ready'] != indexed_refs['document_index:ready']
    assert upgraded.load_document('user_settings','rollback-user') == reminder_settings
    for key,doc in old.items():
        if doc['intent'] != 'user_settings':
            ref = upgraded.journal.index[key]
            upgraded.objects.hidden.add(upgraded.journal.prefix+'documents/'+ref+'.json')
    upgraded = CosStore(legacy.objects, legacy.prefix)
    from datetime import datetime, timezone
    from agent.storage.reminders import check_reminders
    sent = []
    def capture_reminder(chat_id, text, **kwargs):
        sent.append((chat_id, text, kwargs))
        return {'code':0}
    scheduled = datetime(2026,11,2,4,0,tzinfo=timezone.utc)
    assert check_reminders(upgraded,scheduled,send=capture_reminder)['sent'] == 1
    assert sent[0][0] == 'rollback-chat'
    assert check_reminders(upgraded,scheduled,send=capture_reminder)['sent'] == 0
    assert len(sent) == 1

    from agent.storage.cos_journal import encode
    with patch('agent.storage.cos_journal.CACHE_MAX_BYTES', 256), patch('agent.storage.cos_journal.CACHE_MAX_ENTRIES', 4):
        bounded = Journal(Objects(), 'partition/bounded-cache/')
        bounded.commit({'kept': {'text':'original'}})
        kept_ref = bounded.index['kept']
        for value in range(12):
            bounded.commit({'changing': {'number':value,'text':'x'*80}})
            assert bounded.cache_bytes <= 256 and len(bounded.cache) <= 4
        assert kept_ref not in bounded.cache
        assert bounded.get('kept') == {'text':'original'} and bounded.get('changing')['number'] == 11
        large = {'text':'x'*1024}
        bounded.commit({'large':large})
        assert bounded.index['large'] not in bounded.cache
        assert bounded.get('large') == large
        assert bounded.cache_bytes == sum(len(encode(value)) for value in bounded.cache.values()) <= 256


def production_parallel_transfers():
    """四路传输保留上下文；读失败不应用增量，写失败收束且不发布manifest。"""
    from contextlib import contextmanager
    import threading
    import time
    from agent.storage.cos_journal import encode
    from agent.storage.telemetry import EVENTS, MESSAGE_ID, PARENT, span
    from agent.storage.timeutil import PROCESSING_DEADLINE, WORK_DEADLINE, BUSINESS_IO

    main_thread = threading.get_ident()
    deadline = time.monotonic()+30
    expected_context = ('parallel-store', 'parallel-parent', deadline, deadline-5, True)

    class Transfers(Objects):
        def __init__(self):
            super().__init__()
            self.mode, self.targets = None, []
            self.lock = threading.Lock()

        def arm(self, mode, targets, inspect, *, fail=None, missing=None):
            self.mode, self.targets, self.inspect = mode, targets, inspect
            self.fail, self.missing = fail, missing
            self.active = self.peak = self.started = self.finished = 0
            self.seen, self.completed, self.manifests = [], [], []
            self.barrier = threading.Barrier(4, timeout=3)
            self.last_read = threading.Event()
            self.failed = threading.Event()

        @contextmanager
        def transfer(self, key):
            with self.lock:
                self.active += 1
                self.started += 1
                rank = self.started
                self.peak = max(self.peak, self.active)
                self.seen.append((MESSAGE_ID.get(), PARENT.get(), PROCESSING_DEADLINE.get(), WORK_DEADLINE.get(), BUSINESS_IO.get()))
                assert threading.get_ident() != main_thread
                self.inspect()
            try:
                with span('parallel_transport', key=key):
                    if rank <= 4:
                        self.barrier.wait()
                    if key == self.fail:
                        self.failed.set()
                        raise OSError('parallel transfer failure')
                    if self.mode == 'get' and key == self.targets[0]:
                        assert self.last_read.wait(3), 'later reads never completed'
                    if self.mode == 'create' and self.fail:
                        assert self.failed.wait(3), 'failed transfer never ran'
                    yield
                    if self.mode == 'get' and key == self.targets[-1]:
                        self.last_read.set()
            finally:
                with self.lock:
                    self.inspect()
                    self.active -= 1
                    self.finished += 1
                    self.completed.append(key)

        def get(self, key):
            if self.mode == 'get' and key in self.targets:
                with self.transfer(key):
                    return None if key == self.missing else super().get(key)
            return super().get(key)

        def create(self, key, body):
            if self.mode == 'create' and '/commits/' in key:
                assert threading.get_ident() == main_thread and self.active == 0
                assert self.finished == len(self.targets)
                self.manifests.append(key)
            if self.mode == 'create' and key in self.targets:
                with self.transfer(key):
                    return super().create(key, body)
            return super().create(key, body)

    variables = [(MESSAGE_ID, 'parallel-store'), (PARENT, 'parallel-parent'), (EVENTS, []),
                 (PROCESSING_DEADLINE, deadline), (WORK_DEADLINE, deadline-5), (BUSINESS_IO, True)]
    tokens = [(var, var.set(value)) for var,value in variables]
    try:
        # The first exact GET returns last; the latest logical value still wins.
        for fault in (None, 'missing', 'error'):
            objects = Transfers()
            writer = Journal(objects, 'parallel/read/')
            writer.commit({'value': 0})
            reader = Journal(objects, writer.prefix, readonly=True)
            before = (reader.revision, dict(reader.index), dict(reader.cache))
            for value in range(1,9): writer.commit({'value': value})
            targets = [reader.key(n) for n in range(before[0]+1, writer.revision+1)]
            def unchanged():
                assert (reader.revision, reader.index, reader.cache) == before
            objects.arm('get', targets, unchanged, fail=targets[0] if fault=='error' else None,
                        missing=targets[0] if fault=='missing' else None)
            creates = objects.creates
            try:
                reader.refresh(confirm=False)
            except (CommitUnavailable, OSError):
                assert fault is not None and reader.unavailable
                unchanged()
                try: reader.get('value')
                except CommitUnavailable: pass
                else: raise AssertionError('failed delta exposed cached state')
            else:
                assert fault is None and reader.get('value')==8
                assert objects.completed.index(targets[-1]) < objects.completed.index(targets[0])
            assert objects.creates == creates and objects.finished == len(targets) and objects.active == 0
            assert objects.peak == 4 and all(ctx == expected_context for ctx in objects.seen)

        # A failed upload is followed by successful siblings, all of which must
        # finish before the failure returns. No manifest or shared cache changes.
        for failed in (False, True):
            objects = Transfers()
            writer = Journal(objects, 'parallel/write/')
            writer.commit({'existing': 'confirmed'})
            before = (writer.revision, dict(writer.index), dict(writer.cache))
            changes = {'new:'+str(i): {'number':i} for i in range(8)}
            targets = [writer.prefix+'documents/'+hashlib.sha256(encode(value)).hexdigest()+'.json' for value in changes.values()]
            def unchanged():
                assert (writer.revision, writer.index, writer.cache) == before
            objects.arm('create', targets, unchanged, fail=targets[0] if failed else None)
            try:
                writer.commit(changes)
            except OSError:
                assert failed and writer.unavailable
                unchanged()
                assert not objects.manifests
                published = Journal(objects, writer.prefix, readonly=True)
                assert dict(published.state) == {'existing':'confirmed'}
            else:
                assert not failed and len(objects.manifests)==1
                assert all(writer.get(k)==value for k,value in changes.items())
            assert objects.finished == len(targets) and objects.active == 0 and objects.peak == 4
            assert all(ctx == expected_context for ctx in objects.seen)
        # The original manifest was stored but its response was lost. A failed
        # LIST cannot clear poison or expose the cached value from before it.
        objects = Objects()
        writer = Journal(objects, 'parallel/refresh-failure/')
        writer.commit({'value':0})
        readonly = Journal(objects, writer.prefix, readonly=True)
        assert readonly.get('value') == 0
        original_revision = writer.revision+1
        objects.lose_action = True
        try:
            writer.commit({'value':1, 'action:uncertain':{'result':1}})
        except OSError:
            pass
        else:
            raise AssertionError('manifest response loss was not injected')
        assert writer.key(original_revision) in objects.data and writer.unavailable
        creates = objects.creates
        with patch.object(objects, 'list', side_effect=OSError('listing temporarily unavailable')):
            for journal in (writer, readonly):
                try: journal.refresh(confirm=not journal.readonly)
                except OSError: pass
                else: raise AssertionError('LIST failure did not propagate')
                assert journal.unavailable
                try: journal.get('value')
                except CommitUnavailable: pass
                else: raise AssertionError('failed refresh exposed the old cached value')
        assert objects.creates == creates
        readonly.refresh(confirm=False)
        assert readonly.get('value') == 1 and objects.creates == creates
        writer.refresh()
        assert writer.get('value') == 1
        observed = [event for event in EVENTS.get() if event['kind']=='parallel_transport']
        assert observed and all(event['message_id']=='parallel-store' and event['parent']=='parallel-parent' for event in observed)
    finally:
        for var,token in reversed(tokens): var.reset(token)


def production_food_snapshot():
    """125份发布来源一次GET；旧数组不覆盖单项修改/新增，损坏内容拒绝读取。"""
    class ReadObjects(Objects):
        def __init__(self):
            super().__init__()
            self.reads = []
        def get(self, key):
            self.reads.append(key)
            return super().get(key)

    objects = ReadObjects()
    writer = CosStore(objects, 'parallel/food-snapshot/')
    foods = [{**deepcopy(A), 'food_id':'source-'+str(i), 'name':'来源食物'+str(i),
              'record_id':'original-'+str(i)} for i in range(125)]
    writer.import_baseline({'baseline_id':'source-125', 'foods':foods})
    snapshot_ref = writer.journal.index['source:foods']
    snapshot_key = writer.journal.prefix+'documents/'+snapshot_ref+'.json'
    readonly = CosStore(objects, writer.prefix, readonly=True)
    objects.reads.clear()
    creates = objects.creates
    loaded = readonly.load_foods()
    assert {f['food_id']:f for f in loaded} == {f['food_id']:f for f in foods}
    assert objects.reads == [snapshot_key] and objects.creates == creates

    updated = {**foods[0], 'kcal_per_100g':999}
    added = {**deepcopy(A), 'food_id':'source-new', 'name':'新来源食物'}
    writer.cache_food(updated)
    writer.cache_food(added)
    assert writer.journal.index['source:foods'] == snapshot_ref
    readonly.confirm_current()
    objects.reads.clear()
    creates = objects.creates
    current = {f['food_id']:f for f in readonly.load_foods()}
    changed_refs = {writer.journal.index['food:'+f['food_id']] for f in (updated, added)}
    assert current[updated['food_id']] == updated and current[added['food_id']] == added and len(current)==126
    assert len(objects.reads)==2 and {key.rsplit('/',1)[1][:-5] for key in objects.reads}==changed_refs
    assert objects.creates == creates

    # A new instance needs the original source array plus just the two current
    # changed/new documents, rather than reloading every food individually.
    fresh = CosStore(objects, writer.prefix, readonly=True)
    objects.reads.clear()
    assert {f['food_id']:f for f in fresh.load_foods()} == current
    assert len(objects.reads)==3 and snapshot_key in objects.reads and objects.creates==creates
    original = objects.data[snapshot_key]
    objects.data[snapshot_key] = b'[]'
    try:
        corrupted = CosStore(objects, writer.prefix, readonly=True)
        try: corrupted.load_foods()
        except CommitUnavailable: pass
        else: raise AssertionError('corrupted source snapshot primed food cache')
        assert not corrupted.journal.cache and objects.creates==creates
    finally:
        objects.data[snapshot_key] = original


CHECKS={'PS01':production_business,'PS02':production_recovery,'PS03':production_partitioning,
        'PS04':production_parallel_transfers,'PS05':production_food_snapshot}
