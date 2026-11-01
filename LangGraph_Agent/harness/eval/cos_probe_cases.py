"""Offline failure injection for the isolated COS probe; no cloud claims."""
from copy import deepcopy
from dataclasses import replace
import json

from agent import run
from agent.storage.cos_probe import CosProbeStore, ProbeUnavailable
from harness.eval.personal_cases import A, B, CONFIG, MEAL, NativeFake, call
from harness.eval.conversation_cases import answer


class MemoryObjects:
    """Model only the transport contract; deliberately stale LIST/GET are injectable."""
    def __init__(self):
        self.data = {}
        self.stale_list = False
        self.hidden = set()
        self.crash_reason = None

    def get(self, key):
        return None if key in self.hidden else self.data.get(key)

    def list(self, prefix):
        return [] if self.stale_list else [k for k in self.data if k.startswith(prefix)]

    def create(self, key, body):
        if key in self.data:
            return False
        self.data[key] = body
        if json.loads(body)["reason"] == self.crash_reason:
            self.crash_reason = None
            raise OSError("injected response loss after object commit")
        return True


def _open(objects, scope="default"):
    foods = deepcopy([A, B])
    foods[1].update(units=[{"unit": "个", "weight": 60, "eat_weight": 50}], detail_loaded=True)
    return CosProbeStore(objects, "cos-probe/" + scope, foods=foods, today="2026-11-01")


def _run(store, mid, text, steps):
    model = NativeFake(responses=steps)
    result = run(dict(message_id=mid, user_id="user", chat_id="test", text=text),
                 store=store, config=replace(CONFIG, preset="live"), model=model)
    assert result["status"] == "ok", result["errors"]
    return result, model


def C01():
    """COS隔离探针：同步前改量/合计、重建/重投、写后响应丢失、陈旧列表与不可见冲突。"""
    objects = MemoryObjects()
    store = _open(objects)
    recorded, _ = _run(store, "cos-record", "记录午餐：夹具A200克，夹具B2个。",
                       [call("record_meal", MEAL), answer])
    assert store.today_summary("user")["totals"]["kcal"] == 270
    # A fresh instance uses persisted facts, including paired model/tool history.
    store = _open(objects)
    _run(store, "cos-revise", "刚才夹具A改为100克，夹具B不变。",
         [call("revise_intake", {"food_query": "夹具A", "grams": 100}), answer])
    expected = {"carbs": 10, "protein": 14, "fat": 11, "kcal": 210}
    assert store.today_summary("user")["totals"] == expected
    _run(store, "cos-query", "今天合计多少热量？", [call("query_today", {}), answer])
    last_session = store.load_session("user", "test")
    objects.stale_list = True
    store = _open(objects)
    replay, model = _run(store, "cos-record", "记录午餐：夹具A200克，夹具B2个。", [])
    assert not model.requests and replay["reply"] == recorded["reply"]
    assert len(store.intake) == 2 and store.today_summary("user")["totals"] == expected
    assert store.load_session("user", "test") == last_session
    # Independent raw decoding, not the Store's aggregation implementation.
    latest_key = max(objects.data)
    raw = json.loads(objects.data[latest_key])["state"]
    assert [(r["food_name"], r["grams"]) for r in raw["intake"]] == [("夹具A", 100), ("夹具B", 100)]
    assert sum(r["nutrition"]["kcal"] for r in raw["intake"]) == 210

    crash_objects = MemoryObjects()
    crashed = _open(crash_objects, "crash")
    crash_objects.crash_reason = "write_intake"
    try:
        _run(crashed, "cos-crash", "记录午餐：夹具A200克，夹具B2个。",
             [call("record_meal", MEAL), answer])
    except ProbeUnavailable:
        pass
    else:
        raise AssertionError("uncertain write must invalidate the instance")
    try:
        crashed.today_summary("user")
    except ProbeUnavailable:
        pass
    else:
        raise AssertionError("uncommitted memory must not serve reads")
    restored = _open(crash_objects, "crash")
    recovered, _ = _run(restored, "cos-crash", "记录午餐：夹具A200克，夹具B2个。", [answer])
    assert recovered["status"] == "ok" and len(restored.intake) == 2
    assert restored.today_summary("user")["totals"]["kcal"] == 270

    # Execution/result was committed, but final session save/response was lost.
    delivery_objects = MemoryObjects()
    delivery = _open(delivery_objects, "delivery")
    delivery_objects.crash_reason = "save_session"
    try:
        _run(delivery, "cos-delivery", "记录午餐：夹具A200克，夹具B2个。",
             [call("record_meal", MEAL), answer])
    except OSError:
        pass
    else:
        raise AssertionError("session-save response-loss fault was not reached")
    delivered = _open(delivery_objects, "delivery")
    receipt, untouched_model = _run(delivered, "cos-delivery", "记录午餐：夹具A200克，夹具B2个。", [])
    assert not untouched_model.requests and len(delivered.intake) == 2
    assert receipt["reply"] and delivered.load_session("user", "test")["last_message_id"] == "cos-delivery"

    hidden = MemoryObjects()
    _open(hidden, "hidden")
    hidden.stale_list = True
    hidden.hidden = set(hidden.data)
    before = dict(hidden.data)
    try:
        _open(hidden, "hidden")
    except ProbeUnavailable:
        pass
    else:
        raise AssertionError("conflict plus invisible object must not reset the ledger")
    assert before == hidden.data


def C02():
    """真实COS SDK离线协议检查：禁止覆盖头、MD5、固定键与404/403区分。"""
    import base64
    import hashlib
    import os
    from pathlib import Path
    import sys
    from types import SimpleNamespace
    from unittest.mock import patch
    from agent.storage.cos_probe_transport import CosObjects

    original_path = list(sys.path)
    sdk_path = Path(os.getenv("COS_PROBE_SDK_PATH", str(Path.home() / ".codex/tmp/cos-probe-deps")))
    try:
        if sdk_path.is_dir():
            sys.path.insert(0, str(sdk_path))
        from qcloud_cos import CosConfig, CosS3Client
        from qcloud_cos.cos_exception import CosServiceError
        client = CosS3Client(CosConfig(Region="ap-shanghai", SecretId="offline-test-id",
                                      SecretKey="offline-test-secret", Scheme="https"), retry=0)
        # Avoid constructor's real bucket-versioning request. Only the actual SDK
        # request pipeline is exercised; its network boundary is intercepted.
        objects = CosObjects.__new__(CosObjects)
        objects.bucket, objects.client = "offline-probe-1234567890", client
        objects.events, objects.bytes = [], 0
        objects.max_requests, objects.max_bytes = 20, 1024 * 1024
        key, body = "cos-probe/offline-sdk/000000000007.json", b'{"revision":7}'
        captured = []

        def capture(**kwargs):
            captured.append(kwargs)
            return SimpleNamespace(headers={"ETag": "offline-etag"})

        with patch.object(client, "send_request", side_effect=capture):
            assert objects.create(key, body) is True
        assert len(captured) == 1
        request = captured[0]
        headers = {k.lower(): v for k, v in request["headers"].items()}
        assert headers["x-cos-forbid-overwrite"] == "true", headers
        actual_md5 = headers["content-md5"]
        if isinstance(actual_md5, bytes):
            actual_md5 = actual_md5.decode("ascii")
        assert actual_md5 == base64.b64encode(hashlib.md5(body).digest()).decode("ascii")
        assert headers["content-type"] == "application/json"
        assert request["method"] == "PUT" and request["url"].endswith("/" + key)
        assert request["bucket"] == objects.bucket and request["data"] == body

        missing = CosServiceError("GET", {"code": "NoSuchKey", "message": "offline missing"}, 404)
        denied = CosServiceError("GET", {"code": "AccessDenied", "message": "offline denied"}, 403)
        with patch.object(client, "send_request", side_effect=missing):
            assert objects.get(key) is None
        assert objects.events[-1]["outcome"] == "missing"
        with patch.object(client, "send_request", side_effect=denied):
            try:
                objects.get(key)
            except CosServiceError as exc:
                assert exc is denied and exc.get_status_code() == 403
            else:
                raise AssertionError("permission failure must not become an empty ledger")
        assert objects.events[-1]["outcome"] == "error"
        exists = CosServiceError("PUT", {"code": "ObjectAlreadyExists", "message": "offline exists"}, 409)
        with patch.object(client, "send_request", side_effect=exists):
            assert objects.create(key, body) is False
        assert objects.events[-1]["outcome"] == "exists"
        file_exists = CosServiceError("PUT", {"code": "FileAlreadyExists", "message": "offline file exists"}, 409)
        with patch.object(client, "send_request", side_effect=file_exists):
            assert objects.create(key, body) is False
        assert objects.events[-1]["outcome"] == "exists"
        other_conflict = CosServiceError("PUT", {"code": "BucketConflict", "message": "offline unrelated conflict"}, 409)
        with patch.object(client, "send_request", side_effect=other_conflict):
            try:
                objects.create(key, body)
            except CosServiceError as exc:
                assert exc is other_conflict
            else:
                raise AssertionError("unrelated 409 must not authorize snapshot catch-up")
        assert objects.events[-1]["outcome"] == "error"
    finally:
        sys.path[:] = original_path


CHECKS = {"C01": C01, "C02": C02}
