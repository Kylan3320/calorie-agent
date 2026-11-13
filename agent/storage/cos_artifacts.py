"""Immutable execution fragments shared by progress versions and result readers."""
import hashlib
import json
from .cos_journal import encode, CommitUnavailable, _transfer_batch


def freeze(journal, value):
    """Split audit lists and large dictionaries, leaving only bounded references."""
    fragments = {}

    def reference(value):
        raw = encode(value)
        ref = hashlib.sha256(raw).hexdigest()
        if ref not in journal.cache:
            fragments[ref] = (raw, value)
        return {'$artifact': ref}

    def visit(value, depth=0):
        if isinstance(value, list):
            parts = [visit(v, depth + 1) for v in value]
            # One immutable fragment per exchange/call/request, not per save.
            return [reference(v) if isinstance(v, (dict, list)) and len(encode(v)) > 512 else v for v in parts]
        if isinstance(value, dict):
            parts = {k: visit(v, depth + 1) for k,v in value.items()}
            return reference(parts) if depth > 1 and len(encode(parts)) > 2048 else parts
        if isinstance(value,str) and depth>1 and len(value.encode())>1024:
            return reference(value)
        return value

    packed = visit(value)
    # Fragments are content-addressed. Readers only see them after the parent
    # commit, so children and parent fragments can transfer together.
    _transfer_batch(journal._publish_document, [(ref, raw) for ref, (raw, _) in fragments.items()])
    for ref, (raw, value) in fragments.items():
        journal.cache_document(ref, value, len(raw))
    return packed


def thaw(journal, value):
    if isinstance(value, dict):
        if set(value) == {'$artifact'}:
            ref = value['$artifact']
            if ref in journal.cache:
                raw_value = journal.cache[ref]
            else:
                raw = journal.objects.get(journal.prefix + 'documents/' + ref + '.json')
                if raw is None or hashlib.sha256(raw).hexdigest() != ref:
                    raise CommitUnavailable('execution artifact unavailable')
                raw_value = json.loads(raw)
                journal.cache_document(ref, raw_value, len(raw))
            return thaw(journal, raw_value)
        return {k: thaw(journal,v) for k,v in value.items()}
    if isinstance(value, list):
        return [thaw(journal,v) for v in value]
    return value
