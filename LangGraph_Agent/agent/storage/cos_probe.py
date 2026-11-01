"""Isolated COS durability probe, never selected by a production preset.

Reuses FakeStore's domain operations on small, explicit fixtures. Persistence is
real when the supplied object transport is real; it is not a production Store.
The caller must enforce one writer for the entire probe namespace. The transport
must provide get(key)->bytes|None, create(key, bytes)->bool (atomic no-overwrite),
and list(prefix)->list[str]. None means a genuine 404, never an arbitrary error.

LIST is only a hint. A successful no-overwrite append to the contiguous sequence
is the read barrier: a hidden successor forces a conflict and exact-key read.
Objects must not be deleted and bucket versioning must remain disabled. A 404
after a create conflict fails closed because an existing snapshot is not visible.
This algorithm does not claim that COS provides transactional or strong reads.
"""
from copy import deepcopy
import json

from .fake_store import FakeStore


class ProbeUnavailable(RuntimeError):
    pass


class CosProbeStore(FakeStore):
    FIELDS = ("foods", "knowledge", "today", "intake", "pending", "usage_events", "_clock")

    def __init__(self, objects, prefix, *, foods=None, today="2026-11-01", max_catchup=1000):
        if not prefix.startswith("cos-probe/"):
            raise ValueError("isolated cos-probe/ prefix required")
        super().__init__(foods=foods, today=today)
        self.objects, self.prefix = objects, prefix.rstrip("/") + "/"
        self.revision, self._depth, self._unavailable = -1, 0, False
        self.metrics = {"read_attempts": 0, "create_attempts": 0, "list_attempts": 0,
                        "bytes_read": 0, "bytes_sent": 0}
        self.metrics["list_attempts"] += 1
        numbers = []
        for key in objects.list(self.prefix):
            tail = key.removeprefix(self.prefix)
            if tail.endswith(".json") and tail[:-5].isdigit() and len(tail[:-5]) == 12:
                numbers.append(int(tail[:-5]))
        if numbers:
            self._read(max(numbers))
        # A missing/stale list cannot authorize using or replacing an empty state.
        for _ in range(max_catchup):
            number = self.revision + 1
            if self._create(number, "restart_barrier"):
                self.revision = number
                return
            self._read(number)
        self._unavailable = True
        raise ProbeUnavailable("snapshot catch-up limit reached")

    def kind(self):
        return "cos_probe"

    def today_summary(self, user_id, *, calories_only=False):
        result = super().today_summary(user_id, calories_only=calories_only)
        result["source"] = "cos_probe"
        return result

    def _key(self, revision):
        return f"{self.prefix}{revision:012d}.json"

    def snapshot(self):
        if self._unavailable:
            raise ProbeUnavailable("probe must be reopened after uncertain persistence")
        return {**{k: deepcopy(getattr(self, k)) for k in self.FIELDS},
                "seen_client_tokens": sorted(self.seen_client_tokens)}

    def _read(self, revision):
        self.metrics["read_attempts"] += 1
        body = self.objects.get(self._key(revision))
        if body is None:
            self._unavailable = True
            raise ProbeUnavailable("known snapshot not visible; retry by reopening, never reset")
        self.metrics["bytes_read"] += len(body)
        envelope = json.loads(body)
        if envelope["schema"] != "cos_probe.v1" or envelope["revision"] != revision:
            raise ProbeUnavailable("snapshot identity mismatch")
        for key in self.FIELDS:
            setattr(self, key, deepcopy(envelope["state"][key]))
        self.seen_client_tokens = set(envelope["state"]["seen_client_tokens"])
        self.revision = revision

    def _create(self, revision, reason):
        body = json.dumps({"schema": "cos_probe.v1", "revision": revision,
                           "reason": reason, "state": self.snapshot()},
                          ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        self.metrics["create_attempts"] += 1
        self.metrics["bytes_sent"] += len(body)
        try:
            return self.objects.create(self._key(revision), body)
        except BaseException:
            # A timeout may have committed. This in-memory instance is unusable.
            self._unavailable = True
            raise

    def _mutate(self, method, args, kwargs):
        if self._unavailable:
            raise ProbeUnavailable("probe must be reopened after uncertain persistence")
        outer = self._depth == 0
        before = self.snapshot() if outer else None
        self._depth += 1
        try:
            result = method(self, *args, **kwargs)
        except BaseException:
            self._unavailable = True
            raise
        finally:
            self._depth -= 1
        if outer and self.snapshot() != before:
            if not self._create(self.revision + 1, method.__name__):
                self._unavailable = True
                raise ProbeUnavailable("another writer exists; reopen before retry")
            self.revision += 1
        return result


def _persisted(method):
    def wrapped(self, *args, **kwargs):
        return self._mutate(method, args, kwargs)
    wrapped.__name__ = method.__name__
    return wrapped


for _name in ("sync_knowledge", "write_food", "write_intake", "undo_last", "create_pending",
              "mark_pending_status", "begin_execution", "save_execution", "append_usage_event",
              "revise_intake", "cache_food", "save_document", "save_session"):
    setattr(CosProbeStore, _name, _persisted(getattr(FakeStore, _name)))


def _readable(method):
    def wrapped(self, *args, **kwargs):
        if self._unavailable:
            raise ProbeUnavailable("probe must be reopened after uncertain persistence")
        return method(self, *args, **kwargs)
    return wrapped


for _name in ("today_summary", "summarize_history", "intake_items", "load_foods", "load_knowledge",
              "get_pending", "load_execution", "load_unfinished_executions", "load_document",
              "load_session", "list_documents", "last_intake"):
    setattr(CosProbeStore, _name, _readable(getattr(CosProbeStore, _name)))
