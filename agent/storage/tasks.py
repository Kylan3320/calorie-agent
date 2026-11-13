"""General delegations reuse Store actions, controls, receipts and candidate fencing.

The serial message writer owns every function in this module. Worker execution
gets fixed snapshot/candidate keys and cannot write these documents.
"""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import time
import secrets
import hmac
from . import memory
from .bitable_adapter import stable_uuid
from .cos_journal import encode
from .task_control import authorized, stop, binding

LIMITS = {'decisions':12, 'tools':24, 'web':9}
CHILD_LIMIT = {'decisions':4, 'tools':8, 'web':3}
ATTEMPT_BINDING = ('id','attempt','task_id','revision','cycle_id','redaction_revision','generation')
TERMINAL_ATTEMPTS = {'completed','rejected','terminated','failed'}


def load(store,user,ident=''):
    ident = ident or store.load_document('task_owner',user).get('id','')
    return store.load_document('task',user,ident) if ident else {}


def sources(store,user,task):
    if 'sources' in task:return task['sources']  # pre-C draft compatibility
    return {ref:store.load_document('task_source',user,ref) for ref in task.get('source_refs',[])}


def keep_sources(store,user,task,values):
    refs=list(dict.fromkeys([*task.get('source_refs',[]),*values]))[-12:]
    for ref,item in values.items():store.save_document('task_source',user,ref,item)
    task['source_refs']=refs
    task.pop('sources',None)


def retain_acquired_source(rt,item):
    if rt.goal_run or not rt.preset.allow_write:
        return
    bound=rt.journal.get('task_binding') or getattr(rt,'task_binding',None)
    if not bound:
        return
    task=load(rt.store,rt.user_id,bound['task_id'])
    if task.get('cycle',{}).get('id') == bound['cycle_id']:
        keep_sources(rt.store,rt.user_id,task,{item['ref']:item})
        save(rt.store,rt.user_id,task)


def within_authorization(task,now):
    try:return now<datetime.fromisoformat(task['spec']['ends'].replace('Z','+00:00')).timestamp()
    except (KeyError,ValueError,TypeError):return False


def cycle_attempts(cycle):
    children = cycle.get('attempts',[])
    return [*children,cycle['router_attempt']] if cycle.get('router_attempt') else children


def close_worker_index(store,user,task,attempt,now):
    active=store.load_document('worker_active','system')
    if attempt.get('candidate_digest') or now>=attempt['deadline_at']:
        active.pop(attempt['id'],None)
        store.save_document('worker_active','system','',active)
    if attempt['cycle_id'] == task['cycle']['id']:
        advance_cycle(store,user,task,now)


def advance_cycle(store,user,task,now):
    cycle = task['cycle']
    if cycle['status'] not in {'queued','running','stop_requested'}:
        return
    if any(store.load_document('worker_attempt',user,i).get('status') not in TERMINAL_ATTEMPTS
           for i in cycle['attempts']):
        return
    if task['status'] != 'active' or cycle['status'] == 'stop_requested':
        router = cycle.get('router_attempt')
        if router and store.load_document('worker_attempt',user,router).get('status') not in TERMINAL_ATTEMPTS:
            return
        cycle.update(status='terminated',completed_at=now,router_pending=False)
    elif not cycle.get('router_attempt'):
        cycle['router_pending']=True


def memory_stamp(store,user,ref):
    item,reason=memory.get(store,user,ref)
    if reason!='ok' or not item:return None
    item=memory.project(store,user,item,store.today_string(),memory.state(store,user))
    if not item or item.get('status') not in {'active','usable','delivered'}:return None
    return hashlib.sha256(encode(item)).hexdigest()


def memory_source(item):
    if not item.get('text') or item.get('status') not in {'active','usable','delivered'}:return None
    if item.get('kind')=='setting' and item.get('section') in {'targets','daily_targets','portions','favorites'}:return None
    return {'ref':item['ref'],'text':item['text'],'title':'个人记忆','url':'','origin':'memory',
        'memory_ref':item['ref'],'memory_stamp':hashlib.sha256(encode(item)).hexdigest()}


def project_artifact(store,user,artifact):
    sources=artifact.get('sources',{})
    invalid={ref for ref,src in sources.items() if src.get('origin')=='memory' and memory_stamp(store,user,src.get('memory_ref',''))!=src.get('memory_stamp')}
    if not invalid:return artifact
    while derived := {ref for ref,src in sources.items() if ref not in invalid and invalid.intersection(src.get('input_refs', []))}:
        invalid.update(derived)
    return {**artifact,'items':[item for item in artifact.get('items',[]) if not any(e['ref'] in invalid for e in item.get('evidence',[]))],
        'sources':{ref:src for ref,src in sources.items() if ref not in invalid},'outcome':'partial',
        'unresolved':['部分个人来源已变化，相关结果需要重新核对。'],'next_step':'重新核对变化的个人来源'}


def save(store,user,task):
    store.save_document('task',user,task['id'],task)
    active = store.load_document('task_active','system')
    if task['status'] == 'ended': active.pop(user,None)
    else: active[user] = task['id']
    store.save_document('task_active','system','',active)


def _cycle(task,user,store,trigger,now):
    previous = task.get('cycle')
    if previous and previous['status'] in {'running','queued','stop_requested'}:
        return previous
    ident = 'cycle:' + stable_uuid(task['id']+':'+trigger)
    if previous:
        store.save_document('task_run_audit',user,previous['id'],previous)
    from ..config import load_config
    cfg=load_config()
    limits={'decisions':cfg.task_cycle_decisions,'tools':cfg.task_cycle_tools,'web':cfg.task_cycle_web}
    task['parts'] = {}
    task['cycle'] = {'id':ident,'trigger':trigger,'status':'queued','started_at':now,'deadline_at':now+cfg.task_cycle_seconds,
        'revision':task['revision'],'redaction_revision':memory.redaction_revision(store,user),
        'limits':limits,'reserved':dict(CHILD_LIMIT),'parent_used':{'decisions':0,'tools':0,'web':0},
        'parent_limit':dict(CHILD_LIMIT), 'attempts':[], 'router_pending':True}
    return task['cycle']


def status(task,store,user):
    value = {k:deepcopy(v) for k,v in task.items() if k not in {'sources','materials','authorization','history'}}
    cycle = value.get('cycle',{})
    value['next_step'] = task.get('next_step','')
    if value.get('status')=='ended' or not within_authorization(task,time.time()):
        value.update(status='ended',auto=False,notify=False)
    cycle['workers'] = [{k:attempt.get(k) for k in ('id','status','objective','claimed_at','completed_at','diagnostic')}
                        for ident in cycle.get('attempts',[]) if (attempt:=store.load_document('worker_attempt',user,ident))]
    if cycle.get('router_attempt'):
        router = store.load_document('worker_attempt',user,cycle['router_attempt'])
        cycle['router_status'] = router.get('status')
    return value


def manage(rt,p):
    if not authorized(rt.message,p.get('user_quote')):
        return {'status':'pending','data':{'reason':'current_authorization_required'}}
    op, user, store, now = p['operation'], rt.user_id, rt.store, time.time()
    task = load(store,user,p.get('task_id',''))
    prior_cycle=task.get('cycle',{}).get('id')
    if op == 'start':
        if load(store,user).get('status') in {'active','paused','preparing'}:
            return {'status':'pending','data':{'reason':'active_general_task_exists'}}
        spec = p.get('spec',{})
        if not all(spec.get(k) for k in ('objective','done_when','ends')):
            return {'status':'pending','data':{'reason':'objective_completion_and_end_required'}}
        end = datetime.fromisoformat(spec['ends'].replace('Z','+00:00'))
        if end.tzinfo is None or end.timestamp() <= now:
            return {'status':'pending','data':{'reason':'future_end_with_timezone_required'}}
        ident = 'task:' + stable_uuid(user+':'+rt.action_id)
        task = {'id':ident,'revision':1,'status':'active','auto':bool(spec.get('triggers')),
            'notify':False,'spec':spec,'created_at':now,'authorization':{'quote':p['user_quote'],'message_id':rt.message['message_id']},
            'source_refs':[], 'materials':rt.message['text'],
            'destination':rt.message['chat_id'] if rt.message.get('delivery_source') == 'feishu' and spec.get('notify_here') else None}
        if 'time' in spec.get('triggers',[]) and not spec.get('check_time'):
            return {'status':'pending','data':{'reason':'time_trigger_requires_check_time'}}
        task['notify'] = bool(task['destination'])
        keep_sources(store,user,task,rt.session.get('general_sources',{}))
        store.save_document('task_owner',user,'',{'id':ident})
        _cycle(task,user,store,rt.action_id,rt.message.get('received_at') or now)
    else:
        if not task or task['status'] == 'ended':
            return {'status':'pending','data':{'reason':'no_open_task'}}
        if p.get('revision') != task['revision']:
            return {'status':'pending','data':{'reason':'task_changed','task':status(task,store,user)}}
        if datetime.fromisoformat(task['spec']['ends'].replace('Z','+00:00')).timestamp() <= now:
            task['status'] = 'ended'; stop(task.get('cycle',{})); _cancel_children(store,user,task)
        elif op == 'revise':
            stop(task.get('cycle',{}))
            _cancel_children(store,user,task)
            task['revision'] += 1
            task['spec'].update(p.get('spec',{}))
            keep_sources(store,user,task,rt.session.get('general_sources',{}))
            task['materials'] += '\n' + rt.message['text']
            task['cycle']['status'] = 'terminated'
            _cycle(task,user,store,rt.action_id,now)
        elif op in {'stop_run','pause','end'}:
            stop(task.get('cycle',{})); _cancel_children(store,user,task)
            if op == 'pause': task['status'] = 'paused'
            if op == 'end': task['status'] = 'ended'
            advance_cycle(store,user,task,now)
        elif op == 'stop_worker':
            if p.get('worker_id') not in task.get('cycle',{}).get('attempts',[]):
                return {'status':'pending','data':{'reason':'worker_not_in_current_cycle'}}
            attempt = store.load_document('worker_attempt',user,p['worker_id'])
            stop(attempt); store.save_document('worker_attempt',user,attempt['id'],attempt)
            advance_cycle(store,user,task,now)
        elif op == 'resume':
            task['status'] = 'active'
            task['cycle']['status'] = 'terminated'
            _cycle(task,user,store,rt.action_id,now)
        elif op == 'request_run':
            if task['status'] != 'active':
                return {'status':'pending','data':{'reason':'task_paused'}}
            _cycle(task,user,store,rt.action_id,now)
        elif op == 'set_auto': task['auto'] = p['enabled']
        elif op == 'set_notifications':
            if p['enabled'] and not task.get('destination'):
                return {'status':'pending','data':{'reason':'authorized_destination_required'}}
            task['notify'] = p['enabled']
    if op=='start' or task.get('cycle',{}).get('id')!=prior_cycle:
        bind_runtime(rt,task)
    save(store,user,task)
    return {'status':'ok','data':{'task':status(task,store,user),'operation':op}}


def _cancel_children(store,user,task):
    for ident in cycle_attempts(task.get('cycle',{})):
        attempt = store.load_document('worker_attempt',user,ident)
        stop(attempt)
        store.save_document('worker_attempt',user,ident,attempt)


def query(rt,p):
    task = load(rt.store,rt.user_id,p.get('task_id',''))
    if not task:
        return {'status':'pending','data':{'reason':'task_not_found'}}
    result = {'task':status(task,rt.store,rt.user_id)}
    if p.get('operation') == 'read_artifact':
        if not authorized(rt.message,p.get('view_quote')):
            return {'status':'pending','data':{'reason':'artifact_view_not_authorized'}}
        artifact = rt.store.load_document('task_artifact',rt.user_id,p.get('artifact_ref') or task.get('latest',''))
        artifact=project_artifact(rt.store,rt.user_id,artifact)
        if not p.get('artifact_ref') and task.get('artifact_revision')!=task['revision']:
            return {'status':'pending','data':{'reason':'revised_result_not_ready','task':result['task']}}
        if not artifact or artifact.get('task_id') != task['id']:
            return {'status':'pending','data':{'reason':'artifact_not_found','task':result['task']}}
        result['artifact'] = {**artifact,'task_id':task['id']}
        if artifact: result['artifact']['reply'] = render(artifact)
    return {'status':'ok','data':result}


def control_receipt(data):
    from .diet_goals import control_receipt as goal_receipt
    task = data['task']
    cycle = task.get('cycle', {})
    operation = data['operation']
    if operation == 'stop_worker':
        return '已提交停止该子任务的请求。其他工作按当前委托继续。'
    if operation in {'start','revise'}:
        action = '已建立' if operation == 'start' else '已更新'
        return action + '研究委托。' + task['spec']['objective'].rstrip('。') + '。已安排处理，可稍后查看进度。'
    return goal_receipt({'operation':operation,'goal':{**task,'run':cycle,'pending':cycle.get('status')=='queued'}})


def delegate(rt,p):
    store,user = rt.store,rt.user_id
    task = load(store,user,p['task_id'])
    cycle = task.get('cycle',{})
    if (task.get('status') != 'active' or task.get('revision') != p['revision'] or
            cycle.get('status') not in {'queued','running'} or cycle.get('router_attempt')
            or time.time() >= cycle.get('deadline_at',0)):
        return {'status':'pending','data':{'reason':'task_cycle_unavailable'}}
    if len(rt.journal['calls']) > CHILD_LIMIT['tools'] or rt.journal['model_calls'] > CHILD_LIMIT['decisions'] or rt.journal.get('general_web_requests',0)>CHILD_LIMIT['web']:
        return {'status':'pending','data':{'reason':'parent_budget_exhausted'}}
    jobs = p['jobs']
    if len(cycle['attempts']) + len(jobs) > 2:
        return {'status':'pending','data':{'reason':'two_workers_per_cycle'}}
    if any(cycle['reserved'][key]+len(jobs)*CHILD_LIMIT[key]>cycle.get('limits',LIMITS)[key] for key in CHILD_LIMIT):
        return {'status':'pending','data':{'reason':'shared_cycle_reservation_exhausted'}}
    known = {**sources(store,user,task), **rt.session.get('general_sources',{})}
    from .deepseek_web import public_url
    for job in jobs:
        if any(ref not in known for ref in job.get('source_refs',[])):
            return {'status':'pending','data':{'reason':'unknown_input_source'}}
        if any(not public_url(url) or url not in task['materials'] for url in job.get('urls',[])):
            return {'status':'pending','data':{'reason':'unknown_input_url'}}
    if not bind_runtime(rt,task):
        return {'status':'pending','data':{'reason':'shared_parent_budget_exhausted'}}
    # Validate every snapshot before staging any child document.
    planned=[]
    for number,job in enumerate(jobs):
        ident = 'worker:' + stable_uuid(rt.action_id+':'+str(number))
        child = new_attempt(store,task,ident,job,CHILD_LIMIT,min(cycle['deadline_at'],time.time()+90))
        snapshot = attempt_snapshot(child,{ref:known[ref] for ref in job.get('source_refs',[])},urls=job.get('urls',[]))
        for ref in child['memory_refs']:
            item, reason = memory.get(store,user,ref)
            projected = memory.project(store,user,item,rt.day,memory.state(store,user)) if item else None
            if reason != 'ok' or not projected or projected.get('status') not in {'active','usable','delivered'}:
                return {'status':'pending','data':{'reason':'memory_not_available'}}
            snapshot['memory'][ref] = projected
        child['memory_bindings']={ref:hashlib.sha256(encode(item)).hexdigest() for ref,item in snapshot['memory'].items()}
        if not seal_attempt(child,snapshot):
            return {'status':'pending','data':{'reason':'input_snapshot_too_large','max_bytes':64000}}
        planned.append((child,snapshot))
    # Fixed identities and all reservations commit together before handoff.
    active=store.load_document('worker_active','system')
    for child,snapshot in planned:
        ident=child['id']
        store.save_document('task_input',user,ident,snapshot)
        store.save_document('worker_attempt',user,ident,child)
        cycle['attempts'].append(ident)
        active[ident]={'user_id':user,'task_id':task['id']}
    store.save_document('worker_active','system','',active)
    cycle['reserved'] = {k:cycle['reserved'][k]+len(jobs)*CHILD_LIMIT[k] for k in CHILD_LIMIT}
    cycle.update(status='running',router_pending=False)
    keep_sources(store,user,task,known)
    save(store,user,task)
    return {'status':'ok','data':{'workers':cycle['attempts'],'deadline_at':cycle['deadline_at']}}


def new_attempt(store,task,ident,spec,limits,deadline,*,role='worker'):
    return {**binding(task,task['cycle']),'id':ident,'attempt':1,'role':role,'status':'queued',
        'objective':spec['objective'],'done_when':spec['done_when'],'limits':dict(limits),'deadline_at':deadline,
        'input_key':'workers/'+ident+'/input.json','candidate_key':'workers/'+ident+'/candidate.json',
        'generation':getattr(store,'prefix','fake/'),'memory_refs':spec.get('memory_refs',[]),'created_at':time.time()}


def attempt_snapshot(attempt,sources,**fields):
    return {'schema':'calorie.worker-input.v1','role':attempt['role'],
        'binding':{k:attempt[k] for k in ATTEMPT_BINDING},
        **{k:attempt[k] for k in ('objective','done_when','limits','deadline_at')},
        'sources':sources,'memory':{},**fields}


def seal_attempt(attempt,snapshot):
    capability = secrets.token_urlsafe(32)
    snapshot['runtime_control_token'] = capability
    raw = encode(snapshot)
    if len(raw)>64000:
        return False
    attempt['control_token_hash'] = hashlib.sha256(capability.encode()).hexdigest()
    attempt['input_digest'] = hashlib.sha256(raw).hexdigest()
    return True


def valid(store,user,task,attempt,now,*,received_at=None):
    cycle = task.get('cycle',{})
    if received_at is None:
        received_at = now
    return bool(task.get('status') == 'active' and within_authorization(task,now) and cycle.get('status') in {'running','queued'}
        and attempt.get('status') in {'queued','running'} and attempt.get('revision') == task['revision']
        and attempt.get('task_id') == task['id'] and attempt.get('cycle_id') == cycle['id']
        and received_at < min(cycle['deadline_at'],attempt['deadline_at'])
        and attempt.get('redaction_revision') == memory.redaction_revision(store,user)
        and all(memory_stamp(store,user,ref)==attempt.get('memory_bindings',{}).get(ref) for ref in attempt.get('memory_refs',[])))


def control(store,event,now=None):
    now = time.time() if now is None else now
    user = event['user_id']
    task, attempt = load(store,user,event['task_id']),store.load_document('worker_attempt',user,event['worker_id'])
    capability = hashlib.sha256(event.get('control_token','').encode()).hexdigest()
    permitted = hmac.compare_digest(capability,attempt.get('control_token_hash',''))
    if not permitted:return {'allowed':False,'status':'denied'}
    return {'allowed':bool(valid(store,user,task,attempt,now) and attempt.get('instance_id') == event.get('instance_id')),
            'status':attempt.get('status','missing'),'deadline_at':attempt.get('deadline_at')}


def _publish(store,event,now=None):
    from ..tools.general import validate_claims
    now = time.time() if now is None else now
    user, ident = event['user_id'],event['worker_id']
    task, attempt = load(store,user,event['task_id']),store.load_document('worker_attempt',user,ident)
    digest = hashlib.sha256(encode(event['candidate'])).hexdigest()
    if attempt.get('candidate_digest'):
        return {'status':'duplicate' if attempt['candidate_digest'] == digest else 'digest_conflict'}
    candidate = event['candidate']
    permitted = valid(store,user,task,attempt,now,received_at=event.get('candidate_received_at'))
    expected = {k:attempt[k] for k in ATTEMPT_BINDING}
    if (not permitted or candidate.get('schema')!='calorie.task-candidate.v1' or candidate.get('binding') != expected
            or attempt.get('dispatch_id') and (not attempt.get('instance_id') or candidate.get('instance_id') != attempt['instance_id'])):
        store.save_document('task_candidate_audit',user,digest,{'candidate':candidate,'digest':digest,'completed_at':now,'status':'rejected'})
        attempt.update(status='rejected',candidate_digest=digest,diagnostic='stale_or_cancelled',completed_at=now)
    else:
        sources = candidate.get('sources',{})
        try:
            validate_claims(candidate,sources)
            usage = candidate['usage']
            if any(type(usage.get(k)) is not int or not 0 <= usage[k] <= attempt['limits'][k] for k in CHILD_LIMIT):
                raise ValueError('runtime usage out of reserved bounds')
        except (AttributeError, KeyError, TypeError, ValueError):
            attempt.update(status='rejected',candidate_digest=digest,diagnostic='candidate_contract_invalid',completed_at=now)
        else:
            outcome = candidate['outcome']
            attempt.update(status='failed' if outcome == 'failed' else 'completed',outcome=outcome,
                           candidate_digest=digest,usage=usage,completed_at=now)
            if attempt.get('role') == 'router':
                task['cycle']['router_used'] = usage
                task['cycle']['parent_used'] = parent_usage(task['cycle'])
                artifact = {k:candidate[k] for k in ('outcome','items','unresolved','sources','next_step') if k in candidate}
                result = _apply_result(store,user,task['id'],attempt['cycle_id'],artifact,now,task=task)
                if result['status'] == 'obsolete_sources':
                    attempt.update(status='rejected',diagnostic='obsolete_sources')
                elif result.get('artifact_ref'):
                    attempt['artifact_ref'] = result['artifact_ref']
            else:
                ref = 'artifact:' + digest[:24]
                store.save_document('task_artifact',user,ref,{**candidate,'ref':ref,'task_id':task['id'],'published_at':now})
                attempt['artifact_ref'] = ref
                task.setdefault('parts',{})[ident] = ref
    store.save_document('worker_attempt',user,ident,attempt)
    close_worker_index(store,user,task,attempt,now)
    save(store,user,task)
    return {'status':attempt['status']}


def submit(rt,p):
    from ..tools.general import validate_claims
    known=rt.session.setdefault('general_sources',{})
    for call in rt.journal['calls']:
        result=call.get('result',{})
        item=result.get('data',{}).get('item') if call.get('tool')=='query_memory' and result.get('status')=='ok' else None
        if item and (src:=memory_source(item)):known[src['ref']]=src
    try:validate_claims(p,known)
    except (ValueError,KeyError,TypeError) as error:
        return {'status':'pending','data':{'reason':'candidate_evidence_invalid','detail':str(error)[:160]}}
    used = {e['ref'] for item in p.get('items',[]) for e in item['evidence']}
    pending = list(used)
    while pending:
        for ref in known[pending.pop()].get('input_refs', []):
            if ref in known and ref not in used:
                used.add(ref)
                pending.append(ref)
    rt.session['goal_candidate'] = {**p,'sources':{ref:deepcopy(known[ref]) for ref in sorted(used)}}
    return {'status':'ok','data':{'outcome':p['outcome']}}


def _apply_result(store,user,ident,cycle_id,candidate,now=None,*,task=None):
    from ..tools.general import validate_claims
    validate_claims(candidate,candidate.get('sources',{}))
    if any(src.get('origin')=='memory' and memory_stamp(store,user,src.get('memory_ref',''))!=src.get('memory_stamp') for src in candidate.get('sources',{}).values()):
        return {'status':'obsolete_sources'}
    now = time.time() if now is None else now
    task = task if task is not None else load(store,user,ident)
    cycle = task.get('cycle',{})
    if task.get('status') != 'active' or not within_authorization(task,now) or cycle.get('status') not in {'queued','running'} or cycle.get('id') != cycle_id or cycle.get('revision') != task['revision'] or cycle.get('redaction_revision') != memory.redaction_revision(store,user):
        return {'status':'obsolete'}
    ref = 'artifact:' + hashlib.sha256(encode([ident, candidate])).hexdigest()[:24]
    outcome = candidate['outcome']
    unchanged = task.get('latest') == ref
    if outcome != 'failed':
        if not unchanged:
            store.save_document('task_artifact',user,ref,{**candidate,'ref':ref,'task_id':ident,'published_at':now})
        task['latest'] = ref
        task['artifact_revision']=task['revision']
    cycle.update(status={'waiting':'waiting','failed':'failed'}.get(outcome,'completed'), outcome=outcome,
                 completed_at=now,router_pending=False)
    task['waiting_for'] = candidate.get('unresolved',[])
    task['next_step'] = candidate.get('next_step','')
    if task['notify'] and outcome != 'failed' and not unchanged:
        task['delivery'] = {'id':stable_uuid(cycle_id+':'+ref),'status':'pending','artifact_ref':ref,'revision':task['revision']}
    save(store,user,task)
    return {'status':'failed'} if outcome == 'failed' else {'status':'unchanged' if unchanged else 'published','artifact_ref':ref}


def render(artifact):
    lines = [item['text'] + '\n' + '\n'.join('「'+e['quote']+'」 '+(artifact['sources'][e['ref']].get('url') or artifact['sources'][e['ref']].get('title') or '用户资料／计算依据') for e in item['evidence']) for item in artifact.get('items',[])]
    if artifact.get('unresolved'): lines.append('未决：'+'；'.join(artifact['unresolved']))
    return '\n\n'.join(lines) or '本轮未取得可核实的成果。'


def parent_usage(cycle):
    foreground=cycle.get('foreground_usage',{})
    router=cycle.get('router_used',{})
    return {k:sum(v.get(k,0) for v in foreground.values())+router.get(k,0) for k in CHILD_LIMIT}


def bind_runtime(rt,task):
    cycle=task['cycle']
    usage={'decisions':rt.journal['model_calls'],'tools':len(rt.journal['calls']),'web':web_usage(rt)}
    foreground=deepcopy(cycle.get('foreground_usage',{}))
    mid=rt.message['message_id']
    old=foreground.get(mid,{})
    foreground[mid]={k:max(old.get(k,0),usage[k]) for k in CHILD_LIMIT}
    proposed={**cycle,'foreground_usage':foreground}
    total=parent_usage(proposed)
    if any(total[k]>cycle['parent_limit'][k] for k in CHILD_LIMIT):return False
    cycle.update(foreground_usage=foreground,parent_used=total)
    rt.task_binding={'task_id':task['id'],'cycle_id':cycle['id']}
    return True


def account(rt):
    bound=getattr(rt,'task_binding',None) or rt.journal.get('task_binding')
    if not bound or rt.goal_run:return
    task=load(rt.store,rt.user_id,bound['task_id'])
    if task.get('cycle',{}).get('id')!=bound['cycle_id']:return
    before=deepcopy(task['cycle'].get('parent_used'))
    if not bind_runtime(rt,task):raise ValueError('shared parent allocation exhausted')
    if task['cycle']['parent_used']!=before:save(rt.store,rt.user_id,task)
    rt.journal['task_binding']=bound


def register_event(store,user,kind,event_id,detail,now):
    task=load(store,user)
    if task.get('status')!='active' or not task.get('auto') or kind not in task['spec'].get('triggers',[]):return
    if event_id.startswith(('artifact:','worker:')):return
    key=task['id']+':'+kind+':'+event_id
    if store.load_document('task_event',user,key):return
    store.save_document('task_event',user,key,{'kind':kind,'event_id':event_id,'created_at':now})
    task.setdefault('pending',{})[kind]={'id':event_id,'detail':detail,'created_at':now}
    save(store,user,task)


def tick(store,config,now=None):
    from .diet_goals import _local
    now=time.time() if now is None else now
    changed=[]
    for user,ident in list(store.load_document('task_active','system').items()):
        task=load(store,user,ident)
        if datetime.fromisoformat(task['spec']['ends'].replace('Z','+00:00')).timestamp()<=now:
            task['status']='ended';stop(task.get('cycle',{}));_cancel_children(store,user,task)
            advance_cycle(store,user,task,now)
            save(store,user,task);changed.append(ident);continue
        if task['status']!='active':continue
        spec=task['spec'];local=_local(config,now)
        if task['auto'] and 'time' in spec.get('triggers',[]) and spec.get('check_time'):
            slot=local.replace(hour=int(spec['check_time'][:2]),minute=int(spec['check_time'][3:]),second=0,microsecond=0)
            if task['created_at']<=slot.timestamp()<=now and task.get('last_slot')!=slot.isoformat():
                task['last_slot']=slot.isoformat();task.setdefault('pending',{})['time']={'id':slot.isoformat()}
        if task.get('pending') and task.get('cycle',{}).get('status') not in {'queued','running','stop_requested'}:
            events=task.pop('pending')
            trigger=hashlib.sha256(encode(events)).hexdigest()
            _cycle(task,user,store,trigger,now);changed.append(ident)
        if ident in changed or task.get('last_slot')!=load(store,user,ident).get('last_slot'):save(store,user,task)
    return {'status':'checked','queued':changed}


def mark_delivered(store,user,ident,ref):
    task=load(store,user,ident)
    if task.get('latest')==ref:
        task['delivered']=ref
        save(store,user,task)


def deliver(store,user,ident,send,*,delivery_id=None):
    task=load(store,user,ident);delivery=task.get('delivery',{})
    if not delivery or delivery.get('status')!='pending' or delivery['id'] != delivery_id:return {'status':'no_delivery'}
    if (task['status']!='active' or not task['notify'] or not within_authorization(task,time.time())
            or delivery['artifact_ref']!=task.get('latest') or delivery.get('revision')!=task['revision']):
        delivery['status']='superseded';save(store,user,task);return {'status':'suppressed'}
    artifact=store.load_document('task_artifact',user,delivery['artifact_ref'])
    projected=project_artifact(store,user,artifact)
    if not artifact or projected is not artifact:
        delivery['status']='superseded';save(store,user,task);return {'status':'suppressed'}
    text=render(artifact)
    result=send(task['destination'],text,uuid_key=delivery['id'])
    if not result.get('data',{}).get('message_id'):
        raise RuntimeError('task notification delivery not confirmed')
    delivery.update(status='delivered',delivered_at=time.time(),send_result=result)
    task['delivered']=delivery['artifact_ref'];save(store,user,task)
    return {'status':'delivered'}


def publish(store,event,now=None):
    digest=hashlib.sha256(encode(event['candidate'])).hexdigest()
    key='worker-result:'+event['worker_id']+':'+digest
    if store.committed_action(key) is not None:return {'status':'duplicate'}
    return store.commit_action(key,lambda:_publish(store,event,now))


def apply_result(store,user,ident,cycle_id,candidate,now=None):
    digest=hashlib.sha256(encode(candidate)).hexdigest()
    key='task-result:'+cycle_id
    previous=store.committed_action(key)
    if previous is not None:
        return {'status':'duplicate' if previous.get('digest')==digest else 'digest_conflict'}
    def apply():
        result=_apply_result(store,user,ident,cycle_id,candidate,now)
        return {**result,'digest':digest}
    return store.commit_action(key,apply)


def web_usage(rt):
    from ..tools.portions import _web_state
    return rt.journal.get('general_web_requests',0)+(len(_web_state(rt)[0]) if hasattr(rt,'prepared') else 0)
