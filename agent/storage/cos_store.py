"""COS current facts and runtime state. No Bitable requests on this path."""
from copy import deepcopy
import hashlib
import re
from datetime import datetime, timedelta, timezone
import os
import time
from .state_store import StateStore
from .cos_journal import Journal, encode
from .cos_transport import CosObjects
from . import paged_document
from .bitable_adapter import (stable_uuid, message_key, intake_client_token,
    DEFAULT_DAILY_STANDARD, _is_valid_intake_status, normalize_food_key)
from .timeutil import today_string, round_number, BUSINESS_DATE


class CosStore(StateStore):
    def __init__(self, objects=None, prefix=None, offset_hours=8, readonly=False, controlled=True):
        self.objects = objects if objects is not None else CosObjects()
        self.prefix = (prefix or os.environ['COS_PREFIX']).strip('/') + '/'
        if not self.prefix.strip('/') or '..' in self.prefix.split('/'):
            raise ValueError('explicit isolated COS prefix required')
        self.offset_hours, self.readonly = offset_hours, readonly
        self.control, guard = None, None
        if controlled:
            from .generations import GenerationControl
            self.control = GenerationControl(self.objects, self.prefix)
            epoch, self.prefix = self.control.location()
            guard = lambda: self.control.guard(epoch, self.prefix)
        self.journal = Journal(self.objects, self.prefix+'message/', readonly=readonly, guard=guard)
        if self.control and (minimum:=self.control.minimum('message')) is not None:
            self.journal.refresh(confirm=False,minimum_revision=minimum)
        self._changes = None
        self._legacy_document_index = None

    def kind(self): return 'cos'
    def today_string(self): return BUSINESS_DATE.get() or today_string(self.offset_hours)
    def confirm_current(self):
        if self.control:
            self.journal.guard()
        return self.journal.refresh(confirm=not self.readonly)

    def _get(self, key, default=None):
        if self._changes is not None and key in self._changes:
            return deepcopy(self._changes[key])
        value = self.journal.get(key, default)
        if key.startswith(('pending:', 'action:', 'document:')):
            from .cos_artifacts import thaw
            return thaw(self.journal, value)
        return value

    def _save(self, changes):
        if self.readonly: raise PermissionError('readonly COS Store')
        if self._changes is not None: self._changes.update(deepcopy(changes))
        else: self._commit(changes)

    def _commit(self, changes):
        from .cos_artifacts import freeze
        packed = {key: freeze(self.journal, value) if value is not None and
                  key.startswith(('pending:', 'action:', 'document:'))
                  else value for key, value in changes.items()}
        return self.journal.commit(packed)

    def _keys(self, prefix):
        keys=set(self.journal.keys(prefix))
        for key,value in (self._changes or {}).items():
            if key.startswith(prefix):
                if value is None:keys.discard(key)
                else:keys.add(key)
        return sorted(keys)

    def commit_action(self, action_id, callback):
        key = 'action:'+action_id
        saved = self._get(key)
        if saved is not None: return saved
        if self._changes is not None: raise RuntimeError('nested business action')
        self._changes = {}
        try:
            result = callback()
            self._changes[key] = deepcopy(result)
            self._commit(self._changes)
            return result
        finally:
            self._changes = None

    def committed_action(self, action_id):
        """Point-read after the writer's confirmation barrier; never re-execute."""
        return self._get('action:' + action_id)

    def flush_preparation(self):
        """Publish fixed prepared facts before any corresponding business effect."""
        if self._changes is not None:
            self._commit(self._changes)
            self._changes = {}

    def load_user_aliases(self): return self._get('source:aliases', {})
    def load_foods(self):
        # The verified batch snapshot seeds immutable content hashes only. The
        # current food: index still selects every returned version and identity.
        for food in self._get('source:foods', []):
            ref = hashlib.sha256(encode(food)).hexdigest()
            if ref not in self.journal.cache:
                self.journal.cache_document(ref, deepcopy(food))
        return [self._get(k) for k in self._keys('food:')]
    def load_knowledge(self): return self._get('source:knowledge', [])
    def base_standard(self, user_id):
        result = {**DEFAULT_DAILY_STANDARD, 'source':'cos'}
        for row in sorted(self._get('source:standards', []), key=lambda r:(bool(r.get('date')), bool(r.get('user_id')))):
            if row.get('user_id','') not in ('',user_id): continue
            if row.get('date') and row['date'] != self.today_string(): continue
            result.update({k:row[k] for k in DEFAULT_DAILY_STANDARD if k in row})
        return result

    def _archive(self, kind, identity, value, user_id=''):
        key = f'archive:{kind}:{identity}'
        old = self._get(key, {})
        if old.get('value') == value: return
        self._save({key:{'kind':kind, 'id':identity, 'revision':old.get('revision',0)+1,
                         'user_id':user_id, 'value':deepcopy(value)}})

    def cache_food(self, food):
        food = deepcopy(food)
        food.setdefault('food_id', stable_uuid('calorie-agent:food:'+normalize_food_key(food['name'])))
        key = 'food:'+food['food_id']
        value = {**self._get(key,{}), **food}
        self._save({key:value})
        self._archive('food', food['food_id'], value)

    def write_food(self, food):
        if not food.get('name') or any(food.get(k) is None for k in ('carbs_per_100g','protein_per_100g','fat_per_100g')):
            return {'status':'invalid','food_id':'','name':food.get('name','')}
        identity = food.get('food_id') or stable_uuid('calorie-agent:food:'+normalize_food_key(food['name']))
        exists = self._get('food:'+identity)
        if not exists: self.cache_food({**food,'food_id':identity})
        return {'status':'duplicate' if exists else 'created','food_id':identity,'name':food['name']}

    def load_document(self, kind, user_id, scope=''):
        payload = self._get('document:'+stable_uuid(f'{kind}:{user_id}:{scope}'),{}).get('payload',{})
        return paged_document.unpack(self, kind, payload)

    def save_document(self, kind, user_id, scope, payload):
        key = 'document:'+stable_uuid(f'{kind}:{user_id}:{scope}')
        if self.journal.format == 2:
            packed = paged_document.pack(self, kind, key, payload)
            changes = {key:{'intent':kind,'user_id':user_id,'chat_id':scope,'payload':packed}}
            if self._get(key) is None:
                changes['catalog:' + kind + ':' + key] = {'key': key}
                changes['user_catalog:' + user_id + ':' + kind + ':' + key] = {'key': key}
        else:
            keys = self._document_keys(kind)
            changes = {key:{'intent':kind,'user_id':user_id,'chat_id':scope,'payload':deepcopy(payload)}}
            if key not in keys:
                coverage = hashlib.sha256(encode(sorted(set(self._keys('document:')) | {key}))).hexdigest()
                changes.update({'document_index:'+kind:keys+[key], 'document_index:ready':coverage})
        self._save(changes)
        if kind == 'user_settings':
            # Delivery routing is runtime metadata, not a settings ledger change.
            business = {k:v for k,v in payload.items() if k != 'feishu_chat_id'}
            if business: self._archive('settings', user_id, business, user_id)

    def list_documents(self, kind):
        return [{**self._get(key), 'payload': self.load_document(kind, self._get(key)['user_id'], self._get(key)['chat_id'])} for key in self._document_keys(kind)]

    def _document_keys(self, kind):
        if self.journal.format == 2:
            return [self._get(k)['key'] for k in self._keys('catalog:' + kind + ':')]
        document_keys = self._keys('document:')
        coverage = hashlib.sha256(encode(document_keys)).hexdigest()
        if self._get('document_index:ready') == coverage:
            return self._get('document_index:'+kind, [])
        # Existing hashed document keys carry no kind. Rebuild this metadata
        # when key coverage changes, including additions by a rolled-back writer
        # that does not maintain this derived index. Boolean markers need repair.
        if self.readonly and self._legacy_document_index and self._legacy_document_index[0] == coverage:
            return self._legacy_document_index[1].get(kind, [])
        index = {}
        for key in document_keys:
            doc = self._get(key)
            index.setdefault(doc['intent'], []).append(key)
        if self.readonly:
            self._legacy_document_index = (coverage, index)
        else:
            changes = {key:[] for key in self._keys('document_index:') if key != 'document_index:ready'}
            changes.update({'document_index:'+name:keys for name,keys in index.items()})
            self._save({**changes, 'document_index:ready':coverage})
        return index.get(kind, [])

    def _put_row(self, row):
        old = self._get('item:'+row['record_id'])
        if old == row: return
        row = deepcopy(row)
        row['agent_revision'] = (old or {}).get('agent_revision',0)+1
        index = 'day:'+row['user_id']+':'+row['date']
        ids = self._get(index,[])
        self._save({'item:'+row['record_id']:row, index:list(dict.fromkeys(ids+[row['record_id']]))})
        self._archive('intake',row['record_id'],row,row['user_id'])

    def write_intake(self, message, items, meal_type='unknown'):
        action = message_key(message)
        legacy_mid = re.sub(r':agent:\d+$', '', action)
        if self.load_document('legacy_message','system',legacy_mid).get('completed'):
            return {'status':'duplicate_skipped','created_count':0,'records':[]}
        key = 'intake_action:'+action
        previous = self._get(key)
        if previous is not None:
            return {'status':'duplicate_skipped','created_count':0,'records':previous}
        if not items: return {'status':'nothing_to_write','created_count':0,'records':[]}
        records = []
        # Direct callers also commit the complete batch atomically.
        def write():
            for i,item in enumerate(items):
                identity = stable_uuid(f'calorie-agent:item:{action}:{i+1}')
                row = {'record_id':identity,'date':item.get('date') or self.today_string(),
                    'user_id':message['user_id'],'message_id':action,'raw_text':message.get('text',''),
                    'meal_type':item.get('meal_type') or meal_type,'food_id':item.get('food_id',''),
                    'food_name':item.get('matched_name',''),'grams':item['grams'],
                    'nutrition':deepcopy(item['nutrition']),'snapshot':deepcopy(item.get('snapshot',{})),
                    'status':'valid','created_at':int(time.time()*1000)+i}
                self._put_row(row)
                records.append({'record_id':identity})
            self._save({key:records, 'token:'+intake_client_token(message):True})
            return {'status':'created','created_count':len(records),'records':records}
        return write() if self._changes is not None else self.commit_action('write:'+action,write)

    def intake_for_action(self, user_id, action_id):
        return self.intake_by_ids(user_id, [r['record_id'] for r in self._get('intake_action:'+action_id, [])], True)

    def intake_by_ids(self,user_id,ids,include_deleted=False):
        rows = [r for identity in ids if (r:=self._get('item:'+identity)) is not None]
        return [r for r in rows if r['user_id']==user_id and (include_deleted or _is_valid_intake_status(r['status']))]

    def intake_items(self,user_id,start_date='',end_date='',include_deleted=False):
        ids = []
        if start_date and end_date:
            current, stop = datetime.fromisoformat(start_date).date(), datetime.fromisoformat(end_date).date()
            keys = []
            while current < stop:
                keys.append('day:' + user_id + ':' + current.isoformat())
                current += timedelta(days=1)
        else:
            keys = self._keys('day:'+user_id+':')
        for key in keys:
            day = key.rsplit(':',1)[1]
            if start_date and day < start_date: continue
            if end_date and day >= end_date: continue
            ids.extend(self._get(key,[]))
        return sorted(self.intake_by_ids(user_id,dict.fromkeys(ids),include_deleted),key=lambda r:r['created_at'])

    def today_summary(self,user_id,calories_only=False):
        day = self.today_string()
        end = (datetime.fromisoformat(day)+timedelta(days=1)).date().isoformat()
        rows = self.intake_items(user_id,day,end)
        totals = {k:round_number(sum(r['nutrition'][k] for r in rows)) for k in DEFAULT_DAILY_STANDARD}
        return {'date':day,'items':rows,'count':len(rows),'totals':totals,'source':'cos','cos_revision':self.journal.revision}

    def summarize_history(self,user_id,start_date,end_date,food_query=''):
        rows = [r for r in self.intake_items(user_id,start_date,end_date) if not food_query or food_query in r['food_name']]
        def aggregate(group):
            return {'count':len(group),'grams':round_number(sum(r['grams'] for r in group)),
                    **{k:round_number(sum(r['nutrition'][k] for r in group)) for k in DEFAULT_DAILY_STANDARD}}
        days = [{'date':day,**aggregate([r for r in rows if r['date']==day])} for day in sorted({r['date'] for r in rows})]
        budget = self.get_standard(user_id)['kcal']
        return {'start_date':start_date,'end_date':end_date,'food_query':food_query,'days':days,
                'total':aggregate(rows),'over_budget_days':[d['date'] for d in days if not food_query and d['kcal']>budget],'source':'cos'}

    def last_intake(self,user_id):
        rows = self.intake_items(user_id)
        return rows[-1] if rows else None

    def revise_intake(self,user_id,record_id,item=None):
        row = self._get('item:'+record_id)
        if not row or row['user_id'] != user_id: raise ValueError('intake target unavailable')
        if item is None: row['status']='deleted'
        else: row.update(grams=item['grams'],nutrition=deepcopy(item['nutrition']),snapshot=deepcopy(item['snapshot']))
        self._put_row(row)
        return self._get('item:'+record_id)

    def undo_last(self,user_id,record_id=''):
        row = next(iter(self.intake_by_ids(user_id,[record_id])),None) if record_id else self.last_intake(user_id)
        if not row: return {'status':'nothing_to_undo','item':None}
        return {'status':'deleted','item':self.revise_intake(user_id,row['record_id'])}

    def create_pending(self,message,intent,food_name,grams,meal_type='unknown',payload=None):
        mid = message_key(message)
        identity = stable_uuid(f'calorie-agent:pending:{mid}:{intent}:{food_name}')
        old = self.get_pending(identity)
        if old: return old
        row = {'record_id':identity,'pending_id':identity,'user_id':message.get('user_id',''),
            'chat_id':message.get('chat_id',''),'source_message_id':mid,'intent':intent,
            'food_name':food_name,'grams':grams,'meal_type':meal_type,'raw_text':message.get('text',''),
            'payload':deepcopy(payload or {}),'status':'pending','created_at':int(time.time()*1000),'expires_at':0}
        self._save({'pending:'+identity:row, 'active_pending:'+identity: {'key': 'pending:'+identity}})
        return row

    def get_pending(self,pending_id): return self._get('pending:'+pending_id)
    def mark_pending_status(self,task,status):
        row = self.get_pending(task['pending_id'])
        if not row: raise ValueError('pending task unavailable')
        self._save({'pending:'+task['pending_id']:{**row,'status':status}, 'active_pending:'+task['pending_id']: {'key':'pending:'+task['pending_id']} if status == 'pending' else None})
        return {'code':0}
    def load_execution(self,message):
        return self.get_pending(stable_uuid(f'calorie-agent:pending:{message_key(message)}:message_execution:'))
    def begin_execution(self,message,payload): return self.create_pending(message,'message_execution','',None,payload=payload)
    def save_execution(self,task,payload,completed=False):
        self._save({'pending:'+task['pending_id']:{**task,'payload':deepcopy(payload),'status':'completed' if completed else 'pending','expires_at':0, 'completed_at':time.time() if completed else None}, 'active_pending:'+task['pending_id']:None if completed else {'key':'pending:'+task['pending_id']}})
    def load_unfinished_executions(self,user_id='',chat_id='',message=None):
        rows = [self._get(self._get(k)['key']) for k in self._keys('active_pending:')] if self.journal.format == 2 else [self._get(k) for k in self._keys('pending:')]
        tasks = {}
        for row in rows:
            if row['status']!='pending' or row['intent'] not in {'message_received','message_execution'}: continue
            if user_id and row['user_id']!=user_id: continue
            if chat_id and row['chat_id']!=chat_id: continue
            key=row['source_message_id']
            if key not in tasks or row['intent']=='message_received': tasks[key]=row
        return sorted(tasks.values(),key=lambda r:r['created_at'])
    def append_usage_event(self,event): self._save({'usage:'+event['event_id']:deepcopy(event)})

    def import_baseline(self,data):
        baseline_id = data.get('baseline_id') or hashlib.sha256(encode(data)).hexdigest()
        if self.journal.get('baseline'):
            if self.journal.get('baseline') != baseline_id: raise ValueError('different baseline already imported')
            return
        if len(self.journal.state):
            raise ValueError('baseline import requires empty business namespace')
        def apply():
            self._save({'source:aliases':data.get('aliases',{}),'source:knowledge':data.get('knowledge',[]),
                        'source:standards':data.get('standards',[])})
            # Published source snapshots already exist in Bitable. Importing
            # them is not a new food-library business change to archive.
            for food in data.get('foods',[]):
                self._save({'food:'+food['food_id']:deepcopy(food)})
            self._save({'source:foods':[self._get(k) for k in self._keys('food:')]})
            actions = {}
            for raw in data.get('intake',[]):
                row=deepcopy(raw)
                row.setdefault('feishu_record_id',row['record_id'])
                self._put_row(row)
                actions.setdefault(row['message_id'],[]).append({'record_id':row['record_id']})
            for action,records in actions.items():
                self._save({'intake_action:'+action:records})
                if action:
                    mid = re.sub(r':agent:\d+$', '', action)
                    self.save_document('legacy_message','system',mid,{'completed':True})
            for row in data.get('pending',[]):
                self._save({'pending:'+row['pending_id']:row})
                if row['intent'] == 'message_received' and row['status'] == 'completed':
                    rid = hashlib.sha256(('feishu:'+row['source_message_id']).encode()).hexdigest()
                    self.save_document('receipt_state','system',rid,{'status':'completed','attempts':0,
                        'migrated':True,'receipt_id':rid,'delivery':deepcopy(row.get('payload',{}).get('delivery',{}))})
                if row['intent'] == 'reminder_receipt':
                    self.save_document('reminder_receipt',row['user_id'],row['chat_id'],row['payload'])
                if row['intent'] in {'agent_session','user_settings'}:
                    self.save_document(row['intent'],row['user_id'],row['chat_id'],row['payload'])
            self._save({'baseline':baseline_id})
            return {'imported':True}
        return self.commit_action('baseline-import',apply)


def _atomic(method):
    def wrapped(self, *args, **kwargs):
        if self._changes is not None:
            return method(self, *args, **kwargs)
        if self.readonly:
            raise PermissionError("readonly COS Store")
        self._changes = {}
        try:
            result = method(self, *args, **kwargs)
            self._commit(self._changes)
            return result
        finally:
            self._changes = None
    return wrapped


for _method in ("cache_food", "write_food", "save_document", "revise_intake", "undo_last"):
    setattr(CosStore, _method, _atomic(getattr(CosStore, _method)))
