"""Offline pipeline acceptance; real run/tools, native fake model, no cloud claims."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading

from agent import run
from agent.storage.cos_probe import ProbeUnavailable
from agent.storage.cos_probe_pipeline import ingress, message_worker, sync_worker, result_key, consumed_key
from harness.eval.personal_cases import CONFIG, MEAL, NativeFake, call, answer


class Objects:
    def __init__(self):
        self.data, self.hidden, self.stale_list = {}, set(), False
        self.lock = threading.Lock()

    def get(self, key):
        with self.lock:
            return None if key in self.hidden else self.data.get(key)

    def list(self, prefix):
        with self.lock:
            return [] if self.stale_list else [k for k in self.data if k.startswith(prefix)]

    def create(self, key, body):
        with self.lock:
            if key in self.data:
                return False
            self.data[key] = body
            return True


class FileObjects:
    """Only used in an explicitly spawned child process hard-exit test."""
    def __init__(self, directory):
        self.directory = Path(directory)

    def get(self, key):
        path = self.directory / key
        return path.read_bytes() if path.exists() else None

    def list(self, prefix):
        return [p.relative_to(self.directory).as_posix() for p in self.directory.rglob("*.json")
                if p.relative_to(self.directory).as_posix().startswith(prefix)]

    def create(self, key, body):
        path = self.directory / key
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with path.open("xb") as stream:
                stream.write(body)
            return True
        except FileExistsError:
            return False


def _payload(mid, text):
    return {"header": {"event_type": "im.message.receive_v1"}, "event": {
        "sender": {"sender_id": {"open_id": "external-input-must-not-select-user"}},
        "message": {"message_id": mid, "chat_id": "external-input-must-not-select-chat",
                    "message_type": "text", "content": json.dumps({"text": text})}}}


def _execute(message, store, intake_date):
    if "改成" in message["text"]:
        step = call("revise_intake", {"food_query": "夹具A", "grams": 100})
    elif "合计" in message["text"]:
        step = call("query_today", {})
    else:
        step = call("record_meal", MEAL)
    existing = store.load_execution(message)
    responses = [answer] if existing and existing["payload"].get("calls") else [step, answer]
    return run(message, store=store, config=replace(CONFIG, preset="live"), intake_date=intake_date,
               model=NativeFake(responses=responses))


def _project(state, scope):
    return [{**deepcopy(r), "message_id": "cos-probe-projection:" + scope + ":" + r["record_id"]}
            for r in state["intake"]]


def C03():
    """独立同步阻塞期间继续改量/查询；乱序、重复、不可见与同步中断恢复。"""
    objects, sent = Objects(), []
    deliver = lambda reply, uuid: sent.append(uuid) or uuid
    # Fixed date is derived once and retained even across later-day replay.
    now = 1793505600
    reordered = Objects()
    before = ingress(_payload("ordered-record", "记录午餐：夹具A200克，夹具B2个。"), reordered, now=now)
    after = ingress(_payload("ordered-revise", "刚才夹具A改成100克。"), reordered, now=now+1)
    consumed = message_worker({"key": after["inbox_key"]}, reordered, execute=_execute, send=lambda reply, uuid: uuid)
    assert consumed["audit"]["count"] == 2 and consumed["audit"]["kcal"] == 210
    old_event = message_worker({"key": before["inbox_key"]}, reordered, execute=_execute, send=lambda reply, uuid: uuid)
    assert old_event["audit"]["kcal"] == 210
    assert len([k for k in reordered.data if "/inbox/" in k]) == 2
    first = ingress(_payload("pipeline-record", "记录午餐：夹具A200克，夹具B2个。"), objects, now=now)
    recorded = message_worker({"key": first["inbox_key"]}, objects, execute=_execute, send=deliver)
    assert recorded["audit"]["count"] == 2 and recorded["audit"]["kcal"] == 270
    entered, release = threading.Event(), threading.Event()
    failures, slow_result = [], []

    def slow_project(state, scope):
        entered.set()
        assert release.wait(10), "test did not release slow sync"
        return _project(state, scope)

    def background():
        try:
            slow_result.append(sync_worker({"key": recorded["outbox_keys"][0]}, objects, project=slow_project))
        except BaseException as exc:
            failures.append(exc)

    thread = threading.Thread(target=background)
    thread.start()
    assert entered.wait(10)
    try:
        revised_in = ingress(_payload("pipeline-revise", "刚才夹具A改成100克，夹具B不变。"), objects, now=now+1)
        revised = message_worker({"key": revised_in["inbox_key"]}, objects, execute=_execute, send=deliver)
        query_in = ingress(_payload("pipeline-query", "今天摄入合计多少千卡？"), objects, now=now+2)
        queried = message_worker({"key": query_in["inbox_key"]}, objects, execute=_execute, send=deliver)
        assert not release.is_set() and thread.is_alive()
        assert revised["audit"]["kcal"] == queried["audit"]["kcal"] == 210
        assert len(sent) == 3
        result = json.loads(objects.get(result_key("pipeline-query")))
        assert result["audit"]["kcal"] == 210 and result["completed"]
        assert [(r["food_name"], r["grams"]) for r in result["audit"]["items"]] == [("夹具A", 100), ("夹具B", 100)]
    finally:
        release.set()
        thread.join(10)
    assert not thread.is_alive() and not failures, failures
    projected = []
    def project(state, scope):
        projected.append(sum(r["nutrition"]["kcal"] for r in state["intake"]))
        return _project(state, scope)
    # Deliver newest event first, then old events. They cannot overwrite 210.
    newest = sync_worker({"key": queried["outbox_keys"][-1]}, objects, project=project)
    older = sync_worker({"key": revised["outbox_keys"][-1]}, objects, project=project)
    oldest = sync_worker({"key": recorded["outbox_keys"][0]}, objects, project=project)
    assert projected == [210] and newest["ack_revision"] == older["ack_revision"] == oldest["ack_revision"]
    objects.stale_list = True
    replay_in = ingress(_payload("pipeline-record", "changed duplicate must be ignored"), objects, now=now+86400)
    assert replay_in["intake_date"] == first["intake_date"]
    replay = message_worker({"key": replay_in["inbox_key"]}, objects, execute=_execute, send=deliver)
    assert replay["audit"]["count"] == 2 and replay["audit"]["kcal"] == 210 and len(sent) == 3
    consumed = json.loads(objects.get(consumed_key(replay_in["inbox_key"], scope="pipeline-validation")))
    assert consumed["inbox_key"] == replay_in["inbox_key"]
    assert consumed["audit"]["count"] == 2 and consumed["audit"]["kcal"] == 210
    assert all(r["date"] == first["intake_date"] for r in replay["audit"]["items"])

    # Failure after remote projection but before ack leaves an inflight version.
    interrupted = Objects()
    a = ingress(_payload("interrupted-record", "记录午餐：夹具A200克，夹具B2个。"), interrupted, now=now)
    ar = message_worker({"key": a["inbox_key"]}, interrupted, execute=_execute, send=deliver)
    def fail(state, scope):
        _project(state, scope)
        raise OSError("lost response after remote write")
    try:
        sync_worker({"key": ar["outbox_keys"][-1]}, interrupted, project=fail)
    except OSError:
        pass
    else:
        raise AssertionError("sync interruption was not reached")
    b = ingress(_payload("interrupted-revise", "刚才夹具A改成100克。"), interrupted, now=now+1)
    br = message_worker({"key": b["inbox_key"]}, interrupted, execute=_execute, send=deliver)
    recovered = []
    def recover(state, scope):
        recovered.append(sum(r["nutrition"]["kcal"] for r in state["intake"]))
        return _project(state, scope)
    interrupted.stale_list = True
    sync_worker({"key": br["outbox_keys"][-1]}, interrupted, project=recover)
    assert recovered == [270, 210]
    # A known committed receipt becoming invisible is an error, never skipped.
    interrupted.hidden.add(a["receipt_key"])
    try:
        message_worker({"key": b["inbox_key"]}, interrupted, execute=_execute, send=deliver)
    except ProbeUnavailable:
        pass
    else:
        raise AssertionError("missing predecessor receipt must fail closed")


def _hard_child(directory, key):
    import os
    message_worker({"key": key}, FileObjects(directory), execute=_execute,
                   send=lambda reply, uuid: uuid, hard_exit=lambda: os._exit(73))


def C04():
    """独立子进程真正os._exit(73)后重开恢复；保留日期、固定动作与原消息防重。"""
    with tempfile.TemporaryDirectory(prefix="cos-pipeline-hard-exit-") as directory:
        objects = FileObjects(directory)
        first = ingress(_payload("hard-record", "记录午餐：夹具A200克，夹具B2个。"), objects,
                        scope="hard-restart", now=1793505600)
        command = [sys.executable, "-c", "from harness.eval.cos_pipeline_cases import _hard_child; import sys; _hard_child(sys.argv[1],sys.argv[2])",
                   directory, first["inbox_key"]]
        process = subprocess.run(command, cwd=Path(__file__).resolve().parents[2], capture_output=True, text=True, timeout=60)
        assert process.returncode == 73, (process.returncode, process.stdout, process.stderr)
        marker = json.loads(objects.get("cos-probe/pipeline/hard-restart/failpoints/after-write.json"))
        committed = json.loads(objects.get(marker["snapshot_key"]))
        assert committed["reason"] == "write_intake" and len(committed["state"]["intake"]) == 2
        assert sum(r["nutrition"]["kcal"] for r in committed["state"]["intake"]) == 270
        sent = []
        restored = message_worker({"key": first["inbox_key"]}, objects, execute=_execute,
                                  send=lambda reply, uuid: sent.append(uuid) or uuid,
                                  hard_exit=lambda: (_ for _ in ()).throw(AssertionError("failpoint must fire only once")))
        replay_in = ingress(_payload("hard-record", "ignored duplicate"), objects, scope="hard-restart", now=1793592000)
        replay = message_worker({"key": replay_in["inbox_key"]}, objects, execute=_execute,
                                send=lambda reply, uuid: sent.append(uuid) or uuid)
        assert restored["audit"] == replay["audit"] and replay["audit"]["count"] == 2 and replay["audit"]["kcal"] == 270
        assert len(sent) == 1 and all(r["date"] == first["intake_date"] for r in replay["audit"]["items"])


CHECKS = {"C03": C03, "C04": C04}
