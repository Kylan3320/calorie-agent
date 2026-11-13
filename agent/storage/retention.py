"""Deterministic retention classification and read-only collection preflight.

No deletion and no model calls. The next generation substitutes compact terminal
credentials. Unknown kinds stay and are explicitly reported.
"""
from copy import deepcopy
import time
from .cos_journal import encode
from .cos_artifacts import thaw

AUDIT_SECONDS = 30 * 86400
LONG_KINDS = frozenset({
    'user_settings', 'agent_session', 'dialogue_turn', 'conversation_context',
    'memory_state', 'memory_index', 'memory_object', 'memory_review', 'memory_work',
    'memory_active', 'memory_backfill', 'summary_request', 'summary_active',
    'diet_goal', 'diet_goal_owner', 'diet_goal_active', 'diet_goal_plan', 'diet_goal_event',
    'legacy_message', 'ingress_cursor', 'reminder_receipt', 'reminder_delivery_active', 'task', 'task_owner',
    'task_active', 'worker_active', 'task_artifact', 'task_source', 'task_input', 'task_router_execution', 'worker_attempt', 'task_event',
})
INDEX_PREFIXES = ('document_index:', 'catalog:', 'user_catalog:', 'active_pending:', 'collection:', 'session:')


def terminal(value):
    if value.get('work_status') in {'needs_attention', 'unknown', 'pending', 'running'}:
        return False
    if value.get('status') not in {'completed', 'complete', 'terminated', 'failed', 'cancelled', 'rejected', 'consumed'}:
        return False
    delivery = value.get('delivery', {})
    if delivery and (delivery.get('status') in {'pending', 'unknown'} or delivery.get('presented') is False):
        return False
    result = value.get('result', {})
    if result.get('status') == 'error' and not result.get('terminal'):
        return False
    return True


def _terminal_time(value):
    delivery = value.get('delivery', {})
    return max((stamp for stamp in (value.get('completed_at'), value.get('ended_at'), value.get('finished_at'),
                                    delivery.get('presented_at'), delivery.get('sent_at'), delivery.get('delivered_at'))
                if stamp is not None), default=None)


def compact_result(result):
    return {**{k:deepcopy(result[k]) for k in ('reply', 'status', 'terminal', 'business_status',
        'presented_candidates', 'session_id', 'goal_candidate', 'artifact_ref') if k in result},
        'tool_events': [], 'tool_results': [], 'native_calls': [], 'model_requests': [],
        'audit_status':'unavailable', 'audit_reason':'retention_expired'}


def _references(value, aliases):
    if isinstance(value, dict):
        for key, child in value.items():
            if key not in {'previous', 'history', 'supersedes', 'source_hash', 'receipt_id', 'binding', 'attempts', 'run_id', 'cycle_id', 'task_id'}:
                yield from _references(child, aliases)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child, aliases)
    elif isinstance(value, str) and value in aliases:
        yield from aliases[value]


def preflight(journals, now=None):
    now = time.time() if now is None else now
    entries, reasons, aliases, expired = {}, {}, {}, set()
    for domain, journal in journals.items():
        for key in journal.keys():
            value = thaw(journal, journal.get(key))
            identity = domain + '/' + key
            entries[identity] = value
            if key.startswith('pending:'):
                for name in ('pending_id', 'record_id'):
                    if value.get(name): aliases.setdefault(value[name], set()).add(identity)
            if key.startswith('action:'):
                aliases.setdefault(key.removeprefix('action:'), set()).add(identity)
            if key.startswith('document:'):
                payload = value.get('payload', {})
                for name in ('ref', 'id', 'receipt_id'):
                    if isinstance(payload.get(name), str): aliases.setdefault(payload[name], set()).add(identity)
    documents = {(v.get('intent'),v.get('user_id'),v.get('chat_id')):v.get('payload',{})
                 for k,v in entries.items() if k.startswith('message/document:')}
    message_receipts = {}
    for identity, receipt in entries.items():
        if identity.startswith('ingress/receipt:') and receipt.get('kind') == 'message':
            message = receipt['message']
            key = tuple(message[k] for k in ('message_id', 'user_id', 'chat_id'))
            state = documents.get(('receipt_state', 'system', receipt['receipt_id']), {})
            message_receipts.setdefault(key, []).append(state)
    def worker_terminal(user, wid):
        worker=documents.get(('worker_attempt',user,wid),{})
        parent=documents.get(('task',user,worker.get('task_id')), {})
        cycle=parent.get('cycle',{})
        if cycle.get('id')!=worker.get('cycle_id'):
            cycle=documents.get(('task_run_audit',user,worker.get('cycle_id')), {})
        closed=worker.get('status') in {'completed','rejected','failed','terminated'} and cycle.get('status') in {'completed','terminated','failed'}
        closed=closed and parent.get('delivery',{}).get('status') not in {'pending','unknown'}
        end=max(worker.get('completed_at',0),cycle.get('completed_at',0)) or None
        return worker, closed, end
    for identity, value in entries.items():
        domain, key = identity.split('/', 1)
        if key.startswith('document:'):
            kind, body = value['intent'], value['payload']
            if kind in {'worker_attempt','task_input'}:
                worker,candidate,ended_at=worker_terminal(value['user_id'],value['chat_id'])
                body={**body,'completed_at':ended_at}
                reason='worker_and_parent_terminal' if candidate else 'worker_or_parent_recovery'
            elif kind == 'task_router_execution':
                parent=next((v for (k,u,_),v in documents.items() if k=='task' and u==value['user_id'] and v.get('cycle',{}).get('id')==value['chat_id']),{})
                cycle=parent.get('cycle',{})
                candidate=cycle.get('status') in {'completed','terminated','failed'} and parent.get('delivery',{}).get('status') not in {'pending','unknown'}
                body={**body,'completed_at':cycle.get('completed_at')}
                reason='router_terminal' if candidate else 'router_recovery'
            elif kind == 'task_candidate_audit':
                candidate,reason=terminal(body),'terminal_rejected_candidate'
            elif kind == 'receipt_state':
                candidate, reason = terminal(body), 'terminal_receipt'
            elif kind in {'diet_goal_run', 'task_run_audit'}:
                candidate, reason = terminal(body), 'terminal_run'
            elif kind in LONG_KINDS:
                candidate, reason = False, 'fact_source_or_active_state'
            else:
                candidate, reason = False, 'unclassified:' + kind
        elif key.startswith('pending:') and value.get('intent') == 'message_execution':
            body = {**value, **value.get('payload', {})}
            # Completed execution alone does not prove delivery is terminal.
            result = body.get('result', {})
            candidate = value.get('status') == 'completed' and bool(result) and (result.get('status') != 'error' or result.get('terminal'))
            if any(e.get('prepared') and 'result' not in e and not e.get('terminated') for e in body.get('calls', [])):
                candidate = False
            matching = message_receipts.get(tuple(value.get(k) for k in ('source_message_id', 'user_id', 'chat_id')), [])
            if matching:
                candidate = candidate and all(terminal(receipt) for receipt in matching)
                body['completed_at'] = max((stamp for stamp in [_terminal_time(body), *map(_terminal_time, matching)]
                                            if stamp is not None), default=None)
            reason = 'terminal_execution'
        elif key.startswith('receipt:'):
            body, candidate, reason = value, False, 'ingress_identity'
        elif key.startswith('action:'):
            body, candidate, reason = value, False, 'action_identity'
        elif key.startswith(('diet-goal-compute:', 'summary:', 'experience:')):
            body,candidate,reason=value,False,'background_recovery'
            if key.startswith('diet-goal-compute:'):
                ident=key.split(':',1)[1]
                runs=[p for (kind,_,scope),p in documents.items() if kind=='diet_goal_run' and scope==ident]
                runs.extend(p.get('run',{}) for (kind,_,_),p in documents.items() if kind=='diet_goal' and p.get('run',{}).get('id')==ident and p.get('delivery',{}).get('status')!='pending')
                run=next((p for p in runs if terminal(p)),{})
                candidate=bool(run);body={**body,'completed_at':run.get('finished_at')}
            elif key.startswith('summary:'):
                ident=key.split(':',1)[1]
                candidate=any(kind=='summary_request' and p.get('task_id')==ident and not p.get('active') and p.get('status') in {'applied','failed'} for (kind,_,_),p in documents.items())
            else:
                batch=value.get('batch_id')
                work=documents.get(('memory_work',value.get('user_id'),''),{})
                pending=work.get('pending',{})
                # Packed collection entries are read by their stable logical key.
                if isinstance(pending,dict) and '$paged_map' in pending:
                    pending={k.split(pending['$paged_map'],1)[1]:v for k,v in entries.items() if k.startswith('message/'+pending['$paged_map'])}
                candidate=bool(batch and value.get('candidate') and not any(ref in pending for ref in value.get('materials',[])))
            if candidate:reason='background_terminal'
        else:
            body, candidate = value if isinstance(value, dict) else {}, False
            reason = 'index' if key.startswith(INDEX_PREFIXES) else 'business_or_unclassified'
        ended = _terminal_time(body)
        if candidate and ended is None and not body.get('retention_anchor') and not value.get('retention_anchor'):
            # Explicit first-upgrade anchor; never infer expiry from object age.
            body = {**body, 'retention_anchor':now}
            entries[identity] = {**value, 'payload':body} if key.startswith('document:') else {**value, 'retention_anchor':now}
            reason = 'legacy_terminal_anchor'
        anchor = ended or body.get('retention_anchor') or (value.get('retention_anchor') if isinstance(value,dict) else None)
        if candidate and anchor is not None and now - anchor >= AUDIT_SECONDS:
            expired.add(identity)
            reason = 'expired_terminal'
        reasons[identity] = reason
    # Active receipts/unknown delivery protect their execution and every fixed
    # action. Source/proposal dependencies protect audits even beyond 30 days.
    for identity, value in entries.items():
        if identity in expired:
            continue
        for dependency in _references(value, aliases):
            if dependency in expired:
                expired.remove(dependency)
                reasons[dependency] = 'referenced_by:' + identity
        if identity.startswith('message/document:') and value.get('intent') == 'receipt_state' and not terminal(value['payload']):
            rid = value['payload'].get('receipt_id')
            # Link via ingress message identity, including awaiting_presentation.
            receipts = [v for k,v in entries.items() if k.startswith('ingress/receipt:') and v.get('receipt_id') == rid]
            for receipt in receipts:
                mid = receipt['message']['message_id']
                for dep, execution in entries.items():
                    if dep.startswith('message/pending:') and execution.get('source_message_id') == mid:
                        expired.discard(dep); reasons[dep] = 'unfinished_delivery:' + str(rid)
    replacements = {}
    for identity in expired:
        value = entries[identity]
        if identity.startswith(('sync/diet-goal-compute:','sync/summary:','sync/experience:')):
            replacements[identity]={**{k:deepcopy(value[k]) for k in ('task_id','run_id','user_id','chat_id','base_id','batch_id','materials','status','attempts','retention_anchor','completed_at','finished_at') if k in value},'audit_status':'unavailable'}
        elif value.get('intent') == 'receipt_state':
            payload = value['payload']
            replacements[identity] = {**{k:v for k,v in value.items() if k != 'payload'}, 'payload':{
                **{k:deepcopy(payload[k]) for k in ('receipt_id', 'sequence', 'status', 'work_status', 'delivery', 'completed_at') if k in payload},
                'result':compact_result(payload.get('result', {})), 'audit_status':'unavailable'}}
        elif value.get('intent') in {'worker_attempt','task_input','task_candidate_audit','task_run_audit','diet_goal_run','task_router_execution'}:
            payload=value['payload']
            names=('id','task_id','cycle_id','attempt','revision','redaction_revision','input_digest','candidate_digest','artifact_ref','status','completed_at','finished_at','usage','diagnostic','generation','input_key','candidate_key','deadline_at')
            replacements[identity]={**{k:v for k,v in value.items() if k!='payload'},'payload':{
                **{k:deepcopy(payload[k]) for k in names if k in payload},'audit_status':'unavailable'}}
        elif value.get('intent') == 'message_execution':
            payload = value['payload']
            replacements[identity] = {**{k:v for k,v in value.items() if k != 'payload'}, 'payload':{
                'day':payload.get('day'), 'result':compact_result(payload.get('result', {})),
                'audit_status':'unavailable', 'calls':[], 'model_requests':[], 'messages':[]}}
    keep = {key:replacements.get(key, value) for key,value in entries.items()}
    # Action identities survive; full results are unnecessary only where a
    # compact terminal execution proves that action will never be resumed.
    for identity in expired:
        for call in entries[identity].get('payload', {}).get('calls', []):
            action = call.get('action_id')
            key = 'message/action:' + str(action)
            if action and key in keep and reasons.get(key) == 'action_identity':
                keep[key] = {'status':'expired', 'committed':True, 'action_id':action, 'audit_status':'unavailable'}
    summary = {'now':now, 'keep_count':len(keep), 'compact_count':len(replacements),
               'logical_bytes_before':sum(len(encode(v)) for v in entries.values()),
               'logical_bytes_after':sum(len(encode(v)) for v in keep.values()),
               'reasons':reasons, 'compacted':sorted(replacements),
               'unclassified':[k for k,v in reasons.items() if 'unclassified' in v]}
    return keep, summary
