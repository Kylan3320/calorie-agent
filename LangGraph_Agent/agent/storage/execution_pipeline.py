"""Durable ingress and the sole business writer, shared by every live entry.

Ingress and message journals have distinct owners. Events only wake the writer;
the immutable ingress sequence, rather than event arrival order, selects work.
"""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import os
import threading
import time

from .bitable_adapter import message_key, reply_uuid
from .timeutil import PROCESSING_DEADLINE


def receipt_id(source, key):
    return hashlib.sha256((source + ":" + key).encode()).hexdigest()


class Pipeline:
    def __init__(self, objects, prefix, *, store_factory=None, journal_factory=None):
        from .cos_journal import Journal
        self.objects, self.prefix = objects, prefix.rstrip("/") + "/"
        self.store_factory = store_factory
        self.journal_factory = journal_factory or Journal
        self._writer = self._reader = None
        self._inbox_writer = self._inbox_reader = None

    def inbox(self, *, readonly=False, minimum_revision=None):
        attribute = "_inbox_reader" if readonly else "_inbox_writer"
        journal = getattr(self, attribute)
        if journal is None:
            journal = self.journal_factory(self.objects, self.prefix + "ingress/", readonly=readonly)
            setattr(self, attribute, journal)
            if minimum_revision is not None:
                journal.refresh(confirm=not readonly, minimum_revision=minimum_revision)
        else:
            journal.refresh(confirm=not readonly, minimum_revision=minimum_revision)
        return journal

    def store(self):
        if self.store_factory:
            return self.store_factory()
        from .cos_store import CosStore
        if self._writer is None:
            self._writer = CosStore(self.objects, self.prefix)
        return self._writer

    def readonly_store(self):
        from .cos_store import CosStore
        if self._reader is None:
            self._reader = CosStore(self.objects, self.prefix, readonly=True)
        else:
            self._reader.confirm_current()
        return self._reader

    def accept(self, message, *, source="feishu", received_at=None, kind="message", reference=None):
        if os.getenv("INGRESS_MAINTENANCE", "0").lower() in {"1", "true"}:
            raise RuntimeError("maintenance: retry with the original message key")
        if source not in {"feishu", "cli", "dsh", "console", "timer", "recovery"}:
            raise ValueError("unknown message source")
        if not all(message.get(k) for k in ("message_id", "user_id", "chat_id")):
            raise ValueError("message_id, user_id and chat_id are required")
        journal = self.inbox()
        rid = receipt_id(source, message_key(message))
        key = "receipt:" + rid
        existing = journal.get(key)
        received = time.time() if received_at is None else received_at
        # Identity is normalized from the published snapshot, never by Bitable.
        sources = self.readonly_store()
        message = {**message, "user_id": message["user_id"] if source == "timer" else sources.load_user_aliases().get(message["user_id"], message["user_id"])}
        if existing:
            if any(existing["message"][k] != message[k] for k in ("message_id", "user_id", "chat_id")):
                raise ValueError("original message key belongs to another identity")
            return {**existing, "ingress_revision": journal.revision}
        sequence = journal.get("next_sequence", 0) + 1
        offset = getattr(sources, "offset_hours", 8)
        date_anchor = datetime.fromisoformat(reference["scheduled_time"]).timestamp() if kind == "reminder" else received
        day = datetime.fromtimestamp(date_anchor, timezone(timedelta(hours=offset))).date().isoformat()
        receipt = {"receipt_id": rid, "sequence": sequence, "kind": kind, "source": source,
                   "message": message, "received_at": received, "intake_date": day,
                   "reference": reference}
        session_key = "session:" + receipt_id(message["user_id"], message["chat_id"])
        entries = journal.get(session_key, [])
        journal.commit({key: receipt, "next_sequence": sequence,
                        session_key: entries + [{"receipt_id": rid, "sequence": sequence}]})
        return {**receipt, "ingress_revision": journal.revision}

    def wake(self, receipt, headers=None):
        from .scf_dispatch import dispatch_event
        return dispatch_event({"type": "calorie_message", "receipt_id": receipt["receipt_id"],
                               "ingress_revision": receipt["ingress_revision"],
                               "message_id": receipt["message"]["message_id"],
                               "user_id": receipt["message"]["user_id"],
                               "chat_id": receipt["message"]["chat_id"]}, headers=headers)

    def result(self, rid):
        return self.readonly_store().load_document("receipt_state", "system", rid)

    def defer_reminder(self, store, receipt, state, session_id):
        """Finish automatic tick attempts while retaining its original recovery.

        Individual delivery receipts retain confirmed progress. A failed tick
        cannot hold every later reminder date/rule behind the system session.
        """
        rid = receipt["receipt_id"]
        state.update(status="completed", work_status="needs_attention", completed_at=time.time(),
                     unfinished_error=state.get("error", "reminder retry budget exhausted"),
                     result={"status": "error", "scheduled_time": receipt["reference"]["scheduled_time"],
                             "errors": [state.get("error", "reminder retry budget exhausted")]})
        def finish():
            store.save_document("receipt_state", "system", rid, state)
            store.save_document("ingress_cursor", "system", session_id, {"sequence": receipt["sequence"]})
            return {"status": "completed"}
        store.commit_action("receipt-complete:" + rid, finish)
        return state

    def worker(self, event, *, execute=None, send=None):
        attempt_started = time.monotonic()
        from agent import run
        from agent.config import load_config
        from agent.graph import mark_reply_presented
        from . import feishu_client
        from .reminders import check_reminders
        if load_config().preset != "live":
            raise ValueError("live writer requires the live preset")
        inbox = self.inbox(readonly=True, minimum_revision=event.get("ingress_revision"))
        requested = inbox.get("receipt:" + event["receipt_id"])
        if requested is None:
            raise RuntimeError("accepted receipt is temporarily unavailable")
        if any(event.get(k) != requested["message"][k] for k in ("message_id", "user_id", "chat_id")):
            raise ValueError("receipt identity mismatch")
        store = self.store()
        store.confirm_current()
        requested_state = store.load_document("receipt_state", "system", event["receipt_id"])
        if event.get("manual_recovery") and requested_state.get("work_status") == "needs_attention" and requested_state.get("result"):
            # A fact receipt was already delivered after automatic recovery was
            # exhausted. Continue the original execution, keeping that delivery
            # immutable; later user messages read the newly committed facts.
            requested_state["manual_attempts"] = requested_state.get("manual_attempts", 0) + 1
            requested_state["recovery_started_at"] = time.time()
            store.save_document("receipt_state", "system", event["receipt_id"], requested_state)
            deadline = PROCESSING_DEADLINE.set(attempt_started + load_config().deadline_seconds)
            try:
                if requested["kind"] == "reminder":
                    stamp = datetime.fromisoformat(requested["reference"]["scheduled_time"])
                    recovered = check_reminders(store, stamp, send)
                else:
                    recovered = (execute or run)(requested["message"], store=store, config=load_config(), intake_date=requested["intake_date"])
                requested_state.update(recovery_result=deepcopy(recovered), recovered_at=time.time(),
                                       work_status="completed" if recovered.get("status", "ok") == "ok" else "needs_attention")
                store.save_document("receipt_state", "system", event["receipt_id"], requested_state)
                return requested_state
            finally:
                PROCESSING_DEADLINE.reset(deadline)
        if requested_state.get("status") in {"completed", "awaiting_presentation"}:
            return {**requested_state, "duplicate": True}
        session_id = receipt_id(requested["message"]["user_id"], requested["message"]["chat_id"])
        cursor = store.load_document("ingress_cursor", "system", session_id).get("sequence", 0)
        entries = inbox.get("session:" + session_id, [])
        receipts = [inbox.get("receipt:" + entry["receipt_id"]) for entry in entries
                    if cursor < entry["sequence"] <= requested["sequence"]]
        if any(r is None for r in receipts):
            raise RuntimeError("accepted predecessor temporarily unavailable")
        requested_state = None
        for receipt in receipts:
            rid = receipt["receipt_id"]
            state = store.load_document("receipt_state", "system", rid)
            if not state and receipt["kind"] == "message" and store.load_document("legacy_message", "system", receipt["message"]["message_id"]).get("completed"):
                # A legacy intake fact, including a soft deletion, proves this
                # message was processed even if old receipt audit is absent.
                state = {"status": "completed", "attempts": 0, "migrated": True,
                         "receipt_id": rid, "sequence": receipt["sequence"]}
                def finish_legacy():
                    store.save_document("receipt_state", "system", rid, state)
                    store.save_document("ingress_cursor", "system", session_id, {"sequence": receipt["sequence"]})
                    return {"status": "completed"}
                store.commit_action("legacy-receipt:" + rid, finish_legacy)
            if state.get("status") in {"completed", "awaiting_presentation"}:
                if rid == event["receipt_id"]:
                    requested_state = {**state, "duplicate": True}
                continue
            if receipt["kind"] == "reminder" and (state.get("status") == "needs_attention" or state.get("attempts", 0) >= 3) and not event.get("manual_recovery"):
                requested_state = self.defer_reminder(store, receipt, state, session_id)
                continue
            if state.get("status") == "needs_attention" and not event.get("manual_recovery"):
                raise RuntimeError("earlier receipt requires recovery: " + rid)
            if state.get("attempts", 0) >= 3 and not event.get("manual_recovery"):
                raise RuntimeError("receipt retry budget exhausted: " + rid)
            state = {**state, "attempts": state.get("attempts", 0) + 1, "status": "processing",
                     "receipt_id": rid, "sequence": receipt["sequence"], "started_at": time.time()}
            store.save_document("receipt_state", "system", rid, state)
            deadline = PROCESSING_DEADLINE.set(attempt_started + load_config().deadline_seconds)
            message = receipt["message"]
            try:
                if receipt["kind"] == "presented":
                    reference = receipt["reference"]
                    original = store.load_document("receipt_state", "system", reference["receipt_id"])
                    original_receipt = inbox.get("receipt:" + reference["receipt_id"])
                    if not original_receipt or original_receipt["source"] != receipt["source"] or original_receipt["message"]["user_id"] != message["user_id"] or original_receipt["message"]["chat_id"] != message["chat_id"]:
                        raise ValueError("presentation identity mismatch")
                    if not original.get("result") or original.get("delivery", {}).get("uuid") != reference.get("uuid"):
                        raise ValueError("presentation does not reference the saved reply")
                    mark_reply_presented(store, message["user_id"], message["chat_id"], original["result"])
                    original["status"] = "completed"
                    original["delivery"].update(presented=True, presented_at=reference["presented_at"])
                    store.save_document("receipt_state", "system", reference["receipt_id"], original)
                    state.update(status="completed")
                elif receipt["kind"] == "reminder":
                    stamp = datetime.fromisoformat(receipt["reference"]["scheduled_time"].replace("Z", "+00:00"))
                    state.update(status="completed", result=check_reminders(store, stamp, send))
                else:
                    if not state.get("result"):
                        if receipt["source"] == "feishu":
                            settings = store.load_document("user_settings", message["user_id"])
                            if settings.get("feishu_chat_id") != message["chat_id"]:
                                store.save_document("user_settings", message["user_id"], "", {**settings, "feishu_chat_id": message["chat_id"]})
                        result = (execute or run)(message, store=store, config=load_config(), intake_date=receipt["intake_date"])
                        errors = ";".join(result.get("errors", []))
                        deterministic = errors.startswith(("KeyError:", "ValueError:", "PermissionError:", "stale_execution_requires_new_message"))
                        if result.get("status") == "error" and state["attempts"] < 3 and not deterministic:
                            raise RuntimeError(errors)
                        if result.get("status") == "error":
                            state.update(work_status="needs_attention", unfinished_error=errors)
                        # Commit final text and reference before any delivery API.
                        state.update(result=deepcopy(result), delivery={"uuid": reply_uuid(message), "presented": False})
                        store.save_document("receipt_state", "system", rid, state)
                    if receipt["source"] == "feishu":
                        delivery = state["delivery"]
                        from .telemetry import span
                        with span("send_reply", receipt_id=rid):
                            sent = (send or feishu_client.send_text)(message["chat_id"], state["result"]["reply"], uuid_key=delivery["uuid"])
                        now = time.time()
                        delivery.update(presented=True, sent_at=now, send_result=sent,
                                        t_service_ms=round((now - receipt["received_at"]) * 1000, 3),
                                        cos_revision=store.journal.revision)
                        import json
                        from pathlib import Path
                        from .telemetry import EVENTS
                        build_file = Path(__file__).resolve().parents[2] / "BUILD_ID"
                        delivery["t_user_ms"] = round(now*1000-int(message["create_time"]), 3) if message.get("create_time", "").isdigit() else None
                        print(json.dumps({"event": "latency_delivery", "message_id": message["message_id"],
                                          "receipt_id": rid, "sequence": receipt["sequence"], "source": receipt["source"],
                                          "build_id": build_file.read_text().strip() if build_file.exists() else "development",
                                          "events": list(EVENTS.get() or []), **delivery}, ensure_ascii=False))
                        mark_reply_presented(store, message["user_id"], message["chat_id"], state["result"])
                        state["status"] = "completed"
                    else:
                        state["status"] = "awaiting_presentation"
                state["completed_at"] = time.time()
                def finish_receipt():
                    store.save_document("receipt_state", "system", rid, state)
                    store.save_document("ingress_cursor", "system", session_id, {"sequence": receipt["sequence"]})
                    if receipt["kind"] == "message":
                        from .bitable_adapter import usage_event_token
                        store.append_usage_event({"event_id": usage_event_token(message_key(message)),
                            "event_type": "message_processed", "message_id": message_key(message),
                            "user_id": message["user_id"], "source": receipt["source"], "preset": "live",
                            "status": state["result"].get("status"), "receipt_id": rid,
                            "reply_sent": state["delivery"]["presented"],
                            "latency_ms": round((time.time()-receipt["received_at"])*1000)})
                    return {"status": state["status"]}
                store.commit_action("receipt-complete:" + rid, finish_receipt)
                requested_state = state
                attempt_started = time.monotonic()
            except Exception as exc:
                state.update(error=f"{type(exc).__name__}:{str(exc)[:300]}",
                             status="needs_attention" if state["attempts"] >= 3 or isinstance(exc, (ValueError, KeyError)) else "retry")
                if receipt["kind"] == "reminder" and state["attempts"] >= 3:
                    requested_state = self.defer_reminder(store, receipt, state, session_id)
                    attempt_started = time.monotonic()
                    continue
                # An uncertain Store must reject this save too; original task and
                # action keys survive and the next attempt starts with a barrier.
                store.save_document("receipt_state", "system", rid, state)
                raise
            finally:
                PROCESSING_DEADLINE.reset(deadline)
        return requested_state or {"status": "completed", "duplicate": True}

    def recover(self, *, wake=None):
        """Maintenance only wakes; it never writes business/session state."""
        inbox = self.inbox(readonly=True)
        pending = []
        store = self.readonly_store()
        for key in inbox.state:
            if not key.startswith("session:"):
                continue
            cursor = store.load_document("ingress_cursor", "system", key.removeprefix("session:")).get("sequence", 0)
            for entry in inbox.get(key, []):
                if entry["sequence"] <= cursor:
                    continue
                state = store.load_document("receipt_state", "system", entry["receipt_id"])
                if state.get("status") not in {"completed", "awaiting_presentation", "needs_attention"} and state.get("attempts", 0) < 3:
                    pending.append(inbox.get("receipt:" + entry["receipt_id"]))
        for receipt in sorted(pending, key=lambda r: r["sequence"]):
            (wake or self.wake)({**receipt, "ingress_revision": inbox.revision})
        return {"woken": len(pending)}


_PIPELINE = None
_PIPELINE_KEY = None
_PIPELINE_LOCK = threading.Lock()


def configured_pipeline(headers=None):
    """Reuse only the current role credential group, replacing expired clients.

    Web SCF injects credentials into request headers; Event SCF exposes the
    current group in the environment. Credentials never enter persisted events.
    """
    from .cos_transport import CosObjects
    global _PIPELINE, _PIPELINE_KEY
    headers = headers or {}
    credentials = (
        headers.get("X-Scf-Secret-Id") or os.getenv("TENCENTCLOUD_SECRET_ID") or os.getenv("TENCENTCLOUD_SECRETID", ""),
        headers.get("X-Scf-Secret-Key") or os.getenv("TENCENTCLOUD_SECRET_KEY") or os.getenv("TENCENTCLOUD_SECRETKEY", ""),
        headers.get("X-Scf-Session-Token") or os.getenv("TENCENTCLOUD_SESSION_TOKEN") or os.getenv("TENCENTCLOUD_SESSIONTOKEN", ""),
    )
    key = (credentials, os.environ["COS_PREFIX"], os.getenv("COS_BUCKET"), os.getenv("COS_REGION", "ap-shanghai"))
    with _PIPELINE_LOCK:
        if _PIPELINE_KEY != key:
            pipeline = Pipeline(CosObjects(credentials=credentials), key[1])
            _PIPELINE, _PIPELINE_KEY = pipeline, key
        return _PIPELINE
