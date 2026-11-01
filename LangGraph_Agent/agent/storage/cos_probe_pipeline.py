"""Isolated COS pipeline. Each of ingress, message and sync has ONE writer.

No production entry imports this module. COS events may repeat or arrive out of
order. Immutable inbox journals contain every earlier receipt, and sync progress
is a separate immutable sequence. LIST is only a hint; conflicts require an exact
GET and an invisible committed object fails closed. No keys may be deleted.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import json
import time
import uuid

from .cos_probe import CosProbeStore, ProbeUnavailable
from .bitable_adapter import reply_uuid

USER, CHAT = "cos-probe-user", "cos-probe-session"
SCOPES = {"pipeline-validation", "hard-restart", "idle-pipeline-validation"}
INSTANCE_ID = uuid.uuid4().hex


def _root(scope):
    if scope not in SCOPES:
        raise ValueError("unknown isolated pipeline scope")
    return "cos-probe/pipeline/" + scope + "/"


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _read(objects, key):
    body = objects.get(key)
    if body is None:
        raise ProbeUnavailable("known pipeline object not visible: " + key)
    return json.loads(body)


def _fixed(objects, key, value):
    if objects.create(key, _encode(value)):
        return value
    return _read(objects, key)


class _Journal:
    """Small single-writer append/read barrier, distinct from business snapshots."""
    def __init__(self, objects, prefix, state):
        self.objects, self.prefix, self.state, self.revision = objects, prefix, state, -1
        numbers = [int(k[len(prefix):-5]) for k in objects.list(prefix)
                   if k.startswith(prefix) and k.endswith(".json")
                   and len(k[len(prefix):-5]) == 12 and k[len(prefix):-5].isdigit()]
        if numbers:
            self._load(max(numbers))
        for _ in range(1000):
            if self._append(self.state):
                return
            self._load(self.revision + 1)
        raise ProbeUnavailable("pipeline journal catch-up limit reached")

    def key(self, number=None):
        return f"{self.prefix}{self.revision if number is None else number:012d}.json"

    def _load(self, number):
        value = _read(self.objects, self.key(number))
        if value.get("schema") != "cos_pipeline.journal.v1" or value["revision"] != number:
            raise ProbeUnavailable("pipeline journal identity mismatch")
        self.state, self.revision = value["state"], number

    def _append(self, state):
        number = self.revision + 1
        if not self.objects.create(self.key(number), _encode({"schema": "cos_pipeline.journal.v1",
                "reason": "pipeline_journal", "revision": number, "state": state})):
            return False
        self.state, self.revision = deepcopy(state), number
        return True

    def commit(self, state):
        if not self._append(state):
            raise ProbeUnavailable("another pipeline journal writer exists")


def _event_key(event, kind):
    key = event.get("key", "")
    if not isinstance(key, str) or not key.startswith("cos-probe/pipeline/"):
        raise ValueError("an exact isolated object key is required")
    parts = key.split("/")
    scope = parts[2]
    root = _root(scope)
    if not key.startswith(root + kind + "/") or not key.endswith(".json"):
        raise ValueError("unexpected pipeline event prefix")
    return scope, root, key


def ingress(payload, objects, *, scope="pipeline-validation", now=None):
    """Persist a Feishu-shaped text event before ACK; caller verifies HTTP auth.

    Sender and chat in the payload never select ledger or delivery identities.
    A duplicate message_id retains the first receipt, date, text and sequence.
    This isolated endpoint is not a replacement for the production webhook.
    """
    from app import _parse_text_message
    root = _root(scope)
    parsed = _parse_text_message(payload)
    if not parsed or not parsed["message_id"] or len(parsed["message_id"]) > 200:
        raise ValueError("a text message with a bounded message_id is required")
    received = time.time() if now is None else now
    receipt_key = root + "receipts/" + hashlib.sha256(parsed["message_id"].encode()).hexdigest() + ".json"
    date = datetime.fromtimestamp(received, timezone(timedelta(hours=8))).date().isoformat()
    receipt = _fixed(objects, receipt_key, {"schema": "cos_pipeline.receipt.v1", "reason": "receipt",
        "scope": scope, "received_at": received, "intake_date": date,
        "message": {**parsed, "user_id": USER, "chat_id": CHAT}})
    # Progress barriers do not notify the message function. Publish one inbox
    # object only after the accepted receipt sequence has durably committed.
    journal = _Journal(objects, root + "ingress-progress/", {"receipts": []})
    entries = journal.state["receipts"]
    if receipt_key not in entries:
        journal.commit({"receipts": entries + [receipt_key]})
    inbox_key = root + f"inbox/{journal.revision:012d}.json"
    _fixed(objects, inbox_key, {"schema": "cos_pipeline.journal.v1", "reason": "inbox",
                              "revision": journal.revision, "state": journal.state})
    return {"ack": True, "inbox_key": inbox_key, "receipt_key": receipt_key,
            "received_at": receipt["received_at"], "intake_date": receipt["intake_date"]}


def _execute(message, store, intake_date):
    from agent import run
    from agent.config import load_config
    return run(message, store=store, config=replace(load_config(), preset="live"), intake_date=intake_date)


def _fixtures():
    from harness.eval.personal_cases import A, B
    foods = deepcopy([A, B])
    foods[1].update(units=[{"unit": "个", "weight": 60, "eat_weight": 50}], detail_loaded=True)
    return foods


def result_key(message_id, *, scope="pipeline-validation"):
    """Deterministic driver-only read; never opens the business Store."""
    return _root(scope) + "results/" + hashlib.sha256(message_id.encode()).hexdigest() + ".json"


def consumed_key(inbox_key, *, scope):
    root = _root(scope)
    if not inbox_key.startswith(root + "inbox/") or not inbox_key.endswith(".json"):
        raise ValueError("an inbox from the same isolated scope is required")
    return root + "consumed/" + hashlib.sha256(inbox_key.encode()).hexdigest() + ".json"


def _audit(objects, store):
    raw = _read(objects, store._key(store.revision))["state"]
    valid = [r for r in raw["intake"] if r["status"] != "deleted"]
    return {"count": len(valid), "kcal": sum(r["nutrition"]["kcal"] for r in valid),
            "items": [{k: r[k] for k in ("record_id", "food_name", "grams", "date", "nutrition")} for r in valid]}


class _WriteFailpoint:
    def __init__(self, objects, root, hard_exit):
        self.objects, self.root, self.hard_exit = objects, root, hard_exit

    def __getattr__(self, name):
        return getattr(self.objects, name)

    def create(self, key, body):
        created = self.objects.create(key, body)
        if created and key.startswith(self.root + "ledger/") and json.loads(body).get("reason") == "write_intake":
            if self.hard_exit is None:
                raise ProbeUnavailable("hard-restart requires an explicit isolated execution exit callback")
            marker = {"reason": "hard_exit", "snapshot_key": key, "at": time.time(), "instance_id": INSTANCE_ID}
            if self.objects.create(self.root + "failpoints/after-write.json", _encode(marker)):
                self.hard_exit()
                raise RuntimeError("hard_exit callback returned")
        return created


def message_worker(event, objects, *, execute=None, send=None, hard_exit=None):
    """Consume all receipts through an immutable inbox event, oldest first.

    execute(message, store, intake_date)->run result. send(reply, uuid)->message_id.
    Send must use one configured test chat and Feishu UUID deduplication. Outbox
    creation is the async dispatch: this function never calls or waits for sync.
    """
    if send is None:
        raise ValueError("an explicit isolated delivery callback is required")
    scope, root, key = _event_key(event, "inbox")
    inbox = _read(objects, key)
    if inbox.get("schema") != "cos_pipeline.journal.v1":
        raise ValueError("unknown inbox schema")
    receipt_keys = inbox["state"]["receipts"]
    # Resolve every predecessor before performing any business work. Missing
    # committed receipts cannot become a successful empty/gapped consumption.
    receipts = []
    for receipt_key in receipt_keys:
        if not receipt_key.startswith(root + "receipts/"):
            raise ValueError("receipt escaped isolated scope")
        receipt = _read(objects, receipt_key)
        if receipt.get("scope") != scope or receipt["message"]["user_id"] != USER or receipt["message"]["chat_id"] != CHAT:
            raise ValueError("non-probe receipt identity refused")
        receipts.append(receipt)
    if not receipts:
        return {"scope": scope, "outbox_keys": [], "timings": [], "empty_barrier": True}
    transport = _WriteFailpoint(objects, root, hard_exit) if scope == "hard-restart" else objects
    store = CosProbeStore(transport, root + "ledger/", foods=_fixtures(),
                          today=receipts[-1]["intake_date"] if receipts else "2026-11-01")
    completed = store.load_document("probe_pipeline", USER, scope) or {"done": []}
    outboxes, timings = [], []
    for receipt in receipts:
        message = receipt["message"]
        mid = message["message_id"]
        outbox_key = root + "outbox/" + hashlib.sha256(mid.encode()).hexdigest() + ".json"
        if mid in completed["done"]:
            _read(objects, result_key(mid, scope=scope))
            outboxes.append(outbox_key)
            continue
        attempt = uuid.uuid4().hex
        start = time.time()
        evidence = {"reason": "message_evidence", "message_id": mid, "received_at": receipt["received_at"],
                    "started_at": start, "instance_id": INSTANCE_ID, "attempt": attempt}
        objects.create(root + "evidence/message/" + attempt + "-started.json", _encode(evidence))
        result = (execute or _execute)(message, store, receipt["intake_date"])
        if result.get("status") != "ok":
            raise RuntimeError("isolated message execution failed: " + str(result.get("errors")))
        # Outbox points only at a fully committed immutable ledger revision.
        outbox = _fixed(objects, outbox_key, {"schema": "cos_pipeline.outbox.v1", "reason": "outbox",
            "scope": scope, "revision": store.revision, "snapshot_key": store._key(store.revision),
            "message_id": mid, "created_at": time.time()})
        delivery = store.load_document("probe_delivery", USER, mid)
        if not delivery:
            delivery = {"reply": result["reply"], "uuid": reply_uuid(message), "sent": False}
            store.save_document("probe_delivery", USER, mid, delivery)
        if not delivery["sent"]:
            sent_id = send(delivery["reply"], delivery["uuid"])
            sent_at = time.time()
            delivery.update(sent=True, message_id=sent_id, sent_at=sent_at)
            store.save_document("probe_delivery", USER, mid, delivery)
        evidence.update(completed_at=time.time(), reply_sent_at=delivery["sent_at"],
                        revision=outbox["revision"], outbox_key=outbox_key,
                        reply_elapsed_ms=round((delivery["sent_at"] - receipt["received_at"]) * 1000))
        objects.create(root + "evidence/message/" + attempt + "-completed.json", _encode(evidence))
        _fixed(objects, result_key(mid, scope=scope), {"reason": "message_result", "completed": True,
            "message_id": mid, "scope": scope, "timing": evidence, "outbox_key": outbox_key,
            "snapshot_key": store._key(store.revision), "audit": _audit(objects, store)})
        completed["done"].append(mid)
        store.save_document("probe_pipeline", USER, scope, completed)
        outboxes.append(outbox_key)
        timings.append(evidence)
    audit = _audit(objects, store)
    _fixed(objects, consumed_key(key, scope=scope), {"reason": "inbox_consumed", "inbox_key": key,
        "scope": scope, "completed_at": time.time(), "instance_id": INSTANCE_ID,
        "snapshot_key": store._key(store.revision), "audit": audit})
    return {"scope": scope, "outbox_keys": outboxes, "timings": timings, "revision": store.revision,
            "audit": audit}


def _verify_projection(state, rows, scope):
    prefix = "cos-probe-projection:" + scope + ":"
    expected = {prefix + r["record_id"]: r for r in state["intake"] if r["status"] != "deleted"}
    actual = {r["message_id"]: r for r in rows if r["status"] != "deleted"}
    if len(actual) != len([r for r in rows if r["status"] != "deleted"]) or set(actual) != set(expected):
        raise ProbeUnavailable("independent projection identity/count check failed")
    for identity, row in expected.items():
        other = actual[identity]
        if any(other[k] != row[k] for k in ("food_name", "grams", "nutrition", "date")):
            raise ProbeUnavailable("independent projection value check failed")


def sync_worker(event, objects, *, project):
    """Separate single writer; resume inflight absolute projection before advance.

    project(state, scope)->independently read-back rows. A failed/ambiguous call
    retains inflight and must be retried successfully before any newer version.
    This worker NEVER opens CosProbeStore or writes the message ledger namespace.
    """
    scope, root, key = _event_key(event, "outbox")
    if scope == "hard-restart":
        raise ValueError("hard-restart scope is not projected to Feishu")
    outbox = _read(objects, key)
    if outbox.get("schema") != "cos_pipeline.outbox.v1" or outbox.get("scope") != scope:
        raise ValueError("outbox identity mismatch")
    progress = _Journal(objects, root + "sync-progress/", {"ack_revision": -1, "inflight": None})
    rows, done = None, []
    for candidate in [progress.state["inflight"], outbox]:
        if candidate is None or candidate["revision"] <= progress.state["ack_revision"]:
            continue
        revision = candidate["revision"]
        expected_key = root + f"ledger/{revision:012d}.json"
        if candidate["snapshot_key"] != expected_key:
            raise ValueError("snapshot escaped isolated committed ledger")
        envelope = _read(objects, expected_key)
        if envelope.get("schema") != "cos_probe.v1" or envelope["revision"] != revision:
            raise ValueError("committed snapshot identity mismatch")
        progress.commit({"ack_revision": progress.state["ack_revision"], "inflight": candidate})
        started = time.time()
        evidence_key = root + "evidence/sync/" + uuid.uuid4().hex
        evidence = {"reason": "sync_evidence", "revision": revision, "started_at": started,
                    "instance_id": INSTANCE_ID, "snapshot_key": expected_key}
        objects.create(evidence_key + "-started.json", _encode(evidence))
        rows = project(envelope["state"], scope)
        _verify_projection(envelope["state"], rows, scope)
        progress.commit({"ack_revision": revision, "inflight": None})
        evidence.update(completed_at=time.time(), rows=rows)
        objects.create(evidence_key + "-completed.json", _encode(evidence))
        done.append(revision)
    return {"scope": scope, "ack_revision": progress.state["ack_revision"], "synced": done, "rows": rows}
