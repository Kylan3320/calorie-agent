"""Explicit isolated SCF probe. No production entry imports this module.

Invoke seed once; record refuses to run until three hours of real inactivity.
Each invocation creates a new Store. SCF must have maximum concurrency one.
This measures the isolated function, not production webhook ACK/queue latency.
"""
from copy import deepcopy
from dataclasses import replace
import json
import os
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "deps"))

from agent import run
from agent.config import load_config
from agent.storage.cos_probe import CosProbeStore
from agent.storage.cos_probe_transport import CosObjects
from harness.eval.personal_cases import A, B

BUCKET = "calorie-cos-probe-20261101-1440448581"
PREFIX = "cos-probe/meal-validation/"
USER, CHAT = "cos-probe-user", "cos-probe-session"
INSTANCE_ID = uuid.uuid4().hex
TEXT = {"record": "记录午餐：夹具A200克，夹具B2个。",
        "revise": "刚才夹具A改成100克，夹具B不变。",
        "query": "今天摄入合计多少千卡？"}


def handler(event, context=None):
    if str(event.get("operation", "")).startswith("pipeline_"):
        from harness.eval.cos_pipeline_live import control
        result = control(event, context)
        return {"build_id": (Path(__file__).resolve().parents[2] / "BUILD_ID").read_text().strip(), **result}
    started = time.time()
    # Event SCF role credentials use the platform's environment spelling.
    for target, source in (("TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRETID"),
                           ("TENCENTCLOUD_SECRET_KEY", "TENCENTCLOUD_SECRETKEY"),
                           ("TENCENTCLOUD_SESSION_TOKEN", "TENCENTCLOUD_SESSIONTOKEN")):
        if os.getenv(source):
            os.environ[target] = os.environ[source]
    objects = CosObjects(BUCKET)
    operation = event["operation"]
    if operation == "stats":
        count, total, marker, writes = 0, 0, "", []
        while True:
            page = objects._call("stats_list", lambda: objects.client.list_objects(
                Bucket=BUCKET, Prefix="cos-probe/", Marker=marker, MaxKeys=1000))
            objects.events[-1].pop("response", None)
            for item in page.get("Contents", []):
                count += 1
                total += int(item["Size"])
                key = item["Key"]
                if key.startswith("cos-probe/write-loss/") and key.rsplit("/", 1)[-1][:-5].isdigit():
                    body = objects.get(key)
                    if body is None:
                        raise RuntimeError("listed audit snapshot is not visible")
                    value = json.loads(body)
                    if value.get("reason") == "write_intake":
                        rows = value["state"]["intake"]
                        writes.append({"revision": value["revision"], "count": len(rows),
                                       "kcal": sum(r["nutrition"]["kcal"] for r in rows)})
            if str(page.get("IsTruncated", "false")).lower() != "true":
                break
            marker = page["NextMarker"]
        return {"operation": "stats", "objects": count, "stored_bytes": total,
                "storage_yuan_per_month_without_free": total/1024**3*0.118,
                "write_loss_commits": writes, "cos_requests": len(objects.events)}
    scenario = event.get("scenario", "meal-validation")
    if scenario not in {"meal-validation", "immediate-validation", "write-loss", "session-loss"}:
        raise ValueError("unknown isolated scenario")
    prefix = "cos-probe/"+scenario+"/"
    seed_key = prefix + "seed.json"
    if operation == "seed":
        objects.create(seed_key, json.dumps({"seeded_at": started}).encode())
    seed = objects.get(seed_key)
    if seed is None and scenario != "meal-validation":
        objects.create(seed_key, json.dumps({"seeded_at": started}).encode())
        seed = objects.get(seed_key)
    if seed is None:
        raise ValueError("seed this isolated namespace before running")
    idle = started - json.loads(seed)["seeded_at"]
    if operation == "record" and scenario == "meal-validation" and idle < 10800:
        raise ValueError("record requires three hours after seed; do not warm the function")
    if operation == "sync":
        if scenario not in {"meal-validation", "immediate-validation"}:
            raise ValueError("fault scenarios may not project to the test table")
        from agent.storage.cos_probe_projection import sync_snapshot
        # This minimum probe invokes sync manually on the SAME single-execution
        # function. Reopen through the barrier so an old retry projects latest
        # facts. A separate production sync worker requires its own design.
        current = CosProbeStore(objects, prefix)
        revision = current.revision
        rows = sync_snapshot(current.snapshot(), os.environ["COS_PROBE_TABLE_ID"], scope=scenario)
        objects.create(f"cos-probe/sync-confirmations/{scenario}-{revision:012d}.json",
                       json.dumps({"revision": revision, "rows": rows}, ensure_ascii=False).encode())
        return {"operation": "sync", "revision": revision,
                "rows": [{k:r[k] for k in ("record_id", "message_id", "food_name", "grams", "nutrition", "status")}
                         for r in rows], "cos_requests": len(objects.events)}
    fault = event.get("fail_after")
    if fault not in {None, "write_intake", "save_session"}:
        raise ValueError("unknown failure point")
    if fault:
        create = objects.create
        def lose_response(key, body):
            created = create(key, body)
            if created and json.loads(body).get("reason") == fault:
                raise OSError("injected loss after durable "+fault)
            return created
        objects.create = lose_response
    foods = deepcopy([A, B])
    foods[1].update(units=[{"unit": "个", "weight": 60, "eat_weight": 50}], detail_loaded=True)
    store = CosProbeStore(objects, prefix, foods=foods, today="2026-11-01")
    if operation == "seed":
        if store.intake:
            raise ValueError("seed must start with an empty isolated ledger")
        result = {"seeded": True}
    elif operation == "read":
        result = {"snapshot": store.snapshot()}
    else:
        selected = "record" if operation == "replay" else operation
        if selected not in TEXT:
            raise ValueError("unknown probe operation")
        message = dict(user_id=USER, chat_id=CHAT, message_id="cos-probe-"+scenario+"-"+selected,
                       text=TEXT[selected])
        result = run(message, store=store, config=replace(load_config(), preset="live"))
        if result["status"] != "ok":
            raise RuntimeError("probe execution incomplete: "+str(result["errors"]))
        if os.getenv("COS_PROBE_CHAT_ID"):
            from agent.storage import feishu_client, reply_uuid
            receipt = store.load_document("probe_delivery", USER, message["message_id"])
            if not receipt.get("sent"):
                if not receipt:
                    receipt = {"reply": result["reply"], "uuid": reply_uuid(message), "sent": False}
                    store.save_document("probe_delivery", USER, message["message_id"], receipt)
                sent = feishu_client.send_text(os.environ["COS_PROBE_CHAT_ID"],
                                              receipt["reply"], uuid_key=receipt["uuid"])
                receipt.update(sent=True, reply_message_id=sent.get("data", {}).get("message_id"))
                store.save_document("probe_delivery", USER, message["message_id"], receipt)
    delivered_elapsed = (time.time()-started)*1000
    # Independently decode exactly the committed version, without Store totals.
    raw = objects.get(store._key(store.revision))
    if raw is None:
        raise RuntimeError("committed version temporarily invisible; do not claim audit passed")
    state = json.loads(raw)["state"]
    rows = [r for r in state["intake"] if r["status"] == "valid"]
    facts = {"count": len(rows), "kcal": sum(r["nutrition"]["kcal"] for r in rows),
             "items": [{k: r[k] for k in ("record_id", "food_name", "grams", "date")} for r in rows]}
    compact = {k: result[k] for k in ("status", "reply", "model_calls", "errors", "seeded") if k in result}
    by_operation = {}
    for event in objects.events:
        by_operation[event["operation"]] = round(by_operation.get(event["operation"], 0)+event["duration_ms"], 3)
    return {"operation": operation, "result": compact, "audit": facts,
            "revision": store.revision, "seed_age_seconds": idle,
            "instance_id": INSTANCE_ID,
            "delivery": "feishu" if os.getenv("COS_PROBE_CHAT_ID") else "function_return",
            "message_elapsed_ms": round(delivered_elapsed, 3),
            "function_elapsed_ms": round((time.time()-started)*1000, 3),
            "cos_requests": len(objects.events), "cos_ms": by_operation, "bytes": objects.bytes,
            "limitations": ["synthetic known-food fixtures", "projection is separately invoked manually",
                            *([] if os.getenv("COS_PROBE_CHAT_ID") else ["Feishu delivery disabled"]),
                            "new Store instance is not proof of a new SCF process",
                            "seed age alone does not prove absence of other invocations"]}
