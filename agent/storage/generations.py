"""One immutable maintenance decision, shared by all three writer domains.

Control objects live outside generations. Pausing the message writer owns a
40-second watchdog inside its invocation; no later event is needed to cancel.
Deleting requires independent restart verification and server-denied old writes.
"""
from copy import deepcopy
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
from .cos_journal import Journal, encode, CommitUnavailable
from .retention import preflight

DOMAINS = ('ingress', 'message', 'sync')


class MaintenancePaused(CommitUnavailable):
    pass


class GenerationControl:
    def __init__(self, objects, prefix):
        clean=prefix.strip('/')
        if not clean or '..' in clean.split('/'):
            raise ValueError('explicit data prefix required')
        self.objects, self.root = objects, clean+'/'
        self.epoch, self._prefix, self._minimum, self._located = 0, self.root, {}, False

    def key(self, epoch, part):
        return self.root + f'control/epochs/{epoch:012d}/{part}.json'

    def read(self, key):
        raw = self.objects.get(key)
        return json.loads(raw) if raw is not None else None

    def create(self, key, value):
        raw = encode(value)
        try:
            made = self.objects.create(key, raw)
        except Exception:
            # Unknown creation must be resolved by exactly the original key.
            found = self.read(key)
            if found is None:
                raise
            return found
        found = value if made else self.read(key)
        if found is None:
            raise CommitUnavailable('maintenance decision unavailable')
        return found

    def location(self, *, allow_paused=False):
        if not self._located:
            # Epochs have a bounded seven control objects. Locate one bounded
            # final block, then follow exact successors to defeat a stale LIST.
            base=self.root+'control/epochs/'
            low,high=0,1
            marker=lambda block:base+f'{block*100:012d}/'
            while self.objects.list(base,marker=marker(high),limit=1):low,high=high,high*2
            while high-low>1:
                middle=(low+high)//2
                if self.objects.list(base,marker=marker(middle),limit=1):low=middle
                else:high=middle
            keys=self.objects.list(base,marker=marker(low) if low else '',limit=1000)
            epochs=[int(key[len(base):].split('/')[0]) for key in keys]
            if epochs:
                self.epoch=max(epochs)
                pause=self.read(self.key(self.epoch,'pause'))
                if pause is None:raise CommitUnavailable('known generation pause unavailable')
                self._prefix=pause['source']
                self._minimum=pause.get('versions',{})
            self._located=True
        epoch, prefix = self.epoch, self._prefix
        while True:
            pause = self.read(self.key(epoch, 'pause'))
            if pause is None:
                self.epoch, self._prefix = epoch,prefix
                return epoch, prefix
            decision = self.read(self.key(epoch, 'decision'))
            if decision is None:
                if allow_paused or self.read(self.key(epoch, 'started')) is None:
                    return epoch, prefix
                raise MaintenancePaused('generation paused; retry original identity')
            if decision.get('source') != prefix or decision.get('epoch') != epoch or decision.get('choice') not in {'enable', 'cancel'}:
                raise CommitUnavailable('invalid generation decision')
            if decision['choice']=='enable':
                verified=self.read(self.key(epoch,'verified'))
                if verified is None:raise CommitUnavailable('known generation root unavailable')
                self._minimum=verified['target_versions']
            prefix = decision['target'] if decision['choice'] == 'enable' else prefix
            epoch += 1
            self.epoch,self._prefix=epoch,prefix

    def guard(self, epoch, prefix):
        current_epoch, current = self.location()
        if current != prefix or current_epoch != epoch:
            raise MaintenancePaused('stale writer generation; reopen current view')

    def acknowledge(self, domain, epoch, revision):
        if domain not in DOMAINS:
            raise ValueError('unknown writer domain')
        pause = self.read(self.key(epoch, 'pause'))
        if pause is None:
            raise ValueError('pause not requested')
        return self.create(self.key(epoch, 'ack-' + domain), {'domain':domain, 'revision':revision, 'epoch':epoch})

    def decide(self, epoch, choice, now=None):
        now = time.time() if now is None else now
        pause = self.read(self.key(epoch, 'pause'))
        if not pause:
            raise ValueError('pause absent')
        started = self.read(self.key(epoch, 'started'))
        if not started:
            raise MaintenancePaused('message writer has not begun watchdog')
        if choice == 'enable':
            if now >= started['decision_deadline']:
                raise TimeoutError('enable deadline expired')
            if any(self.read(self.key(epoch, 'ack-' + role)) is None for role in DOMAINS):
                raise MaintenancePaused('all writer acknowledgements required')
            if not self.read(self.key(epoch, 'verified')):
                raise CommitUnavailable('target not independently verified')
        return self.create(self.key(epoch, 'decision'), {'epoch':epoch, 'source':pause['source'],
            'target':pause['target'], 'choice':choice, 'decided_at':now})

    def message_wait(self, epoch, revision, *, clock=time.time, wait=time.sleep):
        if self.read(self.key(epoch, 'pause')) is None:
            raise MaintenancePaused('pause not requested')
        started = self.create(self.key(epoch, 'started'), {'started_at':clock(),
            'decision_deadline':clock()+40, 'resume_deadline':clock()+60})
        self.acknowledge('message', epoch, revision)
        while clock() < started['resume_deadline']:
            from .timeutil import execution_deadline
            with execution_deadline(max(.001,started['resume_deadline']-clock())):
                decision = self.read(self.key(epoch, 'decision'))
                if decision is not None:
                    self.location()
                    return decision
                if clock() >= started['decision_deadline']:
                    self.decide(epoch, 'cancel', clock())
                    continue
            wait(min(.2, max(0, started['decision_deadline']-clock())))
        raise CommitUnavailable('decision unreadable; writers remain closed')

    def pause_event(self, event):
        epoch, prefix = self.location(allow_paused=True)
        if event.get('epoch') != epoch:
            return None
        state = self.read(self.key(epoch, 'pause'))
        if (not state or event.get('task') != state['task'] or state['source'] != prefix
                or self.read(self.key(epoch, 'decision')) is not None):
            return None
        return state

    def minimum(self,domain,*,confirmed=False):
        if not confirmed:self.location()
        return self._minimum.get(domain)

    def translate_ingress(self, source, revision):
        _, current = self.location()
        if source == current and revision is not None:
            return revision
        epoch,prefix = 0,self.root
        origin = source or self.root
        water = revision
        while True:
            pause = self.read(self.key(epoch,'pause'))
            if not pause:break
            decision = self.read(self.key(epoch,'decision'))
            if not decision:raise MaintenancePaused('generation paused')
            if decision['choice']=='enable':
                verified = self.read(self.key(epoch,'verified'))
                if origin==prefix:
                    if water is not None and water>verified['versions']['ingress']:
                        raise CommitUnavailable('old ingress watermark beyond frozen boundary')
                    origin,water=decision['target'],verified['target_versions']['ingress']
                prefix=decision['target']
            epoch+=1
        if origin!=prefix:raise CommitUnavailable('unknown ingress generation')
        return water


def _materialized(journal):
    from .cos_artifacts import thaw
    return {key:thaw(journal, journal.get(key)) for key in journal.keys()}


def upgrade_values(domain, values, *, target_ingress=None, source_ingress=None):
    """Explicit v1 conversion, used only by offline preparation/maintenance."""
    values = deepcopy(values)
    if domain == 'ingress':
        if target_ingress:
            for key, receipt in values.items():
                if key.startswith('receipt:') and receipt and 'session_position' not in receipt:
                    previous = target_ingress.get(key)
                    if previous:
                        receipt['session_position'] = previous['session_position']
        for key, value in list(values.items()):
            if key.startswith('session:') and isinstance(value, list):
                copied = target_ingress.get(key, {}).get('count', 0) if target_ingress else 0
                if copied > len(value):
                    raise CommitUnavailable('legacy session shortened after preparation')
                values[key] = {'count':len(value)}
                if copied == len(value):
                    continue
                for i in range(copied // 32 * 32, len(value), 32):
                    page = [{**entry, 'position':i+n} for n,entry in enumerate(value[i:i+32])]
                    values[key+f':page:{i//32:012d}'] = page
                    for entry in page:
                        if entry['position'] >= copied:
                            values['receipt:'+entry['receipt_id']]['session_position'] = entry['position']
    if domain == 'message':
        from .cos_store import CATALOG_KINDS
        for key, value in list(values.items()):
            if key.startswith('document:') and value is not None:
                kind = value['intent']
                if kind in CATALOG_KINDS:
                    values['catalog:' + kind + ':' + key] = {'key':key}
                if kind == 'ingress_cursor' and source_ingress:
                    entries = source_ingress.get('session:'+value['chat_id'], [])
                    if isinstance(entries, list):
                        sequence = value['payload'].get('sequence', 0)
                        value['payload']['position'] = bisect_right(entries, sequence, key=lambda entry: entry['sequence']) - 1
            elif key.startswith('pending:') and value is not None and value.get('status') == 'pending':
                values['active_pending:' + key.removeprefix('pending:')] = {'key':key}
            elif key.startswith(('document_index:', 'user_catalog:')) or (
                    key.startswith('catalog:') and key.split(':', 2)[1] not in CATALOG_KINDS):
                values.pop(key)
    return values


def _pack_legacy_documents(store, changes):
    from .paged_document import FIELDS, SEQUENCES, WHOLE, pack

    packed = {}
    for key, document in changes.items():
        if not key.startswith('document:') or not document:
            continue
        kind, payload = document['intent'], document['payload']
        if kind not in (FIELDS.keys() | SEQUENCES.keys() | WHOLE):
            continue
        packed[key] = {**document, 'payload':pack(store, kind, key, payload)}
    return packed


def _plain(value):
    from collections.abc import Mapping
    from .paged_document import DocumentSequence

    if isinstance(value, Mapping):
        return {key:_plain(item) for key,item in value.items()}
    if isinstance(value, (list, DocumentSequence)):
        return [_plain(item) for item in value]
    return value


class Maintenance:
    def __init__(self, objects, prefix):
        self.control = GenerationControl(objects, prefix)
        self.objects, self.root = objects, self.control.root

    def state_key(self, task):
        return self.root + 'control/tasks/' + task + '/'

    def prepare(self, task, *, now=None, compact=False):
        now = time.time() if now is None else now
        epoch, source = self.control.location()
        target = self.root + 'generations/' + task + '/'
        if '/' in task or not task or target == source:
            raise ValueError('one isolated maintenance identity required')
        registered=self.control.read(self.state_key(task)+'prepared.json')
        if registered is None and any(self.objects.list(target+role+'/',limit=1) for role in DOMAINS):
            raise ValueError('target contains objects outside this registered preparation')
        journals = {role:Journal(self.objects, source+role+'/', readonly=True) for role in DOMAINS}
        selected, report = preflight(journals, now)
        if not compact:
            selected = {role+'/'+key:value for role,j in journals.items() for key,value in _materialized(j).items()}
        versions = {role:j.revision for role,j in journals.items()}
        frozen = self.control.create(self.state_key(task)+'prepared.json', {'task':task, 'epoch':epoch,
            'source':source, 'target':target, 'versions':versions, 'compact':compact, 'report':report, 'prepared_at':now})
        if self.control.read(self.state_key(task)+'copied.json'):
            return frozen
        if frozen['versions'] != versions:
            versions = frozen['versions']
            for role,journal in journals.items():
                journal.revision, journal.index, journal.format = -1, {}, None
                journal._catchup(versions[role])
            selected, report = preflight(journals, frozen['prepared_at'])
            if not compact:
                selected = {role+'/'+key:value for role,j in journals.items() for key,value in _materialized(j).items()}
        now = frozen['prepared_at']
        from .cos_store import CosStore
        for role in DOMAINS:
            values = upgrade_values(role, {key.split('/',1)[1]:v for key,v in selected.items() if key.startswith(role+'/')},
                                    source_ingress=journals['ingress'])
            if role == 'sync':
                # Baseline carries unconfirmed discovery, inflight and mappings.
                for key in journals['message'].keys('archive:'):
                    change = journals['message'].get(key)
                    ack = values.get('item:'+key, {}).get('ack_revision', -1)
                    if change['revision'] > ack:
                        values['archive_pending:'+key] = change
                values['archive_scan'] = {'generation':target, 'revision':0}
                values['recovery_scan'] = {'revision':-1}
            dest = Journal(self.objects, target+role+'/')
            if role == 'message' and journals[role].format == 1:
                converted = CosStore(self.objects, target, controlled=False)
                converted._changes = {}
                values.update(_pack_legacy_documents(converted, values))
                values.update(converted._changes)
                dest = converted.journal
            dest.commit(values)
        # Fresh readers independently compare the entire prepared baseline
        # before any role is paused. Paged metadata upgrades happen here.
        for role in DOMAINS:
            fresh = Journal(self.objects, target+role+'/', readonly=True)
            for identity, expected in selected.items():
                if not identity.startswith(role+'/'):
                    continue
                key = identity.split('/',1)[1]
                if role == 'message' and (key.startswith(('document_index:', 'catalog:', 'user_catalog:')) or key.startswith('document:')):
                    continue  # compared through the converted document contract below
                if role == 'ingress' and (key.startswith('session:') or key.startswith('receipt:')):
                    continue
                if key in {'archive_scan', 'recovery_scan'}:
                    continue
                if fresh.get(key) != expected:
                    raise CommitUnavailable('prepared baseline verification mismatch: ' + identity)
        from .paged_document import unpack
        source_store = CosStore(self.objects,source,readonly=True,controlled=False)
        source_store.journal = journals['message']
        fresh_store = CosStore(self.objects,target,readonly=True,controlled=False)
        for identity,expected in selected.items():
            if identity.startswith('message/document:'):
                kind,user,scope=expected['intent'],expected['user_id'],expected['chat_id']
                original=_plain(unpack(source_store,kind,expected['payload']))
                actual=_plain(fresh_store.load_document(kind,user,scope))
                # Explicit cursor conversion adds its stable session position.
                if kind=='ingress_cursor':actual.pop('position',None);original.pop('position',None)
                if kind=='conversation_context' and 'positions' not in original:actual.pop('positions',None)
                if actual!=original:raise CommitUnavailable('document baseline mismatch: '+identity)
        self.control.create(self.state_key(task)+'copied.json', {'versions':versions})
        return frozen

    def request_pause(self, task):
        state = self.control.read(self.state_key(task)+'prepared.json')
        if not state or not self.control.read(self.state_key(task)+'copied.json'):
            raise ValueError('prepare target first')
        self.control.create(self.control.key(state['epoch'],'pause'), state)
        return state

    def _finish(self, task, *, now=None):
        state = self.control.read(self.state_key(task)+'prepared.json')
        epoch = state['epoch']
        existing = self.control.read(self.control.key(epoch, 'decision'))
        if existing:
            return existing
        started = self.control.read(self.control.key(epoch,'started'))
        if not started:
            raise MaintenancePaused('message watchdog not started')
        now = time.time() if now is None else now
        if now >= started['decision_deadline']:
            return self.control.decide(epoch, 'cancel', now)
        acks = {role:self.control.read(self.control.key(epoch,'ack-'+role)) for role in DOMAINS}
        if not all(acks.values()):
            raise MaintenancePaused('writer not frozen')
        # Only post-preparation deltas are read and independently compared inside
        # the pause. No scan of the source library in the pause window.
        archive_delta = {}
        source_ingress = None
        for role in DOMAINS:
            source = Journal(self.objects, state['source']+role+'/', readonly=True)
            source.refresh(confirm=False, minimum_revision=acks[role]['revision'])
            if source.revision != acks[role]['revision']:
                raise CommitUnavailable('writer moved after acknowledgement')
            if role == 'ingress':
                source_ingress = source
            changes = {}
            for revision in range(state['versions'][role]+1, source.revision+1):
                commit = source._read(revision)
                for key, ref in commit['changes'].items():
                    from .cos_artifacts import thaw
                    changes[key] = thaw(source,source.get(key)) if ref else None
            if role=='message':
                archive_delta.update({k:v for k,v in changes.items() if k.startswith('archive:') and v is not None})
            dest = Journal(self.objects, state['target']+role+'/')
            changes = upgrade_values(role, changes, target_ingress=dest, source_ingress=source_ingress)
            legacy = {}
            if role == 'message' and source.format == 1:
                from .cos_store import CosStore
                converted = CosStore(self.objects,state['target'],controlled=False)
                converted._changes = {}
                converted_values = _pack_legacy_documents(converted, changes)
                legacy = {key:changes[key] for key in converted_values}
                changes.update(converted_values)
                changes.update(converted._changes)
                dest = converted.journal
            if changes:
                dest.commit(changes)
            fresh = Journal(self.objects, state['target']+role+'/', readonly=True)
            if any(fresh.get(key) != value for key,value in changes.items()):
                raise CommitUnavailable('target delta mismatch')
            if legacy:
                reader=CosStore(self.objects,state['target'],readonly=True,controlled=False)
                for document in legacy.values():
                    kind,user,scope=document['intent'],document['user_id'],document['chat_id']
                    expected=document['payload']
                    actual=_plain(reader.load_document(kind,user,scope))
                    if kind=='conversation_context' and 'positions' not in expected:actual.pop('positions',None)
                    if actual!=expected:raise CommitUnavailable('converted document delta mismatch: '+kind)
        target_heads = {role:Journal(self.objects,state['target']+role+'/',readonly=True).revision for role in DOMAINS}
        sync = Journal(self.objects,state['target']+'sync/')
        pending = {'archive_pending:'+key:value for key,value in archive_delta.items() if value['revision']>sync.get('item:'+key,{}).get('ack_revision',-1)}
        sync.commit({**pending,'archive_scan':{'generation':state['target'],'revision':target_heads['message']},
                     'recovery_scan':{'revision':-1}})
        self.control.create(self.control.key(epoch,'verified'), {'task':task, 'source':state['source'],'target':state['target'],
            'versions':{r:a['revision'] for r,a in acks.items()},'target_versions':target_heads})
        return self.control.decide(epoch, 'enable')

    def finish(self,task,*,now=None):
        from .timeutil import execution_deadline
        state=self.control.read(self.state_key(task)+'prepared.json')
        started=self.control.read(self.control.key(state['epoch'],'started')) if state else None
        if not started:return self._finish(task,now=now)
        existing=self.control.read(self.control.key(state['epoch'],'decision'))
        if existing:return existing
        remaining=started['decision_deadline']-time.time()
        if remaining<=0:return self.control.decide(state['epoch'],'cancel')
        with execution_deadline(remaining):
            return self._finish(task,now=now)

    def collection_plan(self, task):
        state = self.control.read(self.state_key(task)+'prepared.json')
        decision = self.control.read(self.control.key(state['epoch'],'decision'))
        if not decision or decision['choice'] != 'enable':
            raise MaintenancePaused('enabled decision required')
        # Explicit source-domain ranges; never control objects or new generations.
        import re
        found = sorted(key for role in DOMAINS for key in self.objects.list(state['source']+role+'/'))
        pattern = re.compile(r'(?:commits/[0-9]{12}|(?:documents|indexes)/[0-9a-f]{64})\.json$')
        keys = [key for key in found if pattern.fullmatch(key[len(state['source']):].split('/',1)[1])]
        unclassified = [key for key in found if key not in set(keys)]
        from .cos_store import CosStore
        target_store = CosStore(self.objects,state['target'],readonly=True,controlled=False)
        for doc in target_store.list_documents('worker_attempt'):
            attempt = doc['payload']
            if attempt.get('generation') != state['source']:
                continue
            if time.time() < attempt['deadline_at']:
                raise PermissionError('old Worker write grant is still valid')
            if attempt.get('status') not in {'completed','rejected','terminated','failed'}:
                raise PermissionError('old Worker candidate reception not closed')
            candidate_key = attempt['generation'] + attempt['candidate_key']
            raw = self.objects.get(candidate_key)
            if raw and hashlib.sha256(raw).hexdigest() != attempt.get('candidate_digest'):
                raise PermissionError('old candidate not reconciled into current generation')
            keys.extend(key for key in (attempt['generation']+attempt['input_key'],candidate_key) if self.objects.get(key) is not None)
        keys = sorted(set(keys))
        return {'task':task, 'source':state['source'], 'target':state['target'], 'keys':keys, 'unclassified_keys':unclassified,
                'digest':hashlib.sha256(encode(keys)).hexdigest(),
                'bytes':sum(len(self.objects.get(key) or b'') for key in keys)}

    def collect(self, task, *, restart_verified, denied_roles, delete=None, limit=100):
        if not restart_verified or set(denied_roles) != set(DOMAINS) or any(
                evidence.get('status') != 403 or evidence.get('source') != self.control.read(self.state_key(task)+'prepared.json')['source']
                for evidence in denied_roles.values()):
            raise PermissionError('independent restart and COS server-denied old-role PUTs required')
        manifest_key = self.state_key(task)+'collection.json'
        manifest = self.control.read(manifest_key)
        if manifest is None:
            manifest = self.control.create(manifest_key, self.collection_plan(task))
        # A previous interrupted deletion uses the original immutable manifest.
        completed = 0
        for index,key in enumerate(manifest['keys']):
            done = self.state_key(task)+f'deleted/{index:012d}.json'
            if self.control.read(done):
                continue
            (delete or self.objects.delete)(key)
            self.control.create(done, {'key':key})
            completed += 1
            if completed >= limit:
                break
        remaining = sum(self.control.read(self.state_key(task)+f'deleted/{i:012d}.json') is None for i in range(len(manifest['keys'])))
        return {'deleted':completed, 'remaining':remaining, 'manifest':self.state_key(task)+'collection.json', 'unclassified_keys':manifest.get('unclassified_keys',[])}


    def verify_restart(self,task):
        state=self.control.read(self.state_key(task)+'prepared.json')
        _,current=self.control.location()
        if current!=state['target']:raise ValueError('target is not the current generation')
        result={}
        for role in DOMAINS:
            journal=Journal(self.objects,current+role+'/',readonly=True)
            journal.refresh(confirm=False,minimum_revision=self.control.minimum(role))
            values=_materialized(journal)
            result[role]={'revision':journal.revision,'logical_objects':len(values),'sha256':hashlib.sha256(encode(values)).hexdigest()}
        return {'source':state['source'],'target':current,'fresh_readers':result}

    def denied_role_writes(self,task,files):
        from pathlib import Path
        from .cos_transport import CosObjects
        state=self.control.read(self.state_key(task)+'prepared.json')
        evidence={}
        for role in DOMAINS:
            settings=json.loads(Path(files[role]).read_text())
            # Authenticated bucket metadata also establishes that these old
            # credentials are still valid; an expired-token 403 isn't proof.
            old=CosObjects(bucket=settings['bucket'],region=settings['region'],credentials=(settings['secret_id'],settings['secret_key'],settings.get('token')))
            if old.bucket!=self.objects.bucket:raise ValueError('old role bucket differs')
            key=state['source']+role+'/retirement-denial-probe.json'
            try:old.create(key,encode({'probe':'retirement','task':task}))
            except Exception as exc:
                if getattr(exc,'get_status_code',lambda:None)()!=403 or getattr(exc,'get_error_code',lambda:None)()!='AccessDenied':raise
                evidence[role]={'status':403,'source':state['source'],'key':key,'transport':'real_cos','credentials_validated_by':'GetBucketVersioning'}
            else:raise PermissionError('old-role PUT was not rejected: '+key)
        return evidence

    def cleanup_cancelled(self,task,limit=100):
        state=self.control.read(self.state_key(task)+'prepared.json')
        decision=self.control.read(self.control.key(state['epoch'],'decision'))
        if not decision or decision['choice']!='cancel':raise PermissionError('cancel decision required')
        _,current=self.control.location()
        if current==state['target']:raise PermissionError('cannot delete current generation')
        import re
        found=sorted(key for role in DOMAINS for key in self.objects.list(state['target']+role+'/'))
        pattern=re.compile(r'(?:commits/[0-9]{12}|(?:documents|indexes)/[0-9a-f]{64})\.json$')
        keys=[key for key in found if pattern.fullmatch(key[len(state['target']):].split('/',1)[1])]
        unknown=[key for key in found if key not in set(keys)]
        manifest=self.control.create(self.state_key(task)+'cancelled-cleanup.json',{'keys':keys,'target':state['target'],'unclassified_keys':unknown})
        deleted=0
        for number,key in enumerate(manifest['keys']):
            done=self.state_key(task)+f'cancelled-deleted/{number:012d}.json'
            if self.control.read(done):continue
            self.objects.delete(key);self.control.create(done,{'key':key});deleted+=1
            if deleted>=limit:break
        remaining=sum(self.control.read(self.state_key(task)+f'cancelled-deleted/{n:012d}.json') is None for n in range(len(manifest['keys'])))
        return {'deleted':deleted,'remaining':remaining,'unclassified_keys':manifest.get('unclassified_keys',[])}


def maintenance_tick(pipeline, *, dispatch, now=None):
    """Resume a registered switch before archive work; deletion stays gated.

    New automatic tasks require an explicitly validated isolated configuration.
    Runtime roles need pre-provisioned generation access; this never edits IAM.
    """
    import os
    now=time.time() if now is None else now
    ctl=pipeline.control
    ledger=Journal(pipeline.objects,ctl.root+'control/maintenance/')
    active=ledger.get('active')
    retirement=ledger.get('retirement')
    local=datetime.fromtimestamp(now,timezone(timedelta(hours=8)))
    maint=Maintenance(pipeline.objects,ctl.root)
    if retirement:
        task=retirement['task']
        if retirement['choice']=='cancel':
            result=maint.cleanup_cancelled(task)
        else:
            files=os.getenv('CALORIE_RETIRED_ROLE_FILES')
            if not files:return {'status':'pending_permission_evidence','task':task}
            from pathlib import Path
            verified=maint.verify_restart(task)
            denial=maint.denied_role_writes(task,json.loads(Path(files).read_text()))
            result=maint.collect(task,restart_verified=bool(verified),denied_roles=denial)
        if result['remaining']==0 and not result.get('unclassified_keys'):
            ledger.commit({'retirement':None,'last_collection':{'task':task,**result}})
        return {'status':'retirement_resumed','task':task,**result}
    if not active:
        if os.getenv('CALORIE_MAINTENANCE_VALIDATED')!='isolated-v2':
            return {'status':'disabled_pending_isolated_validation'}
        if local.hour<3 or ledger.get('checked_day')==local.date().isoformat():
            return {'status':'idle'}
        epoch,prefix=ctl.location()
        journals={role:Journal(pipeline.objects,prefix+role+'/',readonly=True) for role in DOMAINS}
        _,report=preflight(journals,now)
        count=sum(j.revision+1 for j in journals.values())
        ledger.commit({'checked_day':local.date().isoformat()})
        if count<10000 and not report['compact_count']:
            return {'status':'no_candidate'}
        task='auto-'+local.strftime('%Y%m%d')+'-'+str(epoch)
        active={'task':task,'stage':'prepare'}
        ledger.commit({'active':active})
    task=active['task']
    if active['stage']=='prepare':
        maint.prepare(task,now=now,compact=True)
        active['stage']='pause';ledger.commit({'active':active})
    state=ctl.read(maint.state_key(task)+'prepared.json');epoch=state['epoch']
    decision=ctl.read(ctl.key(epoch,'decision'))
    if decision:
        ledger.commit({'active':None,'retirement':{'task':task,'choice':decision['choice']},'last':{'task':task,'decision':decision,'collection':'permission_and_restart_evidence_required'}})
        return {'status':decision['choice'],'task':task,'collection':'pending_permission_evidence'}
    maint.request_pause(task)
    pause_event={'type':'calorie_maintenance_pause','epoch':epoch,'task':task}
    dispatch(pause_event,target='message')
    # The message invocation now owns cancellation even if this maintenance
    # invocation dies. Ingress acknowledges only after that reliable start.
    until=time.monotonic()+2
    while not ctl.read(ctl.key(epoch,'started')) and time.monotonic()<until:
        time.sleep(.05)
    if not ctl.read(ctl.key(epoch,'started')):
        return {'status':'awaiting_message_watchdog','task':task}
    dispatch(pause_event,target='ingress')
    source=Journal(pipeline.objects,state['source']+'sync/')
    ctl.acknowledge('sync',epoch,source.revision)
    until=time.monotonic()+38
    while time.monotonic()<until:
        decision=ctl.read(ctl.key(epoch,'decision'))
        if decision:break
        try:
            decision=maint.finish(task)
            break
        except MaintenancePaused:
            time.sleep(.1)
    return {'status':decision['choice'] if decision else 'watchdog_owns_resume','task':task}


def ingress_pause(objects,prefix,event):
    ctl=GenerationControl(objects,prefix)
    pause=ctl.pause_event(event)
    if not pause or not ctl.read(ctl.key(pause['epoch'],'started')):
        return {'status':'not_started'}
    epoch=pause['epoch']
    journal=Journal(objects,pause['source']+'ingress/')
    ctl.acknowledge('ingress',epoch,journal.revision)
    return {'status':'paused','epoch':epoch}
