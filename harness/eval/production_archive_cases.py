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

    def list(self, prefix, marker='', limit=None):
        with self.lock:
            keys = sorted(key for key in self.data if key.startswith(prefix) and key > marker)
            return keys if limit is None else keys[:limit]

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


def QA05():
    """Archive comparisons preserve source decimals, integer dates/versions and one-decimal intake values without false matches."""
    same = cos_archive._same_fields
    for cell in (3.26, "3.26", ["3.26"], [{"text": "3.26"}], {"value": "3.26"},
                 {"text": 3.26}, {"number": "3.260"}, {"value": [{"number": "3.26"}]}):
        assert same({"fat_per_100g": cell}, {"fat_per_100g": 3.26}), cell
    assert same({"protein_per_100g": "27.96"}, {"protein_per_100g": 27.96})
    for actual, expected in (("3.3", 3.26), ("27.9", 27.96), ("3.261", 3.26),
                             ("27.961", 27.96), ("52.51", 52.5)):
        assert not same({"n": actual}, {"n": expected}), (actual, expected)
    for invalid in (None, "", "not-a-number", "1_000", True, False, float("nan"), float("inf"),
                    "-Infinity", "NaN", [], [3.26, 4], {"value": "bad", "text": "3.26"}):
        assert not same({"n": invalid}, {"n": 3.26}), invalid
    assert not same({"n": None}, {"n": float("nan")})
    assert not same({"n": "Infinity"}, {"n": float("inf")})
    assert same({"date": [{"value": "1793548800000"}], "agent_revision": {"number": "12.0"},
                 "grams": "52.5", "kcal": 52.5},
                {"date": 1793548800000, "agent_revision": 12, "grams": 52.5, "kcal": 52.5})
    # Large integer revisions must not be collapsed by conversion to float.
    assert not same({"agent_revision": "9007199254740993"}, {"agent_revision": 9007199254740992})
    from agent.storage.timeutil import extract_number, round_number
    assert extract_number("3.26") == 3.3 and round_number(27.96) == 28


def QA06():
    """Precise source readback after a lost create ACK only reads; mismatched fields never advance the watermark."""
    source = {"food_id": "boohee-precise", "name": "真实精度来源夹具", "aliases": [],
              "carbs_per_100g": 0, "protein_per_100g": 27.96, "fat_per_100g": 3.26,
              "kcal_per_100g": 141, "source": "boohee", "boohee_id": "fixture-code", "detail_loaded": True}
    change = {"kind": "food", "id": source["food_id"], "revision": 1, "value": source}
    store, progress = _setup(change)
    key = "archive:food:" + source["food_id"]
    store.journal.state = {key: change}
    wire = Wire()
    wire.lose_create = True
    with gateway(wire) as transport:
        assert cos_archive.archive_pending(store, gateway=transport, progress=progress)["failed"] == [key]
        saved = progress.get("item:" + key)
        assert saved.get("ack_revision", -1) == -1 and saved["inflight"]["revision"] == 1
        # Simulate the real Feishu cells: values were persisted with all source
        # precision despite the lost response, and are returned as numeric text.
        actual = next(iter(wire.rows["foods"].values()))
        request = deepcopy(saved["inflight"])
        fat = next(k for k, v in request["fields"].items() if v == 3.26)
        protein = next(k for k, v in request["fields"].items() if v == 27.96)
        actual[fat], actual[protein] = "3.26", [{"text": "27.96"}]
        before = deepcopy(wire.calls)
        resumed = cos_archive.archive_pending(store, gateway=transport, progress=progress)
        assert resumed["failed"] == [] and resumed["confirmed"][0]["revision"] == 1
        assert wire.calls == before and len(wire.rows["foods"]) == 1
        assert progress.get("item:" + key)["ack_revision"] == 1

    # A transport which acknowledges an update but still returns an unequal
    # source value cannot confirm it, even when one-decimal rounding would hide
    # the difference. The fixed request remains retryable and unacknowledged.
    store, progress = _setup(change)
    store.journal.state = {key: change}
    wire = Wire()
    with gateway(wire) as transport:
        request = transport.prepare(change)
        wire.rows["foods"] = {"existing": {**request["fields"], fat: "3.261", protein: "27.961"}}
        with patch.object(transport.store, "_update_record", return_value={"code": 0}):
            result = cos_archive.archive_pending(store, gateway=transport, progress=progress)
        assert result["failed"] == [key] and not result["confirmed"]
        saved = progress.get("item:" + key)
        assert saved.get("ack_revision", -1) == -1 and saved["status"] == "retry" and saved["inflight"] == request
        assert wire.calls == []

    intake = {"kind": "intake", "id": "item-a", "revision": 12, "value": row(kcal=52.5)}
    wire = Wire()
    with gateway(wire) as transport:
        request = transport.prepare(intake)
        wire.rows["intake"] = {"existing": {**request["fields"], "agent_revision": [{"number": "13"}]}}
        assert transport.apply_and_readback(request) == {"record_id": "existing", "superseded_by": 13}
        assert wire.calls == []
        # A fractional revision must not round into a false newer-version ACK.
        wire.rows["intake"]["existing"]["agent_revision"] = "12.96"
        with patch.object(transport.store, "_update_record", return_value={"code": 0}):
            try:
                transport.apply_and_readback(request)
            except RuntimeError as exc:
                assert "readback" in str(exc)
            else:
                raise AssertionError("fractional revision incorrectly confirmed")
        assert wire.calls == []


def QA07():
    """New archive tokens isolate destination/version while identical preparations and UUIDv4 form remain stable."""
    from uuid import UUID
    from agent.storage.bitable_adapter import stable_uuid
    change = {"kind": "food", "id": "shared-food-id", "revision": 7,
              "value": {"food_id": "shared-food-id", "name": "来源食物", "carbs_per_100g": 1,
                        "protein_per_100g": 27.96, "fat_per_100g": 3.26}}
    wire = Wire()
    with gateway(wire) as transport:
        first = transport.prepare(change)
        assert transport.prepare(deepcopy(change)) == first
        assert UUID(first["client_token"]).version == 4
        next_version = transport.prepare({**change, "revision": 8})
        assert next_version["client_token"] != first["client_token"]
        transport.store.food_table = "foods-other"
        other_table = transport.prepare(change)
        transport.store.app_token = "other-app"
        other_app = transport.prepare(change)
        assert len({r["client_token"] for r in (first, next_version, other_table, other_app)}) == 4
        transport.store.app_token = "test-app"
        assert transport.prepare(change) == other_table

        # A deliberately global-token fake demonstrates the old formula's
        # collision. This fixture does not assert Feishu's actual token scope.
        legacy_token = stable_uuid("calorie-agent:archive:food:shared-food-id")
        transport.apply_and_readback({**first, "client_token": legacy_token})
        try:
            transport.apply_and_readback({**other_table, "client_token": legacy_token})
        except RuntimeError as exc:
            assert "readback" in str(exc)
        else:
            raise AssertionError("global-token fixture did not expose old destination collision")
        assert len(wire.rows["foods"]) == 1 and not wire.rows.get("foods-other")
    wire = Wire()
    with gateway(wire) as transport:
        transport.apply_and_readback(first)
        transport.apply_and_readback(other_table)
        assert len(wire.rows["foods"]) == len(wire.rows["foods-other"]) == 1
        assert len(wire.calls) == 2 and all(c[0] == "create" for c in wire.calls)
        before = deepcopy(wire.calls)
        transport.apply_and_readback(first)
        transport.apply_and_readback(other_table)
        assert wire.calls == before


def QA08():
    """Legacy inflight recovery uses its original fields/token, reads before retry, and never rotates an uncertain request."""
    from agent.storage.bitable_adapter import stable_uuid
    change = {"kind": "intake", "id": "item-a", "revision": 1, "value": row(kcal=52.5)}
    store, progress = _setup(change)
    wire = Wire()
    wire.lose_create = True
    with gateway(wire) as transport:
        current = transport.prepare(change)
        legacy = {**current, "client_token": stable_uuid("calorie-agent:archive:intake:item-a")}
        assert current["client_token"] != legacy["client_token"]
        with patch.object(transport, "prepare", return_value=deepcopy(legacy)):
            failed = cos_archive.archive_pending(store, gateway=transport, progress=progress)
        assert failed["failed"] == ["archive:intake:item-a"]
        state = progress.get("item:archive:intake:item-a")
        assert state["inflight"] == legacy and state.get("ack_revision", -1) == -1
        assert wire.calls[0][3] == legacy["client_token"]
        before = deepcopy(wire.calls)

        # An unavailable read cannot authorize a new create or a new token.
        with patch.object(transport, "prepare", side_effect=AssertionError("legacy request must not be prepared again")), \
             patch.object(transport, "_find", side_effect=TimeoutError("readback unavailable")):
            failed = cos_archive.archive_pending(store, gateway=transport, progress=progress)
        assert failed["failed"] == ["archive:intake:item-a"] and wire.calls == before
        state = progress.get("item:archive:intake:item-a")
        assert state["inflight"] == legacy and state.get("ack_revision", -1) == -1

        # Under the new prepare implementation, the lost-ACK legacy request is
        # resolved solely by its persisted identity/fields. No write is replayed.
        with patch.object(transport, "prepare", side_effect=AssertionError("legacy request must not be prepared again")):
            confirmed = cos_archive.archive_pending(store, gateway=transport, progress=progress)
        assert not confirmed["failed"] and confirmed["confirmed"][0]["revision"] == 1
        assert wire.calls == before and len(wire.rows["intake"]) == 1
        state = progress.get("item:archive:intake:item-a")
        assert state["ack_revision"] == 1 and state["inflight"] is None


CHECKS = {key: globals()[key] for key in ("QA01", "QA02", "QA03", "QA04", "QA05", "QA06", "QA07", "QA08")}
