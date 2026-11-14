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
        self._sync = None
        from .generations import GenerationControl
        self.control = GenerationControl(objects, self.prefix)
        self._location = None
        self._alias_snapshot = None

    def inbox(self, *, readonly=False, minimum_revision=None):
        exact_receipt = readonly and minimum_revision is not None
        epoch, prefix = self.control.location()
        if self._location != (epoch, prefix):
            self._inbox_writer = self._inbox_reader = self._writer = self._reader = None
            self._sync = None
            self._location = (epoch, prefix)
        attribute = "_inbox_reader" if readonly else "_inbox_writer"
        floor = self.control.minimum('ingress', confirmed=True)
        if floor is not None:
            minimum_revision = floor if minimum_revision is None else max(minimum_revision, floor)
        journal = getattr(self, attribute)
        if journal is None:
            journal = self.journal_factory(self.objects, prefix + "ingress/", readonly=readonly,
                                           guard=lambda: self.control.guard(epoch, prefix))
            setattr(self, attribute, journal)
            if minimum_revision is not None and journal.revision < minimum_revision:
                journal.refresh(confirm=not readonly, minimum_revision=minimum_revision)
        elif exact_receipt:
            # The accepted trigger carries the exact ingress revision needed
            # through its own sequence. Ahead-of-request receipts are immaterial.
            journal.confirm_readable(minimum_revision)
        else:
            journal.refresh(confirm=not readonly, minimum_revision=minimum_revision)
        return journal

    def store(self):
        if self.store_factory:
            return self.store_factory()
        from .cos_store import CosStore
        location = self.control.location()
        if self._location != location:
            self._writer = self._reader = self._inbox_writer = self._inbox_reader = None
            self._sync = None
            self._location = location
        if self._writer is None:
            self._writer = CosStore(self.objects, self.prefix, pack=os.getenv("CALORIE_PACK_MESSAGE_COMMITS") == "1",
                                    control=self.control)
        return self._writer

    def readonly_store(self):
        from .cos_store import CosStore
        location = self.control.location()
        if self._location != location:
            self._writer = self._reader = self._inbox_writer = self._inbox_reader = None
            self._sync = None
            self._location = location
        if self._reader is None:
            self._reader = CosStore(self.objects, self.prefix, readonly=True)
        else:
            self._reader.confirm_current()
        return self._reader

    def sync_progress(self, *, generation):
        epoch, prefix = self.control.location()
        if prefix != generation:
            from .generations import MaintenancePaused
            raise MaintenancePaused('sync input generation changed; reopen current view')
        if self._location != (epoch, prefix):
            self._writer = self._reader = self._inbox_writer = self._inbox_reader = self._sync = None
            self._location = (epoch, prefix)
        if self._sync is None:
            self._sync = self.journal_factory(self.objects, prefix + 'sync/',
                                              guard=lambda: self.control.guard(epoch, prefix))
        else:
            self._sync.refresh()
        return self._sync

    def ingress_identity(self, user_id):
        snapshot = self._alias_snapshot
        if snapshot is None or snapshot[0] != self._location:
            sources = self.readonly_store()
            aliases = sources.load_user_aliases()
            # The baseline publishes aliases once per generation.
            if sources.journal.get('baseline'):
                self._alias_snapshot = (self._location, aliases)
        else:
            aliases = snapshot[1]
        return aliases.get(user_id, user_id)

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
        if source != "timer":
            message = {**message, "user_id": self.ingress_identity(message["user_id"])}
        if existing:
            if any(existing["message"][k] != message[k] for k in ("message_id", "user_id", "chat_id")):
                raise ValueError("original message key belongs to another identity")
            return {**existing, "ingress_revision": journal.revision, "ingress_generation":journal.prefix.removesuffix('ingress/')}
        sequence = journal.get("next_sequence", 0) + 1
        date_anchor = datetime.fromisoformat(reference["scheduled_time"]).timestamp() if kind == "reminder" else received
        day = datetime.fromtimestamp(date_anchor, timezone(timedelta(hours=8))).date().isoformat()
        receipt = {"receipt_id": rid, "sequence": sequence, "kind": kind, "source": source,
                   "message": message, "received_at": received, "intake_date": day,
                   "reference": reference}
        session_key = "session:" + receipt_id(message["user_id"], message["chat_id"])
        head = journal.get(session_key, {})
        if journal.format == 1:
            entries = head if isinstance(head, list) else []
            journal.commit({key:receipt, 'next_sequence':sequence,
                            session_key:entries + [{'receipt_id':rid, 'sequence':sequence}]})
        else:
            position = head.get('count', 0)
            page_key = session_key + f':page:{position // 32:012d}'
            page = journal.get(page_key, [])
            receipt['session_position'] = position
            journal.commit({key: receipt, 'next_sequence':sequence, session_key:{'count':position+1},
                            page_key:page + [{'receipt_id':rid, 'sequence':sequence, 'position':position}]})
        return {**receipt, "ingress_revision": journal.revision, "ingress_generation":journal.prefix.removesuffix('ingress/')}

    def wake(self, receipt, headers=None):
        from .scf_dispatch import dispatch_event
        return dispatch_event({"type": "calorie_message", "receipt_id": receipt["receipt_id"],
                               "ingress_revision": receipt["ingress_revision"], "ingress_generation":receipt.get('ingress_generation',self.prefix),
                               "message_id": receipt["message"]["message_id"],
                               "user_id": receipt["message"]["user_id"],
                               "chat_id": receipt["message"]["chat_id"]}, headers=headers)

    def result(self, rid):
        return self.readonly_store().load_receipt(rid, include_audit=True)

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
            store.save_document("ingress_cursor", "system", session_id, {"sequence": max(receipt["sequence"], store.load_document("ingress_cursor", "system", session_id).get("sequence", 0)), "position":receipt.get("session_position", -1)})
            return {"status": "completed"}
        store.commit_action("receipt-complete:" + rid, finish)
        return state

    def budget_message(self, receipt, state):
        from agent.config import load_config
        if "deadline_at" not in state:
            origin = receipt.get("received_at")
            state["legacy_budget"] = origin is None
            state["budget_started_at"] = origin if origin is not None else time.time()
            state["deadline_at"] = state["budget_started_at"] + load_config().deadline_seconds
        return {**receipt["message"], "received_at": receipt.get("received_at"), "delivery_source": receipt["source"],
                "budget_started_at": state["budget_started_at"], "legacy_budget": state["legacy_budget"],
                "deadline_at": state["deadline_at"], "recovery_attempt": state.get("attempts", 0) + state.get("manual_attempts", 0)}

    def present(self, store, receipt):
        try:
            return self._confirm_presented(store, receipt)
        except ValueError as exc:
            # Reject only this control acknowledgement. It must neither confirm
            # the business reply nor block later messages in the same queue.
            rejected = {"status": "completed", "presentation_rejected": True,
                        "error": str(exc), "receipt_id": receipt["receipt_id"],
                        "sequence": receipt["sequence"]}
            store.save_document("receipt_state", "system", receipt["receipt_id"], rejected)
            return rejected

    def _confirm_presented(self, store, receipt):
        from agent.graph import mark_reply_presented
        message, reference = receipt["message"], receipt["reference"]
        original = store.load_document("receipt_state", "system", reference["receipt_id"])
        original_receipt = self.inbox(readonly=True).get("receipt:" + reference["receipt_id"])
        if not original_receipt or original_receipt["source"] != receipt["source"] or any(original_receipt["message"][key] != message[key] for key in ("user_id", "chat_id")):
            raise ValueError("presentation identity mismatch")
        delivery = original.get("delivery", {})
        if not original.get("result") or delivery.get("uuid") != reference.get("uuid"):
            raise ValueError("presentation does not reference the saved reply")
        expected = delivery.get("result_digest")
        if expected and reference.get("result_digest") != expected and (reference.get("result_digest") or delivery.get("progress_reply") is not None):
            raise ValueError("presentation references an older reply")
        mark_reply_presented(store, message["user_id"], message["chat_id"], {"dialogue_turn_id": "calorie-user:" + message_key(original_receipt["message"]), **original["result"]})
        original["status"] = "needs_attention" if original.get("work_status") == "needs_attention" else "completed"
        delivery.update(presented=True, presented_at=reference["presented_at"])
        original['completed_at'] = time.time()
        store.save_document("receipt_state", "system", reference["receipt_id"], original)
        state = {"status": "completed", "receipt_id": receipt["receipt_id"], "sequence": receipt["sequence"]}
        store.save_document("receipt_state", "system", receipt["receipt_id"], state)
        return state

    def deliver(self, store, receipt, state, send=None, update_send=None):
        import json
        from pathlib import Path
        from . import feishu_client
        from .telemetry import span, EVENTS
        message, delivery = receipt["message"], state["delivery"]
        delivery["result_digest"] = hashlib.sha256(state["result"]["reply"].encode()).hexdigest()
        unresolved = state.get("work_status") == "needs_attention"
        if receipt["source"] != "feishu":
            state["status"] = "needs_attention" if unresolved else "awaiting_presentation"
            return
        started = time.monotonic()
        with span("send_reply", receipt_id=receipt["receipt_id"]):
            if delivery.get("mode") == "action_note_edit":
                mid = delivery["message_id"]
                delivery["update_result"] = (update_send or feishu_client.update_text)(mid, state["result"]["reply"])
                sent = {"data": {"message_id": mid}}
            elif "progress_reply" in delivery:
                mid = delivery.get("message_id") or delivery.get("send_result", {}).get("data", {}).get("message_id")
                if not mid:
                    prior = (send or feishu_client.send_text)(message["chat_id"], delivery["progress_reply"], uuid_key=delivery["uuid"])
                    mid = prior.get("data", {}).get("message_id")
                    if not mid:
                        raise RuntimeError("original progress delivery is not confirmed")
                    delivery.update(send_result=prior, message_id=mid)
                    store.save_document("receipt_state", "system", receipt["receipt_id"], state)
                delivery["update_result"] = (update_send or feishu_client.update_text)(mid, state["result"]["reply"])
                sent = delivery["send_result"]
            else:
                sent = (send or feishu_client.send_text)(message["chat_id"], state["result"]["reply"], uuid_key=delivery["uuid"])
        now = time.time()
        delivery.update(presented=True, sent_at=now, send_result=sent, message_id=sent.get("data", {}).get("message_id"),
                        t_service_ms=round((now - receipt["received_at"]) * 1000, 3) if receipt.get("received_at") is not None else None,
                        send_duration_ms=round((time.monotonic() - started) * 1000, 3), cos_revision=store.journal.revision)
        delivery["slow_request"] = delivery["t_service_ms"] is not None and delivery["t_service_ms"] > 10000
        delivery["t_user_ms"] = round(now * 1000 - int(message["create_time"]), 3) if message.get("create_time", "").isdigit() else None
        build = Path(__file__).resolve().parents[2] / "BUILD_ID"
        print(json.dumps({"event": "latency_delivery", "message_id": message["message_id"], "receipt_id": receipt["receipt_id"],
                          "sequence": receipt["sequence"], "source": receipt["source"], "build_id": build.read_text().strip() if build.exists() else "development",
                          "events": list(EVENTS.get() or []), **delivery}, ensure_ascii=False))
        state["status"] = "needs_attention" if unresolved else "completed"

    def confirm_delivery(self, store, receipt, state):
        from agent.graph import mark_reply_presented
        message = receipt["message"]
        if state["delivery"]["presented"]:
            mark_reply_presented(store, message["user_id"], message["chat_id"],
                                 {"dialogue_turn_id": "calorie-user:" + message_key(message), **state["result"]},
                                 atomic=False)

    def stage_reply(self, message, state, result, execution_id=None):
        state = deepcopy(state)
        if execution_id:
            state["execution_id"] = execution_id
            result = {key: result[key] for key in ("status", "reply", "reply_source", "business_status",
                "completion_status", "dialogue_turn_id", "presented_candidates", "proposal_id", "terminal", "model_calls")}
        state.update(result=deepcopy(result), delivery={"uuid": reply_uuid(message), "presented": False,
                     "result_digest": hashlib.sha256(result["reply"].encode()).hexdigest(), "mode": "send"})
        notice = state.get("action_notice", {})
        if notice.get("status") == "confirmed" and notice.get("message_id"):
            state["delivery"].update(mode="action_note_edit", message_id=notice["message_id"])
        return state

    def action_note(self, store, receipt, state, text, send=None, **context):
        from . import feishu_client
        from .bitable_adapter import stable_uuid
        from .model_transport import deadline_scope
        from .timeutil import remaining_timeout
        from .telemetry import span
        if state.get("action_notice"):
            return state["action_notice"]
        notice = {"text": text, "status": "attempted", "attempted_at": time.time(),
                  "uuid": stable_uuid("action-note:" + reply_uuid(receipt["message"])), **context}
        state["action_notice"] = notice
        store.save_document("receipt_state", "system", receipt["receipt_id"], state)
        try:
            with span("action_note_send", receipt_id=receipt["receipt_id"]), deadline_scope(time.monotonic() + remaining_timeout(1)):
                response = (send or feishu_client.send_action_note)(receipt["message"]["chat_id"], text, uuid_key=notice["uuid"])
                mid = response.get("data", {}).get("message_id")
                notice.update(status="confirmed" if mid else "unknown", message_id=mid, confirmed_at=time.time() if mid else None)
        except Exception as exc:
            notice.update(status="unknown", error=type(exc).__name__)
        store.save_document("receipt_state", "system", receipt["receipt_id"], state)
        return deepcopy(notice)

    def recover_result(self, store, receipt, state, execute, send, update_send):
        from agent import run
        from agent.config import load_config
        state["manual_attempts"] = state.get("manual_attempts", 0) + 1
        message = self.budget_message(receipt, state)
        state["recovery_started_at"] = time.time()
        store.save_document("receipt_state", "system", receipt["receipt_id"], state)
        token = PROCESSING_DEADLINE.set(min(PROCESSING_DEADLINE.get() or float("inf"), time.monotonic() + load_config().deadline_seconds))
        try:
            recovered = (execute or run)(message, store=store, config=load_config(), intake_date=receipt["intake_date"])
            state["recovery_result"] = deepcopy(recovered)
            if recovered.get("status") == "error" and not recovered.get("terminal"):
                state.update(work_status="needs_attention", recovered_at=time.time())
                store.save_document("receipt_state", "system", receipt["receipt_id"], state)
                return state
            state["delivery"].setdefault("progress_reply", state["result"]["reply"])
            state["delivery"]["presented"] = False
            state["result"] = deepcopy(recovered)
            state["delivery"]["result_digest"] = hashlib.sha256(recovered["reply"].encode()).hexdigest()
            state["delivery"]["recovery_attempts"] = 1
            state["work_status"] = "terminated" if recovered.get("terminal") else "completed"
            state["status"] = "retry"
            store.save_document("receipt_state", "system", receipt["receipt_id"], state)
            self.deliver(store, receipt, state, send, update_send)
            state["recovered_at"] = time.time()
            session = receipt_id(receipt["message"]["user_id"], receipt["message"]["chat_id"])
            def finish():
                self.confirm_delivery(store, receipt, state)
                store.save_document("receipt_state", "system", receipt["receipt_id"], state)
                cursor = store.load_document("ingress_cursor", "system", session)
                store.save_document("ingress_cursor", "system", session, {"sequence": max(cursor.get('sequence',0), receipt["sequence"]),
                    'position':max(cursor.get('position',-1),receipt.get('session_position',-1))})
                return {"status": state["status"]}
            store.commit_action("receipt-complete:" + receipt["receipt_id"], finish)
            return state
        finally:
            PROCESSING_DEADLINE.reset(token)

    def worker(self, event, *, execute=None, send=None, update_send=None, note_send=None):
        attempt_started = time.monotonic()
        worker_started_at = time.time()
        from agent.config import load_config
        config = load_config()
        controls={'calorie_task','calorie_task_tick','calorie_reminder_check','calorie_reminder_recover','calorie_maintenance_pause'}
        if event.get('type') in controls and config.preset!='live':
            return {'status':'skipped_preset','sent':0}
        if event.get('type') == 'calorie_task_tick':
            from . import tasks
            from .task_runtime import dispatch_pending
            store = self.store();store.confirm_current()
            result=tasks.tick(store,config);dispatch_pending(store)
            return result
        if event.get('type') == 'calorie_task':
            from .task_runtime import writer, dispatch_pending
            store = self.store()
            store.confirm_current()
            from . import feishu_client
            result = writer(store,event,config,send=send or feishu_client.send_text)
            if event['operation'] != 'claim':
                dispatch_pending(store,user_id=event['user_id'])
            return result
        if event.get('type') == 'calorie_maintenance_pause':
            from .cos_journal import Journal
            pause = self.control.pause_event(event)
            if pause is None:
                return {'status': 'obsolete_pause'}
            epoch, prefix = pause['epoch'], pause['source']
            # Establish the sole writer's exact barrier before starting its
            # watchdog; a fresh readonly LIST cannot resolve an unknown PUT.
            journal = Journal(self.objects, prefix + 'message/')
            decision=self.control.message_wait(epoch, journal.revision)
            from .reminders import catch_up
            started=self.control.read(self.control.key(epoch,'started'))
            store=self.store();store.confirm_current()
            return {**decision,'reminders':catch_up(store,started['started_at'],time.time(),send)}
        if event.get('type') == 'calorie_reminder_recover':
            from .reminders import recover_deliveries
            store=self.store();store.confirm_current()
            return recover_deliveries(store,send,manual=bool(event.get('manual_recovery')))
        if event.get('type') == 'calorie_reminder_check':
            from .reminders import check_reminders
            store = self.store()
            store.confirm_current()
            from . import tasks
            from .task_runtime import dispatch_pending
            # Reuse the one-minute message timer. Recovery is independent of archive.
            if store.load_document('task_active','system') or store.load_document('worker_active','system'):
                tasks.tick(store,config)
                dispatch_pending(store)
            return check_reminders(store, datetime.fromisoformat(event['scheduled_time']), send)
        if event.get("type") == "calorie_diet_goal":
            if config.preset != "live":
                return {"status": "skipped_preset"}
            from . import diet_goal_runtime, feishu_client
            store = self.store()
            store.confirm_current()
            return diet_goal_runtime.writer(store, event, config, send=send or feishu_client.send_text)
        if event.get("type") in {"calorie_experience", "calorie_memory_backfill"}:
            if config.preset != "live":
                return {"status": "skipped_preset"}
            store = self.store()
            store.confirm_current()
            if event["type"] == "calorie_experience":
                from .experience import apply_candidate
                return apply_candidate(store, event, config)
            from .memory_backfill import backfill_user
            return backfill_user(store, self.inbox(readonly=True), event["user_id"], event.get("limit", 25))
        if event.get("type") == "calorie_summary":
            from .conversation import apply_candidate
            store = self.store()
            store.confirm_current()
            return apply_candidate(store, event)
        from agent import run
        from .reminders import check_reminders
        if config.preset != "live":
            raise ValueError("live writer requires the live preset")
        minimum = self.control.translate_ingress(event.get('ingress_generation'),event.get('ingress_revision'))
        inbox = self.inbox(readonly=True, minimum_revision=minimum)
        requested = inbox.get("receipt:" + event["receipt_id"])
        if requested is None:
            raise RuntimeError("accepted receipt is temporarily unavailable")
        if any(event.get(k) != requested["message"][k] for k in ("message_id", "user_id", "chat_id")):
            raise ValueError("receipt identity mismatch")
        store = self.store()
        store.confirm_current()
        requested_state = store.load_document("receipt_state", "system", event["receipt_id"])
        if requested["kind"] == "presented":
            if requested_state.get("status") == "completed":
                return {**requested_state, "duplicate": True}
            return self.present(store, requested)
        if event.get("manual_recovery") and requested_state.get("work_status") == "needs_attention" and requested["kind"] == "reminder":
            requested_state["manual_attempts"] = requested_state.get("manual_attempts", 0) + 1
            token = PROCESSING_DEADLINE.set(min(PROCESSING_DEADLINE.get() or float("inf"), attempt_started + load_config().deadline_seconds))
            try:
                recovered = check_reminders(store, datetime.fromisoformat(requested["reference"]["scheduled_time"]), send, manual=True)
                requested_state.update(recovery_result=deepcopy(recovered), recovered_at=time.time(),
                                       work_status="completed" if recovered.get("status", "ok") == "ok" else "needs_attention")
                store.save_document("receipt_state", "system", event["receipt_id"], requested_state)
                return requested_state
            finally:
                PROCESSING_DEADLINE.reset(token)
        if event.get("manual_recovery") and requested_state.get("work_status") == "needs_attention" and requested_state.get("result") and requested["kind"] == "message":
            return self.recover_result(store, requested, requested_state, execute, send, update_send)
        if requested_state.get("status") in {"completed", "awaiting_presentation"}:
            from .task_runtime import dispatch_pending
            dispatch_pending(store,user_id=requested['message']['user_id'])
            return {**requested_state, "duplicate": True}
        session_id = receipt_id(requested["message"]["user_id"], requested["message"]["chat_id"])
        cursor_state = store.load_document("ingress_cursor", "system", session_id)
        cursor = cursor_state.get("sequence", 0)
        head = inbox.get("session:" + session_id, [])
        if isinstance(head, list):
            entries = head
        else:
            from_position = cursor_state.get('position', -1) + 1
            to_position = requested['session_position']
            entries = [entry for page in range(from_position // 32, to_position // 32 + 1)
                       for entry in inbox.get('session:' + session_id + f':page:{page:012d}', [])]
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
                    store.save_document("ingress_cursor", "system", session_id, {"sequence": max(receipt["sequence"], store.load_document("ingress_cursor", "system", session_id).get("sequence", 0)), "position":receipt.get("session_position", -1)})
                    return {"status": "completed"}
                store.commit_action("legacy-receipt:" + rid, finish_legacy)
            if state.get("status") in {"completed", "awaiting_presentation"}:
                if rid == event["receipt_id"]:
                    requested_state = {**state, "duplicate": True}
                continue
            if receipt["kind"] == "reminder" and (state.get("status") == "needs_attention" or state.get("attempts", 0) >= 3) and not event.get("manual_recovery"):
                requested_state = self.defer_reminder(store, receipt, state, session_id)
                continue
            if state.get('status')=='needs_attention' and not event.get('manual_recovery'):
                original_deadline=state.get('deadline_at')
                if original_deadline is None and receipt.get('received_at') is not None:
                    original_deadline=receipt['received_at']+config.deadline_seconds
                if original_deadline is not None and time.time()>=original_deadline:
                    # The confirmation barrier already resolved the COS head.
                    # Expired plans cannot perform new effects on later data;
                    # retain their independent manual-recovery identity.
                    def advance_attention():
                        cursor=store.load_document('ingress_cursor','system',session_id)
                        store.save_document('ingress_cursor','system',session_id,{
                            'sequence':max(cursor.get('sequence',0),receipt['sequence']),
                            'position':max(cursor.get('position',-1),receipt.get('session_position',-1))})
                        return {'status':'manual_recovery_retained'}
                    store.commit_action('attention-cursor:'+rid,advance_attention)
                    continue
                raise RuntimeError('earlier receipt requires recovery: '+rid)
            automatic_attempts = state.get("delivery", {}).get("recovery_attempts", state.get("attempts", 0))
            if automatic_attempts >= 3 and not event.get("manual_recovery"):
                raise RuntimeError("receipt retry budget exhausted: " + rid)
            state = {**state, "attempts": state.get("attempts", 0) + 1, "status": "processing",
                     "receipt_id": rid, "sequence": receipt["sequence"], "started_at": time.time(),
                     "worker_started_at": worker_started_at}
            if "recovery_attempts" in state.get("delivery", {}):
                state["delivery"]["recovery_attempts"] = automatic_attempts + 1
            if receipt["kind"] == "message":
                message = self.budget_message(receipt, state)
                state.setdefault("queue_wait_ms", round(max(0, time.time() - state["budget_started_at"]) * 1000, 3) if not state["legacy_budget"] else None)
            def save_processing():
                store.save_document("receipt_state", "system", rid, state)
            deferred_processing = receipt["kind"] == "message" and execute is None and not state.get("result")
            if not deferred_processing:
                save_processing()
            deadline = PROCESSING_DEADLINE.set(min(PROCESSING_DEADLINE.get() or float("inf"), attempt_started + load_config().deadline_seconds))
            message = self.budget_message(receipt, state) if receipt["kind"] == "message" else receipt["message"]
            try:
                if receipt["kind"] == "presented":
                    state = self.present(store, receipt)
                elif receipt["kind"] == "reminder":
                    stamp = datetime.fromisoformat(receipt["reference"]["scheduled_time"].replace("Z", "+00:00"))
                    state.update(status="completed", result=check_reminders(store, stamp, send))
                else:
                    if not state.get("result"):
                        if receipt["source"] == "feishu":
                            settings = store.load_document("user_settings", message["user_id"])
                            if settings.get("feishu_chat_id") != message["chat_id"]:
                                store.save_document("user_settings", message["user_id"], "", {**settings, "feishu_chat_id": message["chat_id"]})
                        callback = (lambda text, **context: self.action_note(store, receipt, state, text, note_send, **context)) if receipt["source"] == "feishu" else None
                        options = {"on_execution_start": save_processing} if deferred_processing else {}
                        staged = None
                        def stage_ledger_reply(result, execution_id):
                            nonlocal staged
                            staged = self.stage_reply(message, state, result, execution_id)
                            store.save_document("receipt_state", "system", rid, staged)
                        if execute is None and receipt["source"] == "feishu":
                            options["stage_ledger_reply"] = stage_ledger_reply
                        result = (execute or run)(message, store=store, config=load_config(), intake_date=receipt["intake_date"],
                                                  action_note_callback=callback, **options)
                        errors = ";".join(result.get("errors", []))
                        deterministic = errors.startswith(("KeyError:", "ValueError:", "PermissionError:", "stale_execution_requires_new_message"))
                        if result.get("status") == "error" and state["attempts"] < 3 and not deterministic and not result.get("terminal"):
                            raise RuntimeError(errors)
                        if result.get("terminal"):
                            state.update(work_status="terminated", unfinished_error=errors)
                        elif result.get("status") == "error":
                            state.update(work_status="needs_attention", unfinished_error=errors)
                        # Commit final text and reference before any delivery API.
                        state = staged if staged is not None else self.stage_reply(message, state, result)
                        if receipt["source"] == "feishu" and staged is None:
                            store.save_document("receipt_state", "system", rid, state)
                    self.deliver(store, receipt, state, send, update_send)
                    if state.get("work_status") == "needs_attention":
                        store.save_document("receipt_state", "system", rid, state)
                        return state
                state["completed_at"] = time.time()
                def finish_receipt():
                    if receipt["kind"] == "message":
                        self.confirm_delivery(store, receipt, state)
                    store.save_document("receipt_state", "system", rid, state)
                    store.save_document("ingress_cursor", "system", session_id, {"sequence": max(receipt["sequence"], store.load_document("ingress_cursor", "system", session_id).get("sequence", 0)), "position":receipt.get("session_position", -1)})
                    if receipt["kind"] == "message":
                        from .bitable_adapter import usage_event_token
                        store.append_usage_event({"event_id": usage_event_token(message_key(message)),
                            "event_type": "message_processed", "message_id": message_key(message),
                            "user_id": message["user_id"], "source": receipt["source"], "preset": "live",
                            "status": state["result"].get("status"), "receipt_id": rid,
                            "reply_sent": state["delivery"]["presented"],
                            "latency_ms": round((time.time()-receipt["received_at"])*1000) if receipt.get("received_at") is not None else None})
                    return {"status": state["status"]}
                store.commit_action("receipt-complete:" + rid, finish_receipt)
                if receipt["kind"] == "message" and receipt["source"] == "feishu":
                    from .telemetry import log_receipt_latency
                    log_receipt_latency(receipt, state, store.journal.revision)
                requested_state = state
                attempt_started = time.monotonic()
            except Exception as exc:
                state.update(error=f"{type(exc).__name__}:{str(exc)[:300]}",
                             status="needs_attention" if automatic_attempts + 1 >= 3 or isinstance(exc, (ValueError, KeyError)) else "retry")
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
        from .task_runtime import dispatch_pending
        dispatch_pending(store,user_id=requested['message']['user_id'])
        return requested_state or {"status": "completed", "duplicate": True}

    def recover(self, *, wake=None):
        """Maintenance only wakes; it never writes business/session state."""
        inbox = self.inbox(readonly=True)
        generation = inbox.prefix.removesuffix('ingress/')
        pending = []
        blocked = []
        store = self.readonly_store()
        if inbox.format == 2:
            progress = self.sync_progress(generation=generation)
            water = progress.get('recovery_scan', {'revision':-1})['revision']
            changes = {}
            for revision in range(water + 1, inbox.revision + 1):
                for key in inbox._read(revision)['changes']:
                    if key.startswith('receipt:'):
                        rid = key.removeprefix('receipt:')
                        changes['recovery_active:' + rid] = {'receipt_id':rid}
            changes['recovery_scan'] = {'revision':inbox.revision}
            progress.commit(changes)
            receipts = [inbox.get('receipt:' + progress.get(key)['receipt_id']) for key in progress.keys('recovery_active:')]
        else:
            progress = None
            receipts = [inbox.get(key) for key in inbox.keys('receipt:')]
        finished = {}
        for receipt in receipts:
            rid = receipt['receipt_id']
            state = store.load_document('receipt_state', 'system', rid)
            if state.get('status') == 'needs_attention' or state.get('work_status') == 'needs_attention' or state.get('error') and state.get('status') not in {'completed','awaiting_presentation'}:
                blocked.append(rid)
            if state.get('status') in {'completed', 'awaiting_presentation'} and state.get('work_status') != 'needs_attention':
                finished['recovery_active:' + rid] = None
            elif state.get('status') != 'needs_attention' and state.get('delivery', {}).get('recovery_attempts', state.get('attempts', 0)) < 3:
                pending.append(receipt)
        if progress and finished:
            progress.commit(finished)
        for receipt in sorted(pending, key=lambda r: r["sequence"]):
            (wake or self.wake)({**receipt, "ingress_revision": inbox.revision, "ingress_generation": generation})
        return {"woken": len(pending), "blocked": blocked}


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
        from .telemetry import span
        reason = ('first_use' if _PIPELINE is None else 'confirmed_view' if _PIPELINE_KEY == key else
                  'storage_scope_changed' if _PIPELINE_KEY[1:] != key[1:] else 'transport_credentials_changed')
        previous = getattr(_PIPELINE, '_writer', None)
        with span('cos_pipeline_view', reason=reason, rebuilt=_PIPELINE_KEY != key,
                  function=os.getenv('SCF_FUNCTIONNAME', ''), prefix=key[1], region=key[3], bucket=key[2],
                  previous_view_id=previous.journal.view_id if previous else None,
                  previous_revision=previous.journal.revision if previous else None,
                  authorization_scope='unknown' if reason == 'transport_credentials_changed' else 'unchanged' if reason == 'confirmed_view' else 'new'):
            if _PIPELINE_KEY != key:
                pipeline = Pipeline(CosObjects(credentials=credentials), key[1])
                _PIPELINE, _PIPELINE_KEY = pipeline, key
        return _PIPELINE
