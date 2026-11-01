"""Production archive/migration checks with injected object and HTTP transports."""
from copy import deepcopy
from contextlib import contextmanager
import json
import threading
from types import SimpleNamespace
from unittest.mock import patch

from agent.storage import cos_archive
from agent.storage.bitable_adapter import BitableStore, _intake_field
from agent.storage.migration import baseline_manifest, import_cos_baseline


class Objects:
    def __init__(self):
        self.data = {}
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            return self.data.get(key)

    def list(self, prefix):
        with self.lock:
            return [key for key in self.data if key.startswith(prefix)]

    def create(self, key, body):
        with self.lock:
            if key in self.data:
                return False
            self.data[key] = body
            return True


def row(identity="item-a", kcal=105, status="valid"):
    return {"record_id": identity, "user_id": "user", "date": "2026-11-02", "message_id": "record-action",
            "raw_text": "记餐", "meal_type": "lunch", "food_id": "food-a", "food_name": "甲食物", "grams": kcal,
            "nutrition": {"carbs": 0, "protein": 0, "fat": 0, "kcal": kcal}, "snapshot": {}, "status": status, "created_at": 1000}


class Wire:
    def __init__(self):
        self.rows, self.tokens = {}, {}
        self.lose_create = False
        self.calls = []
        self.block = None

    def list(self, app, table, field_names=None, *, filter_formula=""):
        return [(rid, deepcopy(fields)) for rid, fields in self.rows.get(table, {}).items()]

    def create(self, table, records, client_token=""):
        self.calls.append(("create", table, deepcopy(records), client_token))
        if self.block:
            self.block[0].set()
            assert self.block[1].wait(5)
        if client_token not in self.tokens:
            rid = "rec-" + str(len(self.tokens) + 1)
            self.rows.setdefault(table, {})[rid] = deepcopy(records[0]["fields"])
            self.tokens[client_token] = rid
        if self.lose_create:
            self.lose_create = False
            raise TimeoutError("create response lost after commit")
        return {"code": 0}

    def update(self, table, rid, fields):
        self.calls.append(("update", table, rid, deepcopy(fields)))
        self.rows[table][rid].update(deepcopy(fields))
        return {"code": 0}


@contextmanager
def gateway(wire):
    bitable = BitableStore()
    bitable.app_token, bitable.intake_table, bitable.food_table, bitable.pending_table = "test-app", "intake", "foods", "pending"
    bitable._batch_create, bitable._update_record = wire.create, wire.update
    with patch.object(cos_archive, "_list_records", wire.list), patch.dict("os.environ", {"BITABLE_FIELD_INTAKE_DATE_VALUE_TYPE": "text"}):
        yield cos_archive.BitableArchive(bitable)


def _setup(change):
    from agent.storage.cos_journal import Journal
    objects = Objects()
    state = SimpleNamespace(objects=objects, prefix="archive-test/", journal=SimpleNamespace(state={"archive:intake:item-a": change}))
    return state, Journal(objects, "archive-test/sync/")


def QA01():
    """Archive response loss, coalescing, replay and soft delete preserve one latest row."""
    change = {"kind": "intake", "id": "item-a", "revision": 1, "value": row()}
    store, progress = _setup(change)
    wire = Wire()
    wire.lose_create = True
    with gateway(wire) as transport:
        assert cos_archive.archive_pending(store, gateway=transport, progress=progress)["failed"]
        original = deepcopy(progress.get("item:archive:intake:item-a")["inflight"])
        store.journal.state["archive:intake:item-a"] = {**change, "revision": 3, "value": row(kcal=210)}
        result = cos_archive.archive_pending(store, gateway=transport, progress=progress)
        assert not result["failed"] and [c["revision"] for c in result["confirmed"]] == [1, 3]
        assert len(wire.rows["intake"]) == 1
        assert progress.get("item:archive:intake:item-a")["ack_revision"] == 3
        assert original["client_token"] == wire.calls[0][3]
        before = len(wire.calls)
        cos_archive.archive_pending(store, gateway=transport, progress=progress)
        assert len(wire.calls) == before
        store.journal.state["archive:intake:item-a"] = {**change, "revision": 4, "value": row(kcal=210, status="deleted")}
        assert not cos_archive.archive_pending(store, gateway=transport, progress=progress)["failed"]
    saved = next(iter(wire.rows["intake"].values()))
    assert saved["agent_revision"] == 4 and saved[_intake_field("status")] == "deleted"
    assert saved[_intake_field("kcal")] == 210


def QA02():
    """Blocked archive transport does not hold the production message writer."""
    from agent.storage.cos_store import CosStore
    store = CosStore(objects=Objects(), prefix="archive-concurrency/")
    store.import_baseline({"schema": "calorie-agent.baseline.v1", "intake": [], "pending": [], "foods": [], "aliases": {}, "standards": [], "knowledge": []})
    store.write_intake({"message_id": "new-action", "user_id": "user", "text": "食物"},
                       [{"matched_name": "甲食物", "grams": 100, "nutrition": row()["nutrition"], "snapshot": {}, "date": "2026-11-02"}])
    readonly = CosStore(objects=store.objects, prefix="archive-concurrency/", readonly=True)
    started, release = threading.Event(), threading.Event()
    wire = Wire()
    wire.block = (started, release)
    failures = []
    with gateway(wire) as transport:
        def sync():
            try:
                cos_archive.archive_pending(readonly, gateway=transport)
            except BaseException as exc:
                failures.append(exc)
        thread = threading.Thread(target=sync)
        thread.start()
        try:
            assert started.wait(5)
            store.confirm_current()
            target = store.intake_items("user")[0]
            store.revise_intake("user", target["record_id"], {"grams": 50, "nutrition": {**target["nutrition"], "kcal": 52.5}, "snapshot": {}})
            assert store.intake_items("user")[0]["nutrition"]["kcal"] == 52.5
        finally:
            release.set()
            thread.join(5)
    assert not thread.is_alive() and not failures


def QA03():
    """Migration independently verifies old IDs, deleted rows, nutrition and task state."""
    from agent.storage.cos_store import CosStore
    data = {"schema": "calorie-agent.baseline.v1", "intake": [row("rec-old", 270), row("rec-deleted", 50, "deleted")],
            "foods": [], "knowledge": [], "aliases": {"old-user": "user"}, "standards": [], "pending": [
                {"record_id": "old-pending", "pending_id": "receipt-old", "intent": "message_received", "source_message_id": "old-message", "user_id": "user",
                 "chat_id": "chat", "status": "completed", "created_at": 1000, "expires_at": 0, "payload": {"delivery": {"sent": True}}, "food_name": "", "grams": None, "meal_type": "unknown", "raw_text": "old"}]}
    for item in data["intake"]:
        item["feishu_record_id"] = item["record_id"]
    store = CosStore(objects=Objects(), prefix="migration-test/")
    from harness.eval.personal_cases import A
    data["foods"] = [deepcopy(A)]
    result = import_cos_baseline(store, data)
    assert not any(k.startswith("archive:food:") for k in store.journal.state)
    assert result["verified"]["valid_count"] == 1 and result["verified"]["intake_count"] == 2
    assert set(r["record_id"] for r in store.intake_items("user", include_deleted=True)) == {"rec-old", "rec-deleted"}
    assert store.get_pending("receipt-old")["status"] == "completed"
    assert baseline_manifest(data)["daily_kcal"] == {"user:2026-11-02": "270"}
    store.import_baseline(data)  # Same immutable baseline is an idempotent retry.
    changed = deepcopy(data)
    changed["intake"][0]["grams"] += 1
    try:
        store.import_baseline(changed)
    except (ValueError, RuntimeError):
        pass
    else:
        raise AssertionError("nonempty baseline must not be replaced")


def QA04():
    """Permission/configuration failures block; throttling retains the exact inflight request."""
    change = {"kind": "intake", "id": "item-a", "revision": 1, "value": row()}
    for code, status in ((1254302, "blocked"), (1254003, "blocked"), (1254290, "retry"), (1254607, "retry")):
        store, progress = _setup(change)
        wire = Wire()
        def fail(table, records, client_token=""):
            raise RuntimeError("Failed to batch create Bitable records: " + str({"code": code}))
        wire.create = fail
        with gateway(wire) as transport:
            assert cos_archive.archive_pending(store, gateway=transport, progress=progress)["failed"]
        saved = progress.get("item:archive:intake:item-a")
        assert saved["status"] == status and saved["inflight"]["revision"] == 1


CHECKS = {key: globals()[key] for key in ("QA01", "QA02", "QA03", "QA04")}
