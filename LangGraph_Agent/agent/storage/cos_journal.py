"""Immutable single-writer COS commits with lazy, content-addressed documents.

LIST locates candidates only. Creating the next empty commit is the write/read
barrier; a conflict requires exact-key catch-up. Unknown writes poison an
instance until refresh, so callers never continue from an unconfirmed value.
"""
from collections import OrderedDict
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
import hashlib
import json

CACHE_MAX_BYTES = 8 * 1024 * 1024
CACHE_MAX_ENTRIES = 512


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


class CommitUnavailable(RuntimeError):
    pass


def _transfer_batch(transfer, payloads):
    """Join every independent transfer before returning or raising its error."""
    payloads = list(payloads)
    if len(payloads) < 2:
        return [transfer(payload) for payload in payloads]
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(copy_context().run, transfer, payload) for payload in payloads]
        results, error = [], None
        for future in futures:
            try:
                results.append(future.result())
            except BaseException as exc:
                if error is None:
                    error = exc
        if error is not None:
            raise error
        return results


class _State(Mapping):
    def __init__(self, journal):
        self.journal = journal

    def __iter__(self):
        self.journal._check()
        return iter(self.journal.index)

    def __len__(self):
        return len(self.journal.index)

    def __getitem__(self, key):
        journal = self.journal
        journal._check()
        ref = journal.index[key]
        if ref in journal.cache:
            value = journal.cache[ref]
            journal.cache.move_to_end(ref)
        else:
            raw = journal.objects.get(journal.prefix + 'documents/' + ref + '.json')
            if raw is None or hashlib.sha256(raw).hexdigest() != ref:
                raise CommitUnavailable('known document unavailable: ' + key)
            value = json.loads(raw)
            journal.cache_document(ref, value, len(raw))
        return deepcopy(value)


class Journal:
    def __init__(self, objects, prefix, readonly=False):
        self.objects, self.prefix, self.readonly = objects, prefix.rstrip('/') + '/', readonly
        self.revision, self.index, self.unavailable = -1, {}, False
        self.cache, self._cache_sizes, self.cache_bytes = OrderedDict(), {}, 0
        self.state = _State(self)
        self.refresh(confirm=not readonly)

    def cache_document(self, ref, value, size=None):
        size = len(encode(value)) if size is None else size
        if size > CACHE_MAX_BYTES:
            return
        if ref in self.cache:
            self.cache.move_to_end(ref)
            return
        while self.cache and (self.cache_bytes + size > CACHE_MAX_BYTES or len(self.cache) >= CACHE_MAX_ENTRIES):
            oldest, _ = self.cache.popitem(last=False)
            self.cache_bytes -= self._cache_sizes.pop(oldest)
        self.cache[ref] = value
        self._cache_sizes[ref] = size
        self.cache_bytes += size

    def _check(self):
        if self.unavailable:
            raise CommitUnavailable('uncertain commit; refresh before use')

    def key(self, revision):
        return self.prefix + f'commits/{revision:012d}.json'

    def _read(self, revision):
        raw = self.objects.get(self.key(revision))
        if raw is None:
            raise CommitUnavailable('known commit temporarily invisible: ' + self.key(revision))
        value = json.loads(raw)
        if value.get('schema') != 'calorie.cos.v1' or value['revision'] != revision or value['previous'] != revision - 1:
            raise CommitUnavailable('commit identity mismatch')
        return value

    def _catchup(self, revision):
        try:
            checkpoint = None
            if self.revision >= 0 and 0 < revision - self.revision <= 128:
                # Contiguous predecessors are known from the committed format.
                # Fetch only the warm delta; validate every exact key first.
                chain = _transfer_batch(self._read, range(self.revision + 1, revision + 1))
            else:
                chain = []
                while revision > self.revision:
                    value = self._read(revision)
                    if 'index' in value:
                        checkpoint = value
                        break
                    chain.append(value)
                    revision = value['previous']
                chain.reverse()
            # Worker threads never change the shared index, revision or cache.
            if checkpoint is not None:
                self.index = checkpoint['index']
                self.revision = checkpoint['revision']
            for value in chain:
                for key, ref in value['changes'].items():
                    if ref is None:
                        self.index.pop(key, None)
                    else:
                        self.index[key] = ref
                self.revision = value['revision']
        except BaseException:
            self.unavailable = True
            raise

    def refresh(self, confirm=True, minimum_revision=None):
        try:
            if self.readonly and confirm:
                raise PermissionError('readonly journal cannot confirm by writing')
            numbers = [int(k[-17:-5]) for k in self.objects.list(self.prefix+'commits/')
                       if k.startswith(self.prefix+'commits/') and k.endswith('.json') and k[-17:-5].isdigit()]
            candidate = max([self.revision, minimum_revision if minimum_revision is not None else -1, *numbers])
            self._catchup(candidate)
            if confirm:
                for _ in range(10000):
                    if self._append({}):
                        break
                    self._catchup(self.revision+1)
                else:
                    raise CommitUnavailable('commit catch-up limit reached')
            self.unavailable = False
            return self.revision
        except BaseException:
            self.unavailable = True
            raise

    def _append(self, changes):
        number = self.revision + 1
        envelope = {'schema': 'calorie.cos.v1', 'revision': number, 'previous': self.revision, 'changes': changes}
        # Checkpoints copy references, never all historical business payloads.
        if number % 128 == 0:
            index = {**self.index, **{k:v for k,v in changes.items() if v is not None}}
            for k,v in changes.items():
                if v is None: index.pop(k, None)
            envelope['index'] = index
        try:
            created = self.objects.create(self.key(number), encode(envelope))
        except BaseException:
            self.unavailable = True
            raise
        if not created:
            return False
        for k,v in changes.items():
            if v is None: self.index.pop(k, None)
            else: self.index[k] = v
        self.revision = number
        return True

    def get(self, key, default=None):
        return self.state.get(key, default)

    def commit(self, changes):
        try:
            return self._commit(changes)
        except BaseException:
            if not self.readonly:
                self.unavailable = True
            raise

    def _commit(self, changes):
        self._check()
        if self.readonly:
            raise PermissionError('readonly journal')
        refs, documents = {}, {}
        for key,value in changes.items():
            if value is None:
                if key in self.index: refs[key] = None
                continue
            raw = encode(value)
            ref = hashlib.sha256(raw).hexdigest()
            if self.index.get(key) == ref: continue
            documents[ref] = (raw, deepcopy(value))
            refs[key] = ref
        # Immutable payloads are independent. Their manifest is the sole visible
        # commit, and is appended only after all uploads have succeeded.
        _transfer_batch(self._publish_document, [(ref, raw) for ref, (raw, _) in documents.items()])
        for ref, (raw, value) in documents.items():
            self.cache_document(ref, value, len(raw))
        if refs and not self._append(refs):
            self.unavailable = True
            raise CommitUnavailable('writer changed; refresh and recover original action')
        return self.revision

    def _publish_document(self, document):
        ref, raw = document
        object_key = self.prefix + 'documents/' + ref + '.json'
        if not self.objects.create(object_key, raw):
            existing = self.objects.get(object_key)
            if existing != raw:
                raise CommitUnavailable('known document not visible or corrupted')
