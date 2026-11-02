"""Explicit real candidate acceptance driver. Never imported by production.

Use runner.py --production-live-plan PLAN --production-live-stage STAGE.
The fixed plan supplies real source evidence and independent numerical expected
results. No fixtures, implicit credentials, resource creation or COS deletion.
"""
from copy import deepcopy
import argparse
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
STEPS = ("initial", "record", "revise", "today", "knowledge")
NUTRIENTS = ("kcal", "carbs", "protein", "fat")


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def validate_plan(plan):
    mode = plan.get("entry_mode")
    if mode not in {"synthetic_webhook", "observe_feishu"}:
        raise ValueError("choose synthetic_webhook or observe_feishu entry_mode explicitly")
    for key in ("run_id", "build_id", "bucket", "prefix", "namespace", "user_id", "sender_open_id", "chat_id", "webhook_url", "intake_table_id", "bitable_app_token"):
        if not isinstance(plan.get(key), str) or not plan[key].strip() or "REPLACE" in plan[key]:
            raise ValueError("fill the real candidate plan field before execution: " + key)
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", plan["run_id"]):
        raise ValueError("run_id must be a bounded filename-safe identifier")
    if plan["namespace"] != "default" or plan.get("region") != "ap-shanghai":
        raise ValueError("this release requires default namespace and ap-shanghai")
    if not plan["prefix"].startswith("cos-probe/production-candidate/") or ".." in plan["prefix"].split("/"):
        raise ValueError("an independent production-candidate prefix is required")
    if urlsplit(plan["webhook_url"]).scheme != "https" or not plan["webhook_url"].endswith("/webhook/feishu/events"):
        raise ValueError("a real HTTPS webhook entrance is required")
    if plan.get("webhook_auth") not in {"feishu_token", "cam"}:
        raise ValueError("choose feishu_token or cam webhook_auth explicitly")
    if plan["webhook_auth"] == "cam" and not plan.get("function_url_cam_uin"):
        raise ValueError("CAM function URL requires its actual caller UIN")
    if not plan.get("candidate_table_verified") or plan.get("production_intake_table_id") == plan["intake_table_id"]:
        raise ValueError("verify an independent candidate intake table before writes")
    if not plan.get("source_evidence") or any(not item.get("source") or not item.get("excerpt") for item in plan["source_evidence"]):
        raise ValueError("real food source evidence must be fixed before requests")
    messages = plan.get("messages", [])
    if tuple(item.get("step") for item in messages) != STEPS:
        raise ValueError("exactly five planned messages in initial/record/revise/today/knowledge order are required")
    if mode == "synthetic_webhook" and len({item.get("message_id") for item in messages}) != 5:
        raise ValueError("five fixed unique new message IDs are required")
    for message in messages:
        if (mode == "synthetic_webhook" and not message.get("message_id")) or not message.get("text") or not isinstance(message.get("expected_total_kcal"), (int, float)):
            raise ValueError("each original text, message ID and independent total must be fixed")
        rows = message.get("expected_items")
        if not isinstance(rows, list):
            raise ValueError("each message requires its independent expected_items list")
        for row in rows:
            if not all(row.get(key) for key in ("food_name", "date", "meal_type")) or not isinstance(row.get("estimated"), bool):
                raise ValueError("expected items require date, food, meal scope and estimated flag")
            if not isinstance(row.get("grams"), (int, float)) or any(not isinstance(row.get("nutrition", {}).get(key), (int, float)) for key in NUTRIENTS):
                raise ValueError("expected grams and all nutrition facts must come from the independent source")
        if message["step"] in {"record", "revise", "today", "knowledge"} and len(rows) != 2:
            raise ValueError("the acceptance sequence requires a known two-item meal")
    if not plan.get("knowledge_evidence") or any(not item.get("source") or not item.get("excerpt") for item in plan["knowledge_evidence"]):
        raise ValueError("preselect independent knowledge source excerpts")
    for key in ("pause_confirmation_file", "resume_confirmation_file", "platform_evidence_file"):
        if not plan.get(key):
            raise ValueError("required operator evidence location: " + key)
    return plan


def _post_webhook(plan, payload):
    import httpx
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json"}
    if plan["webhook_auth"] == "cam":
        parsed = urlsplit(plan["webhook_url"])
        if not parsed.hostname.endswith(".ap-shanghai.tencentscf.com") or parsed.query:
            raise ValueError("CAM signing requires the exact Shanghai function URL")
        secret_id = os.getenv("TENCENTCLOUD_SECRET_ID") or os.environ["TENCENTCLOUD_SECRETID"]
        secret_key = os.getenv("TENCENTCLOUD_SECRET_KEY") or os.environ["TENCENTCLOUD_SECRETKEY"]
        stamp = int(time.time())
        day = datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%d")
        canonical = "POST\n" + parsed.path + "\n\ncontent-type:application/json\nhost:" + parsed.hostname + "\n\ncontent-type;host\n" + hashlib.sha256(body).hexdigest()
        scope = day + "/scf/tc3_request"
        signing = "TC3-HMAC-SHA256\n" + str(stamp) + "\n" + scope + "\n" + hashlib.sha256(canonical.encode()).hexdigest()
        def sign(key, value):
            return hmac.new(key, value.encode(), hashlib.sha256).digest()
        key = sign(sign(sign(("TC3" + secret_key).encode(), day), "scf"), "tc3_request")
        signature = hmac.new(key, signing.encode(), hashlib.sha256).hexdigest()
        headers.update(Authorization="TC3-HMAC-SHA256 Credential=" + secret_id + "/" + scope + ", SignedHeaders=content-type;host, Signature=" + signature,
                       **{"X-Scf-Cam-Uin": str(plan["function_url_cam_uin"]), "X-Scf-Cam-Timestamp": str(stamp)})
        token = os.getenv("TENCENTCLOUD_SESSION_TOKEN") or os.getenv("TENCENTCLOUD_SESSIONTOKEN")
        if token:
            headers["X-Scf-Cam-Token"] = token
    return httpx.post(plan["webhook_url"], content=body, headers=headers, timeout=15, follow_redirects=False)


class RawCommits:
    """Independent decoder: no production Journal/Store aggregation is reused."""
    def __init__(self, objects, prefix):
        self.objects, self.prefix = objects, prefix.rstrip("/") + "/"

    def snapshot(self, minimum=-1, exact=None):
        base = self.prefix + "commits/"
        numbers = [int(key[len(base):-5]) for key in self.objects.list(base)
                   if key.startswith(base) and key.endswith(".json") and key[len(base):-5].isdigit()]
        revision = exact if exact is not None else max([minimum, *numbers], default=-1)
        current, chain, index = revision, [], {}
        while current >= 0:
            key = base + f"{current:012d}.json"
            raw = self.objects.get(key)
            if raw is None:
                raise RuntimeError("known raw commit is not visible: " + key)
            commit = json.loads(raw)
            if commit.get("schema") != "calorie.cos.v1" or commit["revision"] != current or commit["previous"] != current - 1:
                raise ValueError("raw commit predecessor/identity mismatch")
            if "index" in commit:
                index = deepcopy(commit["index"])
                break
            chain.append(commit)
            current = commit["previous"]
        for commit in reversed(chain):
            for key, value in commit["changes"].items():
                if value is None:
                    index.pop(key, None)
                else:
                    index[key] = value
        return revision, index

    def document(self, index, key):
        ref = index[key]
        raw = self.objects.get(self.prefix + "documents/" + ref + ".json")
        if raw is None or hashlib.sha256(raw).hexdigest() != ref:
            raise RuntimeError("raw committed document unavailable or corrupted: " + key)
        return json.loads(raw)

    def items(self, user_id, minimum=-1, exact=None):
        revision, index = self.snapshot(minimum, exact)
        rows = [self.document(index, key) for key in index if key.startswith("item:")]
        return {"revision": revision, "commit_key": self.prefix + f"commits/{revision:012d}.json",
                "items": [row for row in rows if row["user_id"] == user_id],
                "item_refs": {key: value for key, value in index.items() if key.startswith("item:")}}


def _check_rows(rows, expected, total):
    valid = [row for row in rows if row.get("status", "") in {"", "valid", "active", "normal", "启用", "正常", "有效", "已记录"}]
    if len(valid) != len(expected) or len({row["record_id"] for row in valid}) != len(valid):
        raise AssertionError("independent valid item count/identity mismatch")
    for wanted in expected:
        matches = [row for row in valid if all(row[key] == wanted[key] for key in ("food_name", "date", "meal_type"))]
        if len(matches) != 1:
            raise AssertionError("independent meal/date/food identity mismatch")
        actual = matches[0]
        if actual["grams"] != wanted["grams"] or actual["nutrition"] != wanted["nutrition"]:
            raise AssertionError("independent grams/nutrition mismatch")
        if actual.get("snapshot", {}).get("estimated", False) != wanted["estimated"]:
            raise AssertionError("independent estimated flag mismatch")
    if round(sum(row["nutrition"]["kcal"] for row in valid), 1) != total:
        raise AssertionError("independent daily kcal mismatch")


def _confirmation(path, state):
    evidence = _read_json(path)
    if evidence.get("state") != state or not evidence.get("confirmed") or not evidence.get("observed_at") or not evidence.get("trigger_name") or not evidence.get("evidence"):
        raise ValueError("operator must confirm archive schedule state with actual platform evidence: " + state)
    return evidence


def _quality(message, state, plan):
    result = state.get("result", {})
    if state.get("status") != "completed" or state.get("work_status") == "needs_attention" or result.get("status") != "ok" or result.get("errors"):
        raise AssertionError("message did not complete successfully")
    if not state.get("delivery", {}).get("presented") or not state["delivery"].get("sent_at"):
        raise AssertionError("Feishu delivery is not confirmed")
    facts = result.get("tool_results", [])
    if message["step"] in {"initial", "today"}:
        query = [fact for fact in facts if fact.get("tool") == "query_today"]
        if not query or query[-1].get("data", {}).get("total", {}).get("kcal") != message["expected_total_kcal"]:
            raise AssertionError("query fact does not match independently expected total")
    if message["step"] == "record":
        recorded = [fact for fact in facts if fact.get("tool") == "record_meal"]
        if not recorded or any(fact.get("data", {}).get("unfinished") for fact in recorded) or sum(f.get("data", {}).get("completed_count", 0) for f in recorded) != 2:
            raise AssertionError("both known meal items must commit without unfinished items")
    if message["step"] == "knowledge":
        knowledge = [fact for fact in facts if fact.get("tool") == "query_knowledge"]
        if not knowledge:
            raise AssertionError("knowledge retrieval facts missing")
        entries = [entry for fact in knowledge for entry in fact.get("data", {}).get("entries", [])]
        for wanted in plan["knowledge_evidence"]:
            if not any(wanted["source"] in entry.get("source", "") and wanted["excerpt"] in entry.get("answer", entry.get("evidence", "")) for entry in entries):
                raise AssertionError("knowledge source/excerpt differs from independent expected evidence")
        evidence = "\n".join([fact.get("data", {}).get("evidence", "") for fact in knowledge]
                               + [entry.get("answer", "") + "\n" + entry.get("source", "") for entry in entries])
        if any(number not in evidence for number in re.findall(r"\d+(?:\.\d+)?%?", result.get("reply", ""))):
            raise AssertionError("knowledge reply contains a number absent from retrieval evidence")
        if not any(wanted["source"] in result.get("reply", "") for wanted in plan["knowledge_evidence"]):
            raise AssertionError("knowledge reply is missing its expected source")
    for text in message.get("required_reply_terms", []):
        if text not in result.get("reply", ""):
            raise AssertionError("expected semantic reply term missing: " + text)
    for text in message.get("forbidden_reply_terms", []):
        if text in result.get("reply", ""):
            raise AssertionError("forbidden semantic reply term: " + text)


class Driver:
    def __init__(self, plan, report, save):
        from agent.storage.cos_transport import CosObjects
        from agent.storage.cos_store import CosStore
        self.plan, self.report, self.save = plan, report, save
        self.objects = CosObjects(bucket=plan["bucket"], region=plan["region"])
        self.store = CosStore(self.objects, plan["prefix"], readonly=True)
        self.raw = RawCommits(self.objects, plan["prefix"].rstrip("/") + "/message/")
        self.sync = RawCommits(self.objects, plan["prefix"].rstrip("/") + "/sync/")

    def progress(self):
        revision, index = self.sync.snapshot()
        return {"revision": revision, "refs": {key: value for key, value in index.items() if key.startswith("item:")}}

    def await_result(self, rid):
        deadline = time.monotonic() + self.plan.get("message_wait_seconds", 180)
        while time.monotonic() < deadline:
            self.store.confirm_current()
            state = self.store.load_document("receipt_state", "system", rid)
            if state.get("status") == "completed":
                return state
            if state.get("status") == "needs_attention":
                raise RuntimeError("candidate receipt needs attention: " + json.dumps(state, ensure_ascii=False))
            time.sleep(1)
        raise TimeoutError("candidate receipt result not completed before wait bound")

    def submit(self, message, *, replay=False):
        from agent.storage.execution_pipeline import receipt_id
        token = os.environ["FEISHU_VERIFICATION_TOKEN"]
        rid = receipt_id("feishu", message["message_id"])
        payload = {"schema": "2.0", "header": {"event_id": message["message_id"], "event_type": "im.message.receive_v1", "token": token},
                   "event": {"sender": {"sender_id": {"open_id": self.plan["sender_open_id"]}}, "message": {
                       "message_id": message["message_id"], "chat_id": self.plan["chat_id"], "message_type": "text",
                       "create_time": str(message.setdefault("driver_create_time_ms", int(time.time()*1000))),
                       "content": json.dumps({"text": message["text"]}, ensure_ascii=False)}}}
        sample = {"step": message["step"], "message_id": message["message_id"], "receipt_id": rid, "replay": replay,
                  "entry_mode": "synthetic_webhook", "driver_create_time_ms": message["driver_create_time_ms"], "caller_started_at": time.time()}
        self.report.setdefault("requests", []).append(sample)
        self.save()
        started = time.perf_counter()
        # No automatic HTTP retry: failed samples remain, original fixed key can
        # be explicitly replayed after its acceptance state has been inspected.
        response = _post_webhook(self.plan, payload)
        sample.update(ack_elapsed_ms=round((time.perf_counter()-started)*1000, 3), ack_http_status=response.status_code,
                      caller_ack_at=time.time())
        try:
            sample["ack"] = response.json()
        except ValueError:
            sample["ack"] = {"non_json_response": response.text[:500]}
        self.save()
        if response.status_code != 200 or sample["ack"].get("status") != "accepted" or sample["ack"].get("receipt_id") != rid:
            raise RuntimeError("real webhook did not accept the planned receipt")
        state = self.await_result(rid)
        sample.update(receipt_state=state, t_service_ms=state["delivery"]["t_service_ms"], t_user_ms=None,
                      synthetic_client_to_send_ms=state["delivery"].get("t_user_ms"))
        # Exact raw ingress read independently retains the original date/time.
        inbox = RawCommits(self.objects, self.plan["prefix"].rstrip("/") + "/ingress/")
        _, index = inbox.snapshot()
        receipt = inbox.document(index, "receipt:" + rid)
        if receipt["message"]["user_id"] != self.plan["user_id"] or receipt["message"]["chat_id"] != self.plan["chat_id"] or receipt["message"]["text"] != message["text"]:
            raise AssertionError("persisted normalized receipt identity mismatch")
        measured = round((state["delivery"]["sent_at"] - receipt["received_at"]) * 1000, 3)
        if measured != sample["t_service_ms"]:
            raise AssertionError("service timer was reset or does not include original receipt time")
        sample.update(original_received_at=receipt["received_at"], intake_date=receipt["intake_date"], raw_receipt=receipt)
        self.save()
        return sample

    def observe(self, message):
        from agent.storage.execution_pipeline import receipt_id
        deadline = time.monotonic() + self.plan.get("message_wait_seconds", 180)
        actual = self.plan.get("actual_message_ids", {}).get(message["step"])
        while not actual and time.monotonic() < deadline:
            path = Path(self.plan.get("actual_message_ids_file", "nonexistent-observed-message-map.json"))
            if path.exists():
                actual = _read_json(path).get(message["step"])
            if not actual:
                time.sleep(1)
        if not actual:
            raise ValueError("observe_feishu requires the actual Feishu message ID for " + message["step"])
        if any(sample["message_id"] == actual for sample in self.report.get("requests", [])):
            raise ValueError("observed steps must reference five different real message IDs")
        rid = receipt_id("feishu", actual)
        sample = {"step": message["step"], "message_id": actual, "receipt_id": rid, "replay": False,
                  "entry_mode": "observe_feishu", "from_ingress_observed": True, "observation_started_at": time.time()}
        self.report.setdefault("requests", []).append(sample)
        self.save()
        state = self.await_result(rid)
        inbox = RawCommits(self.objects, self.plan["prefix"].rstrip("/") + "/ingress/")
        _, index = inbox.snapshot()
        receipt = inbox.document(index, "receipt:" + rid)
        if receipt["source"] != "feishu" or any(receipt["message"][key] != value for key, value in
            (("user_id", self.plan["user_id"]), ("chat_id", self.plan["chat_id"]), ("text", message["text"]))):
            raise AssertionError("observed real Feishu message does not match the fixed identity/text plan")
        if receipt["received_at"] < self.report["pause_confirmation"]["observed_at"]:
            raise AssertionError("observed message predates confirmed archive pause")
        delivery = state["delivery"]
        sample.update(receipt_state=state, t_service_ms=delivery["t_service_ms"], t_user_ms=delivery.get("t_user_ms"),
                      original_received_at=receipt["received_at"], intake_date=receipt["intake_date"], raw_receipt=receipt)
        if round((delivery["sent_at"]-receipt["received_at"])*1000, 3) != sample["t_service_ms"]:
            raise AssertionError("observed service timing was reset")
        self.save()
        return sample

    def business(self):
        if self.report.get("requests"):
            raise ValueError("business stage retains a prior sample; use a new fixed run plan after diagnosing the failed sample")
        self.report["pause_confirmation"] = _confirmation(self.plan["pause_confirmation_file"], "paused")
        self.report["sync_before"] = self.progress()
        self.save()
        for message in self.plan["messages"]:
            sample = self.observe(message) if self.plan["entry_mode"] == "observe_feishu" else self.submit(message)
            _quality(message, sample["receipt_state"], self.plan)
            committed_revision = sample["receipt_state"]["delivery"].get("cos_revision")
            if committed_revision is None:
                raise AssertionError("delivery does not reference its committed COS revision")
            audit = self.raw.items(self.plan["user_id"], exact=committed_revision)
            _check_rows(audit["items"], message["expected_items"], message["expected_total_kcal"])
            sample["raw_cos_audit"] = audit
            if self.progress()["refs"] != self.report["sync_before"]["refs"]:
                raise AssertionError("archive watermarks changed while the operator declared archive paused")
            sample["auto_quality_passed"] = True
            self.save()
        original = next(sample for sample in self.report["requests"] if sample["step"] == "record")
        latest = self.report["requests"][-1]["raw_cos_audit"]
        replay_message = deepcopy(self.plan["messages"][1])
        replay_message.update(message_id=original["message_id"], driver_create_time_ms=int(original["raw_receipt"]["message"].get("create_time") or 0))
        replay = self.submit(replay_message, replay=True)
        if any(replay["receipt_state"]["delivery"].get(key) != original["receipt_state"]["delivery"].get(key) for key in ("uuid", "sent_at", "send_result")):
            raise AssertionError("duplicate intake changed the original delivery")
        after = self.raw.items(self.plan["user_id"], latest["revision"])
        if after["item_refs"] != latest["item_refs"]:
            raise AssertionError("duplicate intake changed the latest committed meal")
        replay["raw_cos_audit"] = after
        replay["no_duplicate_or_rollback"] = True
        self.report["business_stage"] = "auto_checks_passed; platform warmth/build and semantic review evidence still required"
        self.save()

    def archive(self):
        if not self.report.get("business_stage"):
            raise ValueError("complete the business stage before archive verification")
        resumed = _confirmation(self.plan["resume_confirmation_file"], "resumed")
        self.report["resume_confirmation"] = resumed
        self.save()
        final = self.plan["messages"][3]
        target = next(sample["raw_cos_audit"] for sample in self.report["requests"] if sample["step"] == "today")
        ids = {row["record_id"] for row in target["items"]}
        # Ten minutes starts at business commit, so manually delayed resumption
        # cannot manufacture a successful window by resetting the timer.
        first = next(sample for sample in self.report["requests"] if sample["step"] == "record")
        commit_at = min(row["created_at"] for row in target["items"]) / 1000
        deadline = commit_at + 600
        while True:
            revision, index = self.sync.snapshot()
            states = {identity: self.sync.document(index, "item:archive:intake:" + identity) for identity in ids if "item:archive:intake:" + identity in index}
            if len(states) == len(ids) and all(state.get("status") == "confirmed" for state in states.values()):
                break
            if time.time() >= deadline:
                raise TimeoutError("archive was not read-back confirmed within ten minutes of the original business commit")
            time.sleep(2)
        from agent.storage.bitable_adapter import BitableStore, _intake_field, _list_records
        from agent.storage.timeutil import extract_text, extract_number
        bitable = BitableStore()
        bitable.app_token, bitable.intake_table = self.plan["bitable_app_token"], self.plan["intake_table_id"]
        actual = _list_records(bitable.app_token, bitable.intake_table)
        selected = [(rid, fields) for rid, fields in actual if extract_text(fields.get("agent_item_id")) in ids]
        if len(selected) != len(ids) or {extract_text(fields["agent_item_id"]) for _, fields in selected} != ids:
            raise AssertionError("direct Feishu readback has missing or duplicate stable candidate IDs")
        decoded = []
        for rid, fields in selected:
            identity = extract_text(fields["agent_item_id"])
            expected_row = next(row for row in target["items"] if row["record_id"] == identity)
            if states[identity]["ack_revision"] != expected_row["agent_revision"] or extract_number(fields.get("agent_revision")) != expected_row["agent_revision"]:
                raise AssertionError("archive watermark/Feishu latest revision mismatch")
            row = bitable._intake_summary_item(identity, fields)
            row.update(date=expected_row["date"], status=extract_text(fields.get(_intake_field("status"), "")))
            # Check date independently against the actual field representation.
            date_value = fields.get(_intake_field("date"))
            if extract_text(date_value) != expected_row["date"] and extract_number(date_value) != bitable._date_to_start_millis(expected_row["date"]):
                raise AssertionError("direct archived date differs from original intake date")
            decoded.append(row)
        _check_rows(decoded, final["expected_items"], final["expected_total_kcal"])
        confirmed_at = max(state["confirmed_at"] for state in states.values())
        self.report["archive"] = {"status": "passed" if confirmed_at <= deadline else "failed_latency", "sync_revision": revision,
                                  "watermarks": states, "direct_feishu_rows": selected, "fixed_cleanup_record_ids": [rid for rid, _ in selected],
                                  "business_commit_lower_bound_at": commit_at,
                                  "archive_ms_upper_bound": round((confirmed_at-commit_at)*1000, 3), "original_received_at": first["original_received_at"],
                                  "operator_resumed_at": resumed["observed_at"], "raw_target_commit": target["commit_key"]}
        self.save()
        if confirmed_at > deadline:
            raise AssertionError("archive exceeded ten-minute release gate")

    def cleanup(self):
        archive = self.report.get("archive", {})
        ids = archive.get("fixed_cleanup_record_ids")
        if not ids or self.plan.get("cleanup_authorization") != "soft-delete-listed-candidate-records":
            raise ValueError("cleanup requires reviewed fixed candidate row IDs and explicit soft-delete authorization")
        from agent.storage.bitable_adapter import BitableStore, _intake_field, _list_records
        bitable = BitableStore()
        bitable.app_token, bitable.intake_table = self.plan["bitable_app_token"], self.plan["intake_table_id"]
        known = {rid: fields["agent_item_id"] for rid, fields in archive["direct_feishu_rows"]}
        before = dict(_list_records(bitable.app_token, bitable.intake_table))
        for rid in ids:
            if rid not in known or before.get(rid, {}).get("agent_item_id") != known[rid]:
                raise ValueError("cleanup row no longer matches the fixed candidate ID")
            bitable._update_record(bitable.intake_table, rid, {_intake_field("status"): "deleted"})
        after = dict(_list_records(bitable.app_token, bitable.intake_table))
        if any(after.get(rid, {}).get(_intake_field("status")) != "deleted" for rid in ids):
            raise AssertionError("candidate cleanup soft deletion not independently confirmed")
        self.report["cleanup"] = {"status": "passed", "record_ids": ids, "cos_recovery_chain_preserved": True, "confirmed_at": time.time()}
        self.save()


def _main_worker(sample, requests):
    """Identify one actual new main-model execution, excluding ingress copies."""
    mid = sample.get("message_id")
    state = sample.get("receipt_state", {})
    if sample.get("replay") is not False or state.get("status") != "completed":
        return None
    if not mid or sum(row.get("message_id") == mid and row.get("replay") is False for row in requests) != 1:
        return None
    if not sample.get("receipt_id") or state.get("receipt_id") != sample["receipt_id"]:
        return None
    received, completed = sample.get("original_received_at"), state.get("completed_at")
    if type(received) not in (int, float) or type(completed) not in (int, float) or completed < received:
        return None
    events = [event for event in state.get("result", {}).get("latency_events", [])
              if event.get("kind") == "model_http" and event.get("purpose") == "main"]
    if not events or any(event.get("message_id") != mid or event.get("outcome") != "ok" or not event.get("id") for event in events):
        return None
    identities = {event.get("instance_id") for event in events}
    if len(identities) != 1:
        return None
    worker = next(iter(identities))
    if not isinstance(worker, str) or not re.fullmatch(r"[0-9a-f]{32}", worker):
        return None
    for event in events:
        started, duration = event.get("started_epoch_ms"), event.get("duration_ms")
        if type(started) not in (int, float) or type(duration) not in (int, float) or duration < 0:
            return None
        if not received*1000 <= started <= started+duration <= completed*1000:
            return None
    return worker, events


def _worker_reuse_proof(report, index, evidence):
    requests = report.get("requests", [])
    current = requests[index]
    actual = _main_worker(current, requests)
    if (not actual or evidence.get("warm_basis") != "worker_process_reuse"
            or evidence.get("process_reused") is not True or evidence.get("worker_instance_id") != actual[0]):
        return None
    for prior_index, prior in enumerate(requests):
        previous = _main_worker(prior, requests)
        if not previous or previous[0] != actual[0] or prior["message_id"] == current["message_id"] or prior["receipt_id"] == current["receipt_id"]:
            continue
        completed = prior["receipt_state"]["completed_at"]
        if completed >= current["original_received_at"]:
            continue
        reference = {"current": f"/requests/{index}/receipt_state/result/latency_events",
                     "prior": f"/requests/{prior_index}/receipt_state/result/latency_events"}
        if evidence.get("reference") != reference:
            continue
        return {"worker_instance_id": actual[0], "current_message_id": current["message_id"],
                "prior_message_id": prior["message_id"], "current_receipt_id": current["receipt_id"],
                "prior_receipt_id": prior["receipt_id"], "current_received_at": current["original_received_at"],
                "prior_completed_at": completed, "reference": reference,
                "current_model_event_ids": [event["id"] for event in actual[1]],
                "prior_model_event_ids": [event["id"] for event in previous[1]]}
    return None


def finish_gates(plan, report):
    path = Path(plan["platform_evidence_file"])
    platform = _read_json(path) if path.exists() else {}
    report["platform_evidence"] = platform
    gates, knowledge = {}, None
    for index, sample in enumerate(report.get("requests", [])):
        if sample["replay"] or "t_service_ms" not in sample:
            continue
        evidence = platform.get("messages", {}).get(sample["message_id"], {})
        cold_start = evidence.get("cold_start")
        if not isinstance(cold_start, bool):
            cold_start = None
        proof = _worker_reuse_proof(report, index, evidence) if cold_start is None else None
        warm_basis = "platform_cold_start_false" if cold_start is False else ("worker_process_reuse" if proof else None)
        ack_ms = sample.get("ack_elapsed_ms", evidence.get("ack_elapsed_ms"))
        gate = {"entry_mode": sample["entry_mode"], "ack_ms": ack_ms, "ack_passed": isinstance(ack_ms, (int, float)) and ack_ms <= 3000,
                "t_service_ms": sample["t_service_ms"], "t_user_ms": sample.get("t_user_ms"),
                "platform_cold_start": cold_start, "warm_basis": warm_basis, "worker_reuse_proof": proof,
                "build_id": evidence.get("build_id"),
                "build_verified": evidence.get("build_id") == plan["build_id"] and bool(evidence.get("reference")),
                "semantic_review_passed": evidence.get("semantic_review_passed") is True,
                "auto_quality_passed": sample.get("auto_quality_passed", False),
                "platform_evidence_reference": evidence.get("reference")}
        if sample["step"] in {"record", "revise", "today"}:
            gate["warm_speed_passed"] = warm_basis is not None and sample["t_service_ms"] <= 5000
            gate["warm_sample_needed"] = warm_basis is None
        if sample["step"] == "knowledge":
            knowledge = {"t_service_ms": sample["t_service_ms"], "target_ms": 10000,
                         "difference_ms": round(sample["t_service_ms"]-10000, 3), "first_load": evidence.get("first_load")}
        gates[sample["step"]] = gate
    report["gates"], report["knowledge_speed"] = gates, knowledge
    report["release_gate_passed"] = (len(gates) == 5 and all(g["ack_passed"] and g["build_verified"] and g["semantic_review_passed"] and g["auto_quality_passed"] for g in gates.values())
                                     and all(gates[step].get("warm_speed_passed") for step in ("record", "revise", "today"))
                                     and report.get("archive", {}).get("status") == "passed" and report.get("cleanup", {}).get("status") == "passed")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--stage", required=True, choices=["business", "archive", "cleanup", "report"])
    args = parser.parse_args(argv)
    plan = validate_plan(_read_json(args.plan))
    report_path = ROOT / "reports/production-candidate" / (plan["run_id"] + ".json")
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = _read_json(report_path) if report_path.exists() else {"schema": "production-candidate.v1", "run_id": plan["run_id"], "plan_sha256": _digest(plan), "fixed_plan": deepcopy(plan), "created_at": time.time(), "stage_attempts": []}
    if report["plan_sha256"] != _digest(plan):
        raise ValueError("a run's independent expected plan cannot be changed after its first sample")
    def save():
        temporary = report_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(report_path)
    attempt = {"stage": args.stage, "started_at": time.time(), "status": "running"}
    report["stage_attempts"].append(attempt)
    save()
    try:
        if args.stage != "report":
            getattr(Driver(plan, report, save), args.stage)()
        finish_gates(plan, report)
        attempt.update(status="completed", completed_at=time.time())
        save()
        print(json.dumps({"report": str(report_path), "stage": args.stage, "release_gate_passed": report["release_gate_passed"], "knowledge_speed": report["knowledge_speed"]}, ensure_ascii=False))
        return int(args.stage == "report" and not report["release_gate_passed"])
    except Exception as exc:
        attempt.update(status="failed", error=f"{type(exc).__name__}:{exc}", failed_at=time.time())
        try:
            finish_gates(plan, report)
        except Exception as evidence_error:
            report.update(release_gate_passed=False, evidence_error=str(evidence_error))
        save()
        print(json.dumps({"report": str(report_path), "stage": args.stage, "error": attempt["error"]}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
