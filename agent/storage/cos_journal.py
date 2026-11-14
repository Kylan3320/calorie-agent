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
import uuid
from .telemetry import span
from .cos_index import RadixIndex

CACHE_MAX_BYTES = 8 * 1024 * 1024
CACHE_MAX_ENTRIES = 512


def encode(value):
    def compatible(value):
        from .paged_document import DocumentMap, DocumentSequence
        if isinstance(value, DocumentMap): return dict(value)
        if isinstance(value, DocumentSequence): return list(value)
        raise TypeError('unsupported persistent value: ' + type(value).__name__)
    return json.dumps(value, default=compatible, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()


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
    def __init__(self, objects, prefix, readonly=False, guard=None):
        self.guard = guard
        self.objects, self.prefix, self.readonly = objects, prefix.rstrip('/') + '/', readonly
        self.revision, self.index, self.unavailable = -1, {}, False
        self.cache, self._cache_sizes, self.cache_bytes = OrderedDict(), {}, 0
        self.state = _State(self)
        self.view_id = uuid.uuid4().hex[:12]
        self.format = None
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
        if value.get('schema') not in {'calorie.cos.v1', 'calorie.cos.v2'} or value['revision'] != revision or value['previous'] != revision - 1:
            raise CommitUnavailable('commit identity mismatch')
        return value

    def keys(self, prefix=''):
        self._check()
        if isinstance(self.index, RadixIndex):
            return self.index.keys_with_prefix(prefix)
        return (key for key in self.index if key.startswith(prefix))

    def _catchup(self, revision):
        start = self.revision
        checkpoint = revision - revision % 128 if revision > start and not (start >= 0 and revision - start <= 128) else None
        with span('cos_catchup', view_id=self.view_id, prefix=self.prefix, start_revision=start,
                  requested_revision=revision, checkpoint_revision=checkpoint,
                  read_from_revision=checkpoint if checkpoint is not None else start + 1,
                  read_to_revision=revision) as event:
            try:
                return self._apply_catchup(revision)
            finally:
                event['end_revision'] = self.revision
                event['format'] = self.format
                if self.format == 2:
                    event.update(checkpoint_revision=self.revision,read_from_revision=revision,read_to_revision=revision)

    def _apply_catchup(self, revision):
        try:
            if revision > self.revision and self.format != 1:
                latest = self._read(revision)
                if latest['schema'] == 'calorie.cos.v2':
                    self.index = RadixIndex(self, latest['root'])
                    self.format, self.revision = 2, revision
                    return
                if self.format == 2:
                    raise CommitUnavailable('incompatible writer: v1 after v2')
                self.format = 1
            checkpoint = None
            if self.revision >= 0 and 0 < revision - self.revision <= 128:
                # Contiguous predecessors are known from the committed format.
                # Fetch only the warm delta; validate every exact key first.
                chain = _transfer_batch(self._read, range(self.revision + 1, revision + 1))
            else:
                # The existing v1 writer publishes a checkpoint at every 128th
                # position. Locate it directly, then validate the complete tail
                # before changing shared state. Never serially search backwards.
                base = revision - revision % 128
                if revision > self.revision:
                    checkpoint = self._read(base)
                    if 'index' not in checkpoint:
                        raise CommitUnavailable('known checkpoint unavailable')
                    chain = _transfer_batch(self._read, range(base + 1, revision + 1))
                else:
                    chain = []
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

    def _candidate(self):
        prefix = self.prefix + 'commits/'

        def numbers(keys):
            return [int(key[-17:-5]) for key in keys
                    if key.startswith(prefix) and key.endswith('.json') and key[-17:-5].isdigit()]

        if self.revision >= 0:
            # COS's native marker avoids enumerating already verified pages.
            return max([self.revision, *numbers(self.objects.list(prefix, marker=self.key(self.revision)))])
        # Commits are contiguous. Probe the first key of exponentially spaced
        # 1000-position blocks, then bisect to the last occupied block. Only
        # that final bounded page is enumerated, regardless of historic pages.
        low, high = 0, 1
        while self.objects.list(prefix, marker=self.key(high * 1000 - 1), limit=1):
            low, high = high, high * 2
        while high - low > 1:
            middle = (low + high) // 2
            if self.objects.list(prefix, marker=self.key(middle * 1000 - 1), limit=1):
                low = middle
            else:
                high = middle
        marker = self.key(low * 1000 - 1) if low else ''
        return max([-1, *numbers(self.objects.list(prefix, marker=marker, limit=1000))])

    def refresh(self, confirm=True, minimum_revision=None):
        with span('cos_refresh', view_id=self.view_id, prefix=self.prefix, readonly=self.readonly,
                  start_revision=self.revision, minimum_revision=minimum_revision, confirm=confirm,
                  reason='first_use' if self.revision < 0 else 'uncertain_commit' if self.unavailable else 'confirmed_view') as event:
            try:
                return self._refresh(confirm, minimum_revision)
            finally:
                event['end_revision'] = self.revision

    def _refresh(self, confirm, minimum_revision):
        try:
            if self.readonly and confirm:
                raise PermissionError('readonly journal cannot confirm by writing')
            # An accepted ingress watermark is exact evidence; satisfy it before
            # consulting LIST, which can only suggest additional candidates.
            if minimum_revision is not None:
                self._catchup(minimum_revision)
            candidate = self._candidate()
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

    def _append(self, changes, documents=None):
        if self.guard:
            self.guard()
        number = self.revision + 1
        envelope = {'schema': 'calorie.cos.v1', 'revision': number, 'previous': self.revision, 'changes': changes}
        pages = {}
        if self.format != 1:
            index = self.index if isinstance(self.index, RadixIndex) else RadixIndex(self)
            updated, pages = index.changed(changes) if changes else (index, {})
            envelope.update(schema='calorie.cos.v2', root=updated.root)
        elif number % 128 == 0:
            # Legacy readers stay usable before explicit generation upgrade.
            index = {**self.index, **{k:v for k,v in changes.items() if v is not None}}
            for k,v in changes.items():
                if v is None: index.pop(k, None)
            envelope['index'] = index
        # The commit is the only visibility boundary. Content-addressed
        # documents and index pages can all transfer in the same bounded batch.
        def publish(transfer):
            writer, item = transfer
            writer(item)
        transfers = [(self._publish_document, (ref, raw)) for ref, (raw, _) in (documents or {}).items()]
        transfers.extend((updated.publish, (ref, raw)) for ref, (raw, _) in pages.items())
        _transfer_batch(publish, transfers)
        try:
            created = self.objects.create(self.key(number), encode(envelope))
        except BaseException:
            self.unavailable = True
            raise
        if not created:
            return False
        for ref, (raw, value) in (documents or {}).items():
            self.cache_document(ref, value, len(raw))
        for ref, (raw, node) in pages.items():
            self.cache_document(ref, node, len(raw))
        if envelope['schema'] == 'calorie.cos.v2':
            self.index, self.format = updated, 2
        else:
            for k,v in changes.items():
                if v is None: self.index.pop(k, None)
                else: self.index[k] = v
        self.revision = number
        return True

    def get(self, key, default=None):
        return self.state.get(key, default)

    def commit(self, changes, *, fragments=None):
        try:
            return self._commit(changes, fragments)
        except BaseException:
            if not self.readonly:
                self.unavailable = True
            raise

    def _commit(self, changes, fragments):
        self._check()
        if self.readonly:
            raise PermissionError('readonly journal')
        refs, documents = {}, dict(fragments or {})
        for key,value in changes.items():
            if value is None:
                if key in self.index: refs[key] = None
                continue
            raw = encode(value)
            ref = hashlib.sha256(raw).hexdigest()
            if self.index.get(key) == ref: continue
            documents[ref] = (raw, deepcopy(value))
            refs[key] = ref
        if refs and not self._append(refs, documents):
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
