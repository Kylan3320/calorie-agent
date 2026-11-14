"""Bounded immutable radix pages. Only the changed leaf and its ancestors copy.

Keys use UTF-8 hex digits, giving a fixed fanout of 16. Leaves contain at most
32 identities. Empty paths disappear; roots carry counts, never page-name lists.
The Journal's existing bounded content cache also caches index pages.
"""
from collections.abc import Mapping
import hashlib

PAGE_SIZE = 32


class RadixIndex(Mapping):
    def __init__(self, journal, root=None):
        self.journal, self.root = journal, root

    def _read(self, ref):
        if ref is None:
            return {'items': []}
        j = self.journal
        node = j.read_content(ref, 'indexes')
        if ('items' in node and len(node['items']) > PAGE_SIZE or
                'children' in node and len(node['children']) > 17):
            raise ValueError('invalid index page capacity')
        return node

    def publish(self, page):
        from .cos_journal import CommitUnavailable
        ref, raw = page
        key = self.journal.prefix + 'indexes/' + ref + '.json'
        if not self.journal.objects.create(key, raw) and self.journal.objects.get(key) != raw:
            raise CommitUnavailable('immutable index page mismatch')

    @staticmethod
    def path(key):
        return key.encode().hex() + 'z'  # terminator sorts after hex, distinct from data

    def __getitem__(self, key):
        node, depth = self._read(self.root), 0
        path = self.path(key)
        while 'children' in node:
            skip=node.get('skip','')
            if path[depth:depth+len(skip)]!=skip:raise KeyError(key)
            depth+=len(skip)
            ref = node['children'].get(path[depth])
            if ref is None:
                raise KeyError(key)
            node, depth = self._read(ref), depth + 1
        for name, value in node['items']:
            if name == key:
                return value
        raise KeyError(key)

    def __len__(self):
        node = self._read(self.root)
        return len(node['items']) if 'items' in node else node['count']

    def __iter__(self):
        return self.keys_with_prefix('')

    def keys_with_prefix(self, prefix):
        path = prefix.encode().hex()
        def walk(ref, depth):
            node = self._read(ref)
            if 'items' in node:
                yield from (key for key, _ in node['items'] if key.startswith(prefix))
            else:
                skip=node.get('skip','')
                shared=min(len(skip),max(0,len(path)-depth))
                if skip[:shared]!=path[depth:depth+shared]:return
                depth+=len(skip)
                if depth<len(path):
                    if path[depth] in node['children']:
                        yield from walk(node['children'][path[depth]], depth+1)
                else:
                    for digit,child in sorted(node['children'].items()):
                        yield from walk(child,depth+1)
        yield from walk(self.root, 0)

    def changed(self, changes, make_ref=lambda digest: digest):
        from .cos_journal import encode
        pages = {}

        def read(ref):
            if ref in pages:
                return pages[ref][1]
            return self._read(ref)

        def stage(node):
            raw = encode(node)
            ref = make_ref(hashlib.sha256(raw).hexdigest())
            if ref not in self.journal.cache:
                pages[ref] = (raw, node)
            return ref

        def update(ref, changes, depth):
            node = read(ref)
            child_counts = node.get('child_counts', {})
            if 'items' in node:
                values = dict(node['items'])
                for key, value in changes.items():
                    if value is None:
                        values.pop(key, None)
                    else:
                        values[key] = value
                if len(values) <= PAGE_SIZE:
                    return stage({'items': sorted(values.items())}) if values else None, len(values)
                paths=[self.path(key)[depth:] for key in values]
                first,last=min(paths),max(paths)
                length=0
                while length<min(len(first),len(last)) and first[length]==last[length]:length+=1
                node,changes={'children':{},'skip':first[:length]},values
            skip=node.get('skip','')
            common=len(skip)
            for key in changes:
                candidate=self.path(key)[depth:depth+len(skip)]
                length=0
                while length<min(common,len(candidate)) and skip[length]==candidate[length]:length+=1
                common=min(common,length)
            if common<len(skip):
                # A new key diverges inside a compressed path. Retain the old
                # subtree and copy only this split plus the changed descendant.
                old={**node,'skip':skip[common+1:]}
                node={'children':{skip[common]:stage(old)},'skip':skip[:common]}
                child_counts = {skip[common]: old['count']}
                skip=skip[:common]
            depth+=len(skip)
            groups = {}
            for key, value in changes.items():
                groups.setdefault(self.path(key)[depth], {})[key] = value
            children = dict(node['children'])
            counts = dict(child_counts)
            for digit, updates in groups.items():
                child, count = update(children.get(digit), updates, depth + 1)
                if child:
                    children[digit] = child
                    counts[digit] = count
                else:
                    children.pop(digit, None)
                    counts.pop(digit, None)
            for digit in children.keys() - counts.keys():
                child = read(children[digit])
                counts[digit] = len(child['items']) if 'items' in child else child['count']
            count = sum(counts.values())
            return (stage({'children': children, 'child_counts': counts, 'skip':skip, 'count': count}) if count else None), count
        root, _ = update(self.root, changes, 0)
        # Splitting a full leaf can stage intermediate pages before later
        # updates in this batch replace them. Upload only the final tree.
        reachable = {}
        pending = [root]
        while pending:
            ref = pending.pop()
            if ref in reachable or ref not in pages:
                continue
            reachable[ref] = pages[ref]
            pending.extend(pages[ref][1].get('children', {}).values())
        return RadixIndex(self.journal, root), reachable

    def physical_pages(self):
        seen = set()
        def walk(ref):
            if ref is None or ref in seen:
                return
            seen.add(ref)
            for child in self._read(ref).get('children', {}).values():
                walk(child)
        walk(self.root)
        return seen
