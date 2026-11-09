"""Paired first-model COS measurements; fixed old state, no cloud credentials.

Capture before changing Journal with ``python -m harness.eval.preflight_cos_cases
--measure reports/preflight-cos-before-20261104.json``. Run the same command for
after. Each RPC sleeps 1ms; intervals are measured, not summed across threads.
"""
from copy import deepcopy
from dataclasses import replace
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from types import ModuleType
from unittest.mock import patch

from agent.storage.cos_journal import Journal, CommitUnavailable, encode
from agent.storage.cos_store import CosStore
from agent.storage.execution_pipeline import Pipeline
from harness.eval.personal_cases import CONFIG, NativeFake
from harness.eval.production_pipeline_cases import event, message

PREFIX = "offline/preflight-cos/"
RPC_DELAY = .001


class TimedObjects:
    def __init__(self, data=None):
        self.data = deepcopy(data or {})
        self.events = []
        self.delay = 0
        self.lock = threading.Lock()
        self.stale_once = False
        self.hidden = set()

    def rpc(self, kind, key, operation):
        start = time.perf_counter()
        try:
            time.sleep(self.delay)
            return operation()
        finally:
            with self.lock:
                self.events.append({"kind": kind, "key": key, "start": start,
                                    "end": time.perf_counter()})

    def get(self, key):
        kind = "commit_get" if "/commits/" in key else "document_get"
        return self.rpc(kind, key, lambda: None if key in self.hidden else self.data.get(key))

    def create(self, key, body):
        def put():
            with self.lock:
                if key in self.data:
                    return False
                self.data[key] = body
                return True
        kind = "barrier" if "/commits/" in key and not json.loads(body)["changes"] else "create"
        return self.rpc(kind, key, put)

    def list(self, prefix, marker="", limit=None):
        if self.stale_once:
            self.stale_once = False
            return self.rpc("list", prefix, lambda: [])
        keys = sorted(key for key in self.data if key.startswith(prefix) and key > marker)
        keys = keys if limit is None else keys[:limit]
        # Model the production transport's per-page RPC, including an empty page.
        for offset in range(0, max(1, len(keys)), 1000):
            self.rpc("list", prefix, lambda: None)
            self.events[-1].update(marker=marker, limit=limit, returned_keys=len(keys[offset:offset + 1000]))
        return keys


def pad(journal, revision):
    while journal.revision < revision:
        assert journal._append({})


@patch("time.time", return_value=1793764800.0)
def fixture(history, _clock):
    objects = TimedObjects()
    store = CosStore(objects, PREFIX)
    store.import_baseline({"baseline_id": "preflight-fixed-v1", "aliases": {}})
    store.write_intake({"message_id": "prior-meal", "user_id": "user", "text": "旧餐"},
                       [{"date": "2026-11-04", "matched_name": "夹具A", "grams": 200,
                         "nutrition": {"carbs": 20, "protein": 4, "fat": 2, "kcal": 120}}])
    store.save_session("user", "test", {"messages": [], "refs": [], "fixture": "原会话"})
    inbox = Journal(objects, PREFIX + "ingress/")
    pad(store.journal, history)
    pad(inbox, history)
    return objects.data


def union_ms(events):
    merged = []
    for item in sorted(events, key=lambda e: e["start"]):
        if merged and item["start"] <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], item["end"])
        else:
            merged.append([item["start"], item["end"]])
    return round(sum(end - start for start, end in merged) * 1000, 3)


class FirstModel(BaseException):
    pass


def measure_one(history, warm):
    objects = TimedObjects(fixture(history))
    pipeline = Pipeline(objects, PREFIX)
    if warm:
        pipeline.inbox()
        pipeline.inbox(readonly=True)
        pipeline.store()
        pipeline.readonly_store()
    # Same confirmed historic state plus the same 64 externally committed
    # successors in each role. Warm caches remain at their original watermarks.
    for role in ("ingress", "message"):
        writer = Journal(objects, PREFIX + role + "/")
        pad(writer, history + 64)
    snapshot = hashlib.sha256(encode({k: v.decode() for k, v in sorted(objects.data.items())})).hexdigest()
    objects.events.clear()
    objects.delay = RPC_DELAY
    started = time.perf_counter()
    received = time.time()
    receipt = pipeline.accept(message("preflight-fixed", "今天多少热量？"), received_at=received)
    observed = {}

    def first_model(runtime, request, handler):
        # This is the existing middleware entry immediately before first step
        # allocation/projection, not an earlier artificial model timestamp.
        observed["first_model_ms"] = round((time.perf_counter() - started) * 1000, 3)
        observed["events"] = list(objects.events)
        observed["receipt"] = pipeline._inbox_reader.get("receipt:" + receipt["receipt_id"])
        observed["ledger"] = runtime.store.intake_items("user", "2026-11-04", "2026-11-05")
        observed["session"] = runtime.store.load_session("user", "test")
        raise FirstModel()

    def execute(msg, **kwargs):
        from agent import run
        return run(msg, model=NativeFake(), **kwargs)

    config = replace(CONFIG, preset="live")
    with patch.dict(os.environ, {"TRACE_ENABLED": "false"}), patch("agent.config.load_config", return_value=config), \
         patch("agent.graph.Runtime.wrap_model_call", first_model):
        try:
            pipeline.worker(event(receipt), execute=execute)
        except FirstModel:
            pass
        else:
            raise AssertionError("first model sentinel was not reached")
    events = observed.pop("events")
    for item in observed["ledger"]:
        item.pop("created_at", None)
    # Request timestamps are expected to differ; all persisted business content
    # and original identity/order must remain identical in the paired samples.
    observed["receipt"].pop("received_at")
    return {"history": history, "tail": 64, "warm": warm, "fixture_sha256": snapshot,
            "rpc_counts": {kind: sum(e["kind"] == kind for e in events)
                           for kind in ("list", "commit_get", "document_get", "barrier", "create")},
            "listed_keys": sum(e.get("returned_keys", 0) for e in events),
            "cos_union_ms": union_ms(events), **observed}


def paired_preflight():
    """相同旧状态+64尾段的冷暖首轮前计时、收件/账本/会话读回。"""
    for history in (1024, 10240):
        cold, warm = measure_one(history, False), measure_one(history, True)
        for field in ("receipt", "ledger", "session"):
            assert cold[field] == warm[field], field
        assert len(cold["ledger"]) == 1 and cold["ledger"][0]["nutrition"]["kcal"] == 120


def preflight_faults():
    """陈旧LIST、提交冲突和缺失检查点均不丢收件、不重复业务。"""
    objects = TimedObjects(fixture(1024))
    stale = Journal(objects, PREFIX + "message/")
    writer = Journal(objects, PREFIX + "message/")
    writer.commit({"receipt:accepted": {"sequence": 2}, "action:once": {"kcal": 120}})
    objects.stale_once = True
    stale.refresh()
    assert stale.get("receipt:accepted") == {"sequence": 2}
    assert stale.get("action:once") == {"kcal": 120}
    # Even after an optimistic LIST candidate, an adjacent writer wins; the
    # original action remains point-readable and cannot be executed twice.
    old = CosStore(objects, PREFIX)
    new = CosStore(objects, PREFIX)
    executions = []
    new.commit_action("conflict-fixed", lambda: executions.append(1) or {"kcal": 120})
    try:
        old.commit_action("conflict-fixed", lambda: {"kcal": 999})
    except CommitUnavailable:
        pass
    else:
        raise AssertionError("stale writer overwrote an adjacent commit")
    old.confirm_current()
    assert old.commit_action("conflict-fixed", lambda: executions.append(2)) == {"kcal": 120}
    assert executions == [1]
    # A known unreadable checkpoint must fail closed, never silently skip facts.
    key = new.journal.key(1024)
    objects.hidden.add(key)
    try:
        Journal(objects, PREFIX + "message/", readonly=True)
    except CommitUnavailable:
        pass
    else:
        raise AssertionError("unreadable checkpoint was accepted")

    # An accepted watermark outranks a stale LIST, including a read-only cold
    # reader. No confirmation writes are allowed in this role.
    objects.hidden.clear()
    objects.stale_once = True
    readonly = Journal(objects, PREFIX + "message/", readonly=True)
    readonly.refresh(confirm=False, minimum_revision=new.journal.revision)
    assert readonly.get("action:conflict-fixed") == {"kcal": 120}

    # The cold tail joins every read before applying state. A missing exact
    # successor must poison the view instead of publishing a partial checkpoint.
    for revision in (1024, 1088):
        tail = TimedObjects(fixture(1024))
        writer = Journal(tail, PREFIX + "message/")
        pad(writer, 1088)
        tail.hidden.add(writer.key(revision))
        try:
            Journal(tail, writer.prefix, readonly=True)
        except CommitUnavailable:
            pass
        else:
            raise AssertionError("unreadable cold checkpoint/tail was accepted")

    # Check the production SDK arguments and bounded pagination, not only the
    # injected in-memory implementation of the new object list contract.
    from agent.storage.cos_transport import CosObjects
    class Client:
        def __init__(self): self.calls = []
        def list_objects(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                return {"Contents": [{"Key": "p/b"}, {"Key": "p/c"}],
                        "IsTruncated": "true", "NextMarker": "p/c"}
            return {"Contents": [{"Key": "p/d"}], "IsTruncated": "false"}
    transport = CosObjects.__new__(CosObjects)
    transport.client, transport.bucket = Client(), "offline"
    assert transport.list("p/", marker="p/a", limit=3) == ["p/b", "p/c", "p/d"]
    assert [(c["Marker"], c["MaxKeys"]) for c in transport.client.calls] == [("p/a", 3), ("p/c", 1)]


CHECKS = {"PC01": paired_preflight, "PC02": preflight_faults}


def main():
    parser = argparse.ArgumentParser()
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--measure", type=Path)
    selection.add_argument("--paired", type=Path)
    args = parser.parse_args()
    from agent.storage import cos_journal, cos_transport
    # Warm imports exclude interpreter/module startup from both paired builds.
    import agent.graph
    import agent.config
    # Tokenizer loading is process-global and lazy; warm it symmetrically too,
    # so the first before-sample cannot include a one-time encoding load.
    from agent.context import tokens
    tokens("paired measurement warmup")
    if args.paired:
        # Preserve/replay the actual pre-change code, rather than a rewritten
        # approximation. All other production consumers remain the same build.
        baseline = "3497e9b8e0f81635e779cb1f1e82ecd986947209"
        source = subprocess.check_output(["git", "show", baseline + ":LangGraph_Agent/agent/storage/cos_journal.py"])
        legacy = ModuleType("preflight_baseline_journal")
        exec(compile(source, "preflight_baseline_journal.py", "exec"), legacy.__dict__)
        root = Path(__file__).resolve().parents[2]
        def consumer_hashes():
            return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in sorted((root / "agent").rglob("*.py"))}
        consumers = consumer_hashes()
        pairs = []
        for history in (1024, 10240):
            for warm in (False, True):
                with patch(__name__ + ".Journal", legacy.Journal), \
                     patch("agent.storage.cos_journal.Journal", legacy.Journal), \
                     patch("agent.storage.cos_store.Journal", legacy.Journal):
                    before = measure_one(history, warm)
                after = measure_one(history, warm)
                for field in ("fixture_sha256", "receipt", "ledger", "session"):
                    assert before[field] == after[field], field
                if history == 10240:
                    assert after["cos_union_ms"] < before["cos_union_ms"]
                    assert after["first_model_ms"] < before["first_model_ms"]
                if warm:
                    assert after["rpc_counts"]["list"] == 4
                pairs.append({"before": before, "after": after})
        assert consumers == consumer_hashes(), "consumer sources changed during paired measurement"
        output = {"trace_enabled": False, "rpc_delay_ms": RPC_DELAY * 1000,
                  "baseline_commit": baseline, "baseline_source_sha256": hashlib.sha256(source).hexdigest(),
                  "consumer_sha256": consumers, "pairs": pairs,
                  "measurement": "accept/original received_at to existing Runtime.wrap_model_call entry; no model call or verification reads included",
                  "limitations": ["fixed-delay in-memory COS, not real cloud latency", "local imports preloaded; cold means cold COS journals", "no queue delay; first model function intercepted before step preparation", "received_at varies by sample; normalized original receipt content, ledger and session compared exactly"]}
        with args.paired.open("x", encoding="utf-8") as target:
            json.dump(output, target, ensure_ascii=False, indent=2)
        print(args.paired)
        return
    samples = [measure_one(history, warm) for history in (1024, 10240) for warm in (False, True)]
    output = {"trace_enabled": False, "rpc_delay_ms": RPC_DELAY * 1000,
              "measurement": "original intake/accept through Runtime.wrap_model_call entry; excludes model and verification reads",
              "sources": {Path(module.__file__).name: hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                          for module in (cos_journal, cos_transport)}, "samples": samples}
    with args.measure.open("x", encoding="utf-8") as target:
        json.dump(output, target, ensure_ascii=False, indent=2)
    print(args.measure)


if __name__ == "__main__":
    main()
