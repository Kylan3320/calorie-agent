"""Three isolated SCF entries and a bounded HTTP driver; never a live preset."""
import hashlib
import hmac
import json
import os
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone
from urllib.parse import urlsplit

from agent.storage.cos_probe_pipeline import ingress, message_worker, sync_worker, result_key, consumed_key
from agent.storage.cos_probe_transport import CosObjects

BUCKET = "calorie-cos-probe-20261101-1440448581"
FUNCTIONS = {"ingress": "calorie-cos-ingress-probe", "message": "calorie-cos-msg-probe",
             "sync": "calorie-cos-sync-probe", "driver": "calorie-cos-probe"}
INSTANCE = uuid.uuid4().hex
_BUILD_FILE = Path(__file__).resolve().parents[2] / "BUILD_ID"
BUILD_ID = _BUILD_FILE.read_text().strip() if _BUILD_FILE.exists() else "development"


def _guard(kind, context):
    name = os.getenv("SCF_FUNCTIONNAME") or getattr(context, "function_name", "")
    if name != FUNCTIONS[kind]:
        raise RuntimeError("isolated function identity required: " + FUNCTIONS[kind])


def _objects():
    for target, source in (("TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRETID"),
                           ("TENCENTCLOUD_SECRET_KEY", "TENCENTCLOUD_SECRETKEY"),
                           ("TENCENTCLOUD_SESSION_TOKEN", "TENCENTCLOUD_SESSIONTOKEN")):
        if os.getenv(source):
            os.environ[target] = os.environ[source]
    return CosObjects(BUCKET)


def _put(objects, key, value):
    return objects.create(key, json.dumps(value, ensure_ascii=False).encode())


def _get(objects, key):
    raw = objects.get(key)
    return json.loads(raw) if raw is not None else None


def ingress_handler(event, context=None):
    _guard("ingress", context)
    started = time.time()
    if event.get("httpMethod") != "POST" or event.get("path") != "/webhook/feishu/events":
        return {"statusCode": 404, "body": "{}"}
    payload = json.loads(event["body"])
    scope = payload.get("probe_scope", "pipeline-validation")
    objects = _objects()
    ack = ingress(payload, objects, scope=scope, now=started)
    ack.update(ack_at=time.time(), instance_id=INSTANCE, build_id=BUILD_ID)
    return {"statusCode": 200, "headers": {"Content-Type": "application/json"},
            "body": json.dumps(ack)}


def _events(event):
    if "Records" not in event:
        return [event]
    events = []
    for record in event["Records"]:
        bucket = record["cos"]["cosBucket"]
        if str(bucket["appid"]) != "1440448581" or bucket["name"] != BUCKET.rsplit("-", 1)[0]:
            raise ValueError("unexpected COS trigger bucket")
        raw = record["cos"]["cosObject"]["key"]
        prefix = "/1440448581/" + BUCKET.rsplit("-", 1)[0] + "/"
        if not raw.startswith(prefix):
            raise ValueError("unexpected COS trigger object")
        events.append({"key": raw[len(prefix):]})
    return events


def _native_evidence(event, objects, kind):
    # COS events can include signed request headers: retain ONLY bucket/key/type.
    if "Records" in event:
        _put(objects, "cos-probe/pipeline/diagnostics/" + kind + "-" + uuid.uuid4().hex + ".json",
             {"at": time.time(), "instance_id": INSTANCE, "build_id": BUILD_ID, "records": [
                 {"bucket": {k: r["cos"]["cosBucket"].get(k) for k in ("name", "appid", "region")},
                  "key": r["cos"]["cosObject"]["key"], "type": r.get("event", {}).get("eventName")}
                 for r in event["Records"]]})


def message_handler(event, context=None):
    _guard("message", context)
    from agent.storage import feishu_client
    objects = _objects()
    _native_evidence(event, objects, "message")

    def send(reply, key):
        response = feishu_client.send_text(os.environ["COS_PROBE_CHAT_ID"], reply, uuid_key=key)
        return response["data"]["message_id"]

    def hard_exit():
        _guard("message", context)
        os._exit(73)

    results = [message_worker(item, objects, send=send, hard_exit=hard_exit) for item in _events(event)]
    return {"instance_id": INSTANCE, "build_id": BUILD_ID, "results": results, "cos_requests": len(objects.events)}


def sync_handler(event, context=None):
    _guard("sync", context)
    from agent.storage.cos_probe_projection import sync_snapshot
    objects = _objects()
    _native_evidence(event, objects, "sync")

    def project(state, scope):
        root = "cos-probe/pipeline/" + scope + "/evidence/"
        attempt = uuid.uuid4().hex
        began = time.time()
        delayed = _put(objects, root + "slow-sync-once.json", {"started_at": began, "instance_id": INSTANCE})
        _put(objects, root + "sync-" + attempt + "-started.json",
             {"started_at": began, "instance_id": INSTANCE, "injected_delay_seconds": 45 if delayed else 0})
        if delayed:
            time.sleep(45)
        rows = sync_snapshot(state, os.environ["COS_PROBE_TABLE_ID"], scope=scope)
        _put(objects, root + "sync-" + attempt + "-finished.json",
             {"started_at": began, "finished_at": time.time(), "instance_id": INSTANCE,
              "rows": [{k: r[k] for k in ("record_id", "message_id", "grams", "nutrition", "status")} for r in rows]})
        return rows

    results = [sync_worker(item, objects, project=project) for item in _events(event)]
    return {"instance_id": INSTANCE, "build_id": BUILD_ID, "results": results, "cos_requests": len(objects.events)}


def _post(payload):
    """Tencent documented TC3 function URL auth, using only SCF temporary keys."""
    import httpx
    endpoint = os.environ["COS_PROBE_INGRESS_URL"].rstrip("/")
    parsed = urlsplit(endpoint)
    if parsed.scheme != "https" or not parsed.hostname.endswith(".ap-shanghai.tencentscf.com") or parsed.path:
        raise ValueError("exact Shanghai test function URL required")
    path = "/webhook/feishu/events"
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    stamp = int(time.time())
    day = datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d")
    signed = "content-type;host"
    canonical = "POST\n" + path + "\n\ncontent-type:application/json\nhost:" + parsed.hostname + "\n\n" + signed + "\n" + hashlib.sha256(body).hexdigest()
    credential = day + "/scf/tc3_request"
    to_sign = "TC3-HMAC-SHA256\n" + str(stamp) + "\n" + credential + "\n" + hashlib.sha256(canonical.encode()).hexdigest()
    def sign(key, value):
        return hmac.new(key, value.encode(), hashlib.sha256).digest()
    key = sign(sign(sign(("TC3" + os.environ["TENCENTCLOUD_SECRET_KEY"]).encode(), day), "scf"), "tc3_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    auth = "TC3-HMAC-SHA256 Credential=" + os.environ["TENCENTCLOUD_SECRET_ID"] + "/" + credential + ", SignedHeaders=" + signed + ", Signature=" + signature
    start = time.time()
    response = httpx.post(endpoint + path, content=body, headers={"Content-Type": "application/json",
        "Authorization": auth, "X-Scf-Cam-Uin": "100049554055", "X-Scf-Cam-Timestamp": str(stamp),
        "X-Scf-Cam-Token": os.environ["TENCENTCLOUD_SESSION_TOKEN"]}, timeout=15)
    response.raise_for_status()
    return {"client_started_at": start, "ack_received_at": time.time(), "ack": response.json()}


def control(event, context=None):
    _guard("driver", context)
    objects = _objects()
    operation = event["operation"]
    scope = event.get("scope", "pipeline-validation")
    if scope not in {"pipeline-validation", "hard-restart", "idle-pipeline-validation"}:
        raise ValueError("unknown isolated scope")
    root = "cos-probe/pipeline/" + scope + "/"
    if operation == "pipeline_idle_seed":
        if scope != "idle-pipeline-validation":
            raise ValueError("idle seed requires the fresh idle namespace")
        _put(objects, root + "seed.json", {"seeded_at": time.time()})
        return {"operation": operation, "scope": scope, "seed": _get(objects, root + "seed.json")}
    if operation == "pipeline_sequence":
        if scope != "idle-pipeline-validation":
            raise ValueError("sequence is reserved for the fresh idle namespace")
        seed = _get(objects, root + "seed.json")
        if not seed or time.time() - seed["seeded_at"] < 10800:
            raise ValueError("wait at least three hours after idle seed; check other invocations separately")
        if not _put(objects, root + "sequence-started.json", {"started_at": time.time(), "build_id": BUILD_ID}):
            raise ValueError("idle sequence already started; audit existing evidence, never time a replay as fresh")
        deadline = time.monotonic() + 90
        sequence = []
        def await_value(key):
            while time.monotonic() < deadline:
                value = _get(objects, key)
                if value is not None:
                    return value
                time.sleep(0.5)
            raise TimeoutError("native pipeline did not complete; do not claim success: " + key)
        for action in ("record", "revise", "query"):
            receipt = control({"operation": "pipeline_http", "scope": scope, "message": action}, context)
            if receipt["ack"]["received_at"] < receipt["client_started_at"] - 1:
                raise ValueError("pre-existing receipt is not a fresh latency sample")
            completed = await_value(result_key(receipt["message_id"], scope=scope))
            if action == "record":
                await_value(root + "evidence/slow-sync-once.json")
            timing = completed["timing"]
            if timing["reply_sent_at"] < receipt["client_started_at"]:
                raise ValueError("old reply cannot be timed against this new HTTP request")
            sequence.append({"message": action, "ack_ms": round((receipt["ack_received_at"]-receipt["client_started_at"])*1000),
                "client_to_reply_ms": round((timing["reply_sent_at"]-receipt["client_started_at"])*1000),
                "ingress_to_reply_ms": timing["reply_elapsed_ms"],
                "queue_ms": round((timing["started_at"]-timing["received_at"])*1000),
                "reply_sent_at": timing["reply_sent_at"], "instance_id": timing["instance_id"],
                "count": completed["audit"]["count"], "kcal": completed["audit"]["kcal"]})
        replay = control({"operation": "pipeline_http", "scope": scope, "message": "replay"}, context)
        replay_done = await_value(consumed_key(replay["ack"]["inbox_key"], scope=scope))
        report = {"operation": operation, "scope": scope, "sequence": sequence,
                  "replay_ack": replay["ack"]["ack"],
                  "replay_consumed": {k: replay_done["audit"][k] for k in ("count", "kcal")},
                  "seeded_at": seed["seeded_at"]}
        _put(objects, root + "sequence.json", report)
        return report
    if operation == "pipeline_diagnostics":
        return {"operation": operation, "native_events": [_get(objects, key)
            for key in objects.list("cos-probe/pipeline/diagnostics/")][-6:]}
    if operation == "pipeline_http":
        from harness.eval.cos_probe_live import TEXT
        selected = event["message"]
        action = "record" if selected == "replay" else selected
        if action not in TEXT:
            raise ValueError("unknown fixture message")
        mid = "cos-probe-" + scope + "-" + action
        payload = {"schema": "2.0", "probe_scope": scope, "header": {"event_type": "im.message.receive_v1"},
            "event": {"sender": {"sender_id": {"open_id": "cos-probe-user"}},
            "message": {"message_id": mid, "chat_id": "cos-probe-session", "message_type": "text",
                        "create_time": str(int(time.time()*1000)), "content": json.dumps({"text": TEXT[action]}, ensure_ascii=False)}}}
        result = _post(payload)
        result.update(operation=operation, message_id=mid, scope=scope)
        _put(objects, root + "evidence/http-" + uuid.uuid4().hex + ".json", result)
        return result
    if operation == "pipeline_audit":
        # Audit only exact immutable evidence; never opens the message Store.
        if event.get("inbox_key"):
            return {"operation": operation, "scope": scope,
                    "consumed": _get(objects, consumed_key(event["inbox_key"], scope=scope))}
        values = []
        for action in ("record", "revise", "query"):
            value = _get(objects, result_key("cos-probe-" + scope + "-" + action, scope=scope))
            if value:
                values.append(value)
        evidence = []
        for key in objects.list(root + "evidence/"):
            value = _get(objects, key)
            if value:
                evidence.append({"key": key, **value})
        result = {"operation": operation, "scope": scope, "results": values, "evidence": evidence,
                  "hard_exit": _get(objects, root + "failpoints/after-write.json")}
        _put(objects, root + "audits/" + uuid.uuid4().hex + ".json", result)
        # Keep API Explorer response below its rendering limit. Full audit is in COS.
        selected = event.get("message")
        marker = result["hard_exit"]
        committed = _get(objects, marker["snapshot_key"]) if marker else None
        committed_rows = [r for r in committed["state"]["intake"] if r["status"] != "deleted"] if committed else []
        return {"operation": operation, "scope": scope,
            "results": [{"message_id": v["message_id"], "timing": v["timing"],
                         "audit": {k: v["audit"][k] for k in ("count", "kcal")}}
                        for v in values if selected is None or v["message_id"].endswith("-" + selected)],
            "sync_evidence": [{k: v[k] for k in ("key", "started_at", "finished_at", "injected_delay_seconds") if k in v}
                              for v in evidence if "/evidence/sync-" in v["key"]],
            "hard_exit": marker,
            **({"hard_exit_committed": {"count": len(committed_rows),
                "kcal": sum(r["nutrition"]["kcal"] for r in committed_rows),
                "record_ids": [r["record_id"] for r in committed_rows]}} if committed else {})}
    raise ValueError("unknown pipeline driver operation")
