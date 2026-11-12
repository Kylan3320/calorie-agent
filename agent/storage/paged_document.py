"""Lazy keyed collections in Store documents; mutations save touched entries only."""
from collections.abc import MutableMapping
from copy import deepcopy
import hashlib


class DocumentMap(MutableMapping):
    def __init__(self, store, prefix, count=0):
        self.store, self.prefix, self.count = store, prefix, count
        self.loaded, self.before, self.deleted = {}, {}, set()

    def __getitem__(self, key):
        if key in self.deleted:
            raise KeyError(key)
        if key not in self.loaded:
            value = self.store._get(self.prefix + key)
            if value is None:
                raise KeyError(key)
            self.loaded[key] = value
            self.before[key] = deepcopy(value)
        return self.loaded[key]

    def __setitem__(self, key, value):
        if key not in self.before:
            self.before[key] = self.store._get(self.prefix + key)
        self.loaded[key] = value
        self.deleted.discard(key)

    def __delitem__(self, key):
        self[key]
        self.deleted.add(key)

    def __iter__(self):
        keys = {key[len(self.prefix):] for key in self.store._keys(self.prefix)}
        return iter(sorted((keys | set(self.loaded)) - self.deleted))

    def prefix_items(self, prefix):
        names = {key[len(self.prefix):] for key in self.store._keys(self.prefix + prefix)}
        names.update(key for key in self.loaded if key.startswith(prefix))
        for key in sorted(names - self.deleted):
            yield key, self[key]

    def __len__(self):
        return self.count + sum(self.before.get(k) is None for k in self.loaded if k not in self.deleted) - sum(self.before.get(k) is not None for k in self.deleted)

    def __deepcopy__(self, memo):
        result = DocumentMap(self.store, self.prefix, self.count)
        result.loaded, result.before, result.deleted = deepcopy(self.loaded, memo), deepcopy(self.before, memo), set(self.deleted)
        return result

    def persist(self):
        changes = {self.prefix + key: value for key, value in self.loaded.items()
                   if key not in self.deleted and value != self.before.get(key)}
        changes.update({self.prefix + key: None for key in self.deleted if self.before.get(key) is not None})
        self.store._save(changes)
        return {'$paged_map': self.prefix, 'count': len(self)}


FIELDS = {
    'conversation_context': {'positions'},
    'memory_index': {'items'},
    'memory_state': {'current', 'revoked', 'redactions', 'feedback'},
    'memory_work': {'pending', 'processed'},
    'memory_backfill': {'uncovered', 'uncovered_refs'},
}
SEQUENCES = {'memory_state': {'redacted_quotes','entries'}}
WHOLE = {'memory_active', 'summary_active', 'diet_goal_active', 'task_active', 'worker_active', 'reminder_delivery_active'}


def unpack(store, kind, payload):
    if isinstance(payload, dict) and '$paged_map' in payload:
        return DocumentMap(store, payload['$paged_map'], payload['count'])
    result = deepcopy(payload)
    for field in FIELDS.get(kind, ()):
        value = result.get(field)
        if isinstance(value, dict) and '$paged_map' in value:
            result[field] = DocumentMap(store, value['$paged_map'], value['count'])
    for field in SEQUENCES.get(kind,()):
        value=result.get(field)
        if isinstance(value,dict) and '$paged_sequence' in value:result[field]=sequence(store,value)
    if kind == 'conversation_context':
        if isinstance(result.get('turns'),dict) and '$paged_sequence' in result['turns']:
            result['turns'] = sequence(store,result['turns'])
        covered = result.get('summary',{}).get('covered')
        if isinstance(covered,dict) and '$paged_sequence' in covered:
            result['summary']['covered'] = sequence(store,covered)
    return result


def pack(store, kind, key, payload):
    def convert(field, value):
        if isinstance(value, DocumentMap):
            return value.persist()
        prefix = 'collection:' + hashlib.sha256((key + ':' + field).encode()).hexdigest() + ':'
        old = store._get(key, {}).get('payload', {})
        marker = old if field == '' else old.get(field, {})
        mapping = DocumentMap(store, prefix, marker.get('count', 0) if '$paged_map' in marker else 0)
        if '$paged_map' in marker:
            for name in mapping:
                if name not in value:
                    del mapping[name]
        mapping.update(value)
        return mapping.persist()
    if kind in WHOLE:
        return convert('', payload)
    result = dict(payload)
    for field in SEQUENCES.get(kind,()):
        if field not in result:continue
        values=result[field]
        if not isinstance(values,DocumentSequence):
            prefix='sequence:'+hashlib.sha256((key+':'+field).encode()).hexdigest()+':'
            converted=DocumentSequence(store,prefix)
            for value in values:converted.append(value)
            values=converted
        result[field]=values.persist()
    if kind == 'conversation_context' and 'turns' in result:
        turns = result['turns']
        if not isinstance(turns,DocumentSequence):
            prefix = 'sequence:' + hashlib.sha256((key+':turns').encode()).hexdigest() + ':'
            turns = DocumentSequence(store,prefix)
            for item in result['turns']: turns.append(item)
            result['positions'] = {item['id']:i for i,item in enumerate(result['turns'])}
        result['turns'] = turns.persist()
        result['summary'] = dict(result.get('summary',{}))
        covered = result['summary'].get('covered')
        if covered is not None:
            if not isinstance(covered,DocumentSequence):
                prefix = 'sequence:' + hashlib.sha256((key+':covered').encode()).hexdigest() + ':'
                converted = DocumentSequence(store,prefix)
                for item in covered: converted.append(item)
                covered = converted
            result['summary']['covered'] = covered.persist()
    for field in FIELDS.get(kind, ()):
        if field in result:
            result[field] = convert(field, result[field])
    return result


class DocumentSequence:
    """Appendable 32-entry pages with a stable absolute position and slice views."""
    def __init__(self,store,prefix,start=0,stop=0,total=0):
        self.store,self.prefix,self.start,self.stop,self.total = store,prefix,start,stop,total
        self.pages,self.before = {},{}

    def _page(self,number):
        if number not in self.pages:
            self.pages[number]=self.store._get(self.prefix+f'{number:012d}',[])
            self.before[number]=deepcopy(self.pages[number])
        return self.pages[number]

    def __eq__(self,other):
        if isinstance(other,(list,DocumentSequence)):return list(self)==list(other)
        return False
    def __len__(self):return self.stop-self.start
    def __iter__(self):
        for i in range(len(self)):yield self[i]
    def __reversed__(self):
        for i in range(len(self)-1,-1,-1):yield self[i]
    def __getitem__(self,index):
        if isinstance(index,slice):
            first,last,step=index.indices(len(self))
            if step!=1:return [self[i] for i in range(first,last,step)]
            excluded=sum(self[i].get('tokens',0) if isinstance(self[i],dict) else 0 for i in range(first))+sum(self[i].get('tokens',0) if isinstance(self[i],dict) else 0 for i in range(last,len(self)))
            result=DocumentSequence(self.store,self.prefix,self.start+first,self.start+last,self.token_total-excluded)
            result.pages,result.before=self.pages,self.before
            return result
        if index<0:index+=len(self)
        if not 0<=index<len(self):raise IndexError(index)
        absolute=self.start+index
        return self._page(absolute//32)[absolute%32]
    def append(self,value):
        page=self._page(self.stop//32);position=self.stop%32
        if position<len(page):page[position]=value
        elif position==len(page):page.append(value)
        else:raise ValueError('sparse sequence page')
        self.stop+=1
    def __deepcopy__(self,memo):
        result=DocumentSequence(self.store,self.prefix,self.start,self.stop,self.total)
        result.pages,result.before=deepcopy(self.pages,memo),deepcopy(self.before,memo)
        return result
    @property
    def token_total(self):
        def total(page,number):
            return sum(value.get('tokens',0) if isinstance(value,dict) else 0 for i,value in enumerate(page) if self.start<=number*32+i<self.stop)
        return self.total+sum(total(page,n)-total(self.before[n],n) for n,page in self.pages.items())
    def persist(self):
        self.store._save({self.prefix+f'{n:012d}':page for n,page in self.pages.items() if page!=self.before[n]})
        total = self.token_total
        self.total, self.before = total, deepcopy(self.pages)
        return {'$paged_sequence':self.prefix,'start':self.start,'stop':self.stop,'tokens':total}


def sequence(store,marker):
    return DocumentSequence(store,marker['$paged_sequence'],marker['start'],marker['stop'],marker.get('tokens',0))
