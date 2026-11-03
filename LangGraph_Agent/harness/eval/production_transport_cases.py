"""Production SDK protocol and real process-death recovery checks."""
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from unittest.mock import patch


def ST01():
    """生产 COS SDK：禁止覆盖、MD5、固定键及缺失/权限/冲突分类。"""
    import base64
    import hashlib
    from types import SimpleNamespace
    from qcloud_cos import CosConfig, CosS3Client
    from qcloud_cos.cos_exception import CosServiceError
    from agent.storage.cos_transport import CosObjects

    client = CosS3Client(CosConfig(Region="ap-shanghai", SecretId="offline-test-id",
                                  SecretKey="offline-test-secret", Scheme="https"), retry=0)
    objects = CosObjects.__new__(CosObjects)
    objects.bucket, objects.client = "offline-production-1234567890", client
    key, body = "acceptance/sdk/message/commits/000000000007.json", b'{"revision":7}'
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
    with patch.object(client, "send_request", side_effect=missing):
        assert objects.get(key) is None
    for code, status in (("AccessDenied", 403), ("NoSuchBucket", 404)):
        error = CosServiceError("GET", {"code": code, "message": "offline error"}, status)
        with patch.object(client, "send_request", side_effect=error):
            try:
                objects.get(key)
            except CosServiceError as exc:
                assert exc is error
            else:
                raise AssertionError("storage failure became an empty ledger")
    for code in ("ObjectAlreadyExists", "FileAlreadyExists"):
        error = CosServiceError("PUT", {"code": code, "message": "offline exists"}, 409)
        with patch.object(client, "send_request", side_effect=error):
            assert objects.create(key, body) is False
    conflict = CosServiceError("PUT", {"code": "BucketConflict", "message": "unrelated conflict"}, 409)
    with patch.object(client, "send_request", side_effect=conflict):
        try:
            objects.create(key, body)
        except CosServiceError as exc:
            assert exc is conflict
        else:
            raise AssertionError("unrelated conflict authorized commit catch-up")


def ST02():
    """生产验收驱动先保存失败样本，拒绝将同一批旧结果重跑为新测速。"""
    from harness.eval import production_live

    driver = production_live.Driver.__new__(production_live.Driver)
    driver.plan = {"sender_open_id": "fixture-sender", "chat_id": "fixture-chat"}
    driver.report = {}
    saved = []
    driver.save = lambda: saved.append(deepcopy(driver.report))
    message = {"step": "record", "message_id": "fixed-sample", "text": "记录夹具A200克。"}

    def timeout(*args):
        assert saved[-1]["requests"][0]["message_id"] == "fixed-sample"
        raise TimeoutError("injected webhook timeout")

    with patch.dict(os.environ, {"FEISHU_VERIFICATION_TOKEN": "offline-test-token"}), \
            patch.object(production_live, "_post_webhook", side_effect=timeout) as post:
        try:
            driver.submit(message)
        except TimeoutError:
            pass
        else:
            raise AssertionError("failed sample was not retained")
        original = deepcopy(driver.report)
        try:
            driver.business()
        except ValueError as exc:
            assert "prior sample" in str(exc)
        else:
            raise AssertionError("old sample was accepted as a fresh business run")
        assert post.call_count == 1 and driver.report == original


class FileObjects:
    """The production object contract, persisted across the test child exit."""
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


PREFIX = "acceptance/hard-restart/"
REPLY = "夹具A和夹具B已记录，这餐270千卡，夹具B按份量估算。"


def _hard_child(directory, saved_event):
    from agent import run
    from agent.storage.execution_pipeline import Pipeline
    from harness.eval.personal_cases import CONFIG, MEAL, NativeFake, call
    from langchain_core.messages import AIMessage

    pipeline = Pipeline(FileObjects(Path(directory) / "objects"), PREFIX)

    def execute(message, **kwargs):
        return run(message, model=NativeFake(responses=[call("record_meal", MEAL),
                                                       AIMessage(content=REPLY)]), **kwargs)

    def die_before_delivery(chat, text, uuid_key):
        # Pipeline already persisted the business result and final reply here.
        (Path(directory) / "send-attempt.json").write_text(
            json.dumps({"chat": chat, "text": text, "uuid": uuid_key}, ensure_ascii=False), encoding="utf-8")
        os._exit(73)

    with patch("agent.config.load_config", return_value=replace(CONFIG, preset="live")):
        pipeline.worker(json.loads(saved_event), execute=execute, send=die_before_delivery)


def P05():
    """生产进程 os._exit 后恢复：原日期、270千卡、软删除、原回复/UUID及重投防重。"""
    from agent.storage.cos_store import CosStore
    from agent.storage.execution_pipeline import Pipeline
    from harness.eval.personal_cases import A, B, CONFIG
    from harness.eval.production_pipeline_cases import event, message

    with tempfile.TemporaryDirectory(prefix="production-hard-exit-") as directory:
        objects = FileObjects(Path(directory) / "objects")
        foods = deepcopy([A, B])
        foods[1].update(units=[{"unit": "个", "weight": 60, "eat_weight": 50}], detail_loaded=True)
        deleted = {"record_id": "prior-deleted", "user_id": "user", "date": "2026-10-31",
                   "message_id": "prior-message:agent:1", "food_id": "A", "food_name": "夹具A",
                   "raw_text": "旧记录已撤销", "meal_type": "lunch", "grams": 100,
                   "nutrition": {"carbs": 10, "protein": 2, "fat": 1, "kcal": 60},
                   "snapshot": {}, "status": "deleted", "created_at": 1000}
        CosStore(objects, PREFIX).import_baseline({"baseline_id": "hard-exit-fixture",
                                                 "foods": foods, "intake": [deleted]})
        pipeline = Pipeline(objects, PREFIX)
        import time
        received = time.time()
        original = message("hard-exit-record", "记录夹具A200克和夹具B2个。")
        receipt = pipeline.accept(original, received_at=received)
        original_date = receipt["intake_date"]
        command = [sys.executable, "-B", "-c",
                   "from harness.eval.production_transport_cases import _hard_child; "
                   "import sys; _hard_child(sys.argv[1], sys.argv[2])",
                   directory, json.dumps(event(receipt))]
        environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8",
                       "TRACE_ENABLED": "false"}
        process = subprocess.run(command, cwd=Path(__file__).resolve().parents[2], env=environment,
                                 capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert process.returncode == 73, (process.returncode, process.stdout, process.stderr)
        attempted = json.loads((Path(directory) / "send-attempt.json").read_text(encoding="utf-8"))

        restored = Pipeline(FileObjects(Path(directory) / "objects"), PREFIX)
        interrupted = restored.result(receipt["receipt_id"])
        assert interrupted["attempts"] == 1 and interrupted["status"] == "processing"
        assert interrupted["result"]["reply"] == REPLY == attempted["text"]
        assert interrupted["delivery"]["uuid"] == attempted["uuid"]
        sent = []

        def forbidden(*args, **kwargs):
            raise AssertionError("already committed execution must not run again")

        def send(chat, text, uuid_key):
            sent.append({"chat": chat, "text": text, "uuid": uuid_key})
            return {"data": {"message_id": "recovered-delivery"}}

        with patch("agent.config.load_config", return_value=replace(CONFIG, preset="live")):
            recovered = restored.worker(event(receipt), execute=forbidden, send=send)
            assert recovered["status"] == "completed" and recovered["attempts"] == 2
            duplicate = restored.accept(original, received_at=received + 86400)
            assert duplicate["received_at"] == received and duplicate["intake_date"] == original_date
            assert restored.worker(event(duplicate), execute=forbidden, send=forbidden)["duplicate"]
        assert sent == [attempted]
        rows = CosStore(objects, PREFIX, readonly=True).intake_items("user", include_deleted=True)
        active = [r for r in rows if r["status"] != "deleted"]
        assert len(rows) == 3 and len(active) == 2
        assert {r["date"] for r in active} == {original_date}
        assert sorted(r["nutrition"]["kcal"] for r in active) == [120, 150]
        assert sum(r["nutrition"]["kcal"] for r in active) == 270
        assert next(r for r in rows if r["record_id"] == "prior-deleted")["status"] == "deleted"
        assert {r["message_id"] for r in active} == {"hard-exit-record:agent:1"}


CHECKS = {"ST01": ST01, "ST02": ST02, "P05": P05}
