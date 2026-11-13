"""SCF HTTP ingress and separate serial message/maintenance entry points."""
from __future__ import annotations
import hmac
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit, parse_qs

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
if (ROOT / "deps").exists():
    sys.path.insert(0, str(ROOT / "deps"))

from agent import load_config
from agent.storage.execution_pipeline import configured_pipeline
from agent.storage.telemetry import EVENTS, MESSAGE_ID

SERVICE_NAME = "calorie-agent-v4"

def _verify_rollover_style_token(payload_token: Any) -> bool:
    expected = os.getenv("FEISHU_VERIFICATION_TOKEN", "")
    if not expected:
        return True
    return str(payload_token or "") == expected


def _extract_challenge(payload: dict[str, Any]) -> str:
    if isinstance(payload.get("challenge"), str):
        return payload["challenge"]
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    challenge = event.get("challenge") if isinstance(event.get("challenge"), str) else ""
    return challenge


def _parse_text_message(payload: dict[str, Any]) -> dict[str, str] | None:
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    _ = header
    message = event.get("message") if isinstance(event.get("message"), dict) else {}
    if str(message.get("message_type") or "") != "text":
        return None
    content = message.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except json.JSONDecodeError:
            content = {}
    if not isinstance(content, dict) or not str(content.get("text") or "").strip():
        return None
    sender = event.get("sender") if isinstance(event.get("sender"), dict) else {}
    sender_id = sender.get("sender_id") if isinstance(sender.get("sender_id"), dict) else {}
    return {
        "message_id": str(message.get("message_id") or ""),
        "chat_id": str(message.get("chat_id") or ""),
        "user_id": str(sender_id.get("open_id") or sender_id.get("user_id") or ""),
        "text": str(content.get("text") or "").strip(),
        "create_time": str(message.get("create_time") or ""),
    }



class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload, status=200):
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _service_auth(self):
        expected = os.getenv("CALORIE_SERVICE_TOKEN", "")
        supplied = self.headers.get("X-Calorie-Service-Token") or self.headers.get("Authorization", "").removeprefix("Bearer ")
        return bool(expected) and hmac.compare_digest(supplied, expected)

    def do_GET(self):
        parsed = urlsplit(self.path)
        if parsed.path in ("/", "/health"):
            self._send_json({"status": "ok", "service": SERVICE_NAME,
                             "build_id": (ROOT / "BUILD_ID").read_text().strip() if (ROOT / "BUILD_ID").exists() else "development",
                             "model": load_config().model, "retriever": load_config().knowledge_retriever,
                             "version": "v4.0.1", "env": os.getenv("APP_ENV", "production"),
                             "env_check": {name: bool(os.getenv(name, "").strip()) for name in (
                                 "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_VERIFICATION_TOKEN",
                                 "DEEPSEEK_API_KEY", "BITABLE_APP_TOKEN", "BITABLE_FOOD_TABLE_ID",
                                 "BITABLE_INTAKE_TABLE_ID", "BITABLE_STANDARD_TABLE_ID",
                                 "BITABLE_PENDING_TABLE_ID", "BITABLE_USAGE_TABLE_ID", "COS_BUCKET", "COS_PREFIX")}})
        elif parsed.path == '/internal/worker-control':
            if not self._service_auth():
                self._send_json({'allowed':False},403)
                return
            try:
                from agent.storage.tasks import control
                params = {k:v[0] for k,v in parse_qs(parsed.query).items()}
                required = {'user_id','task_id','worker_id','instance_id'}
                if set(params) != required:
                    self._send_json({'allowed':False},400)
                    return
                params['control_token']=self.headers.get('X-Calorie-Worker-Token','')
                self._send_json(control(configured_pipeline(self.headers).readonly_store(),params))
            except Exception:
                self._send_json({'allowed':False,'reason':'control_unavailable'},503)
        elif parsed.path == "/internal/result":
            if not self._service_auth():
                self._send_json({"status": "error", "reason": "invalid_service_token"}, 403)
                return
            try:
                rid = parse_qs(parsed.query).get("receipt_id", [""])[0]
                state = configured_pipeline(self.headers).result(rid)
                self._send_json(state or {"status": "pending"})
            except Exception as exc:
                self._send_json({"status": "error", "reason": str(exc)[:200]}, 503)
        else:
            self._send_json({"status": "not_found"}, 404)

    def do_POST(self):
        received_at = time.time()
        events_token = EVENTS.set([])
        message_token = MESSAGE_ID.set(None)
        try:
            self._post(received_at)
        finally:
            print(json.dumps({"event": "latency_webhook", "events": EVENTS.get(), "duration_ms": round((time.time()-received_at)*1000, 3)}, ensure_ascii=False))
            EVENTS.reset(events_token)
            MESSAGE_ID.reset(message_token)

    def _post(self, received_at):
        if self.path not in {"/webhook/feishu/events", "/internal/intake", "/internal/presented", "/internal/reminder"}:
            self._send_json({"status": "not_found"}, 404)
            return
        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except (ValueError, UnicodeDecodeError):
            self._send_json({"status": "error", "reason": "invalid_json"}, 400)
            return
        if not isinstance(payload, dict):
            self._send_json({"status": "error", "reason": "invalid_payload"}, 400)
            return
        if self.path == "/webhook/feishu/events":
            if "encrypt" in payload:
                self._send_json({"status": "error", "reason": "encrypt_mode_not_supported"}, 400)
                return
            header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
            if not _verify_rollover_style_token(payload.get("token") or header.get("token")):
                self._send_json({"status": "error", "reason": "invalid_token"}, 403)
                return
            challenge = _extract_challenge(payload)
            if challenge:
                self._send_json({"challenge": challenge})
                return
            message = _parse_text_message(payload)
            if message is None:
                self._send_json({"status": "ignored", "reason": "not_text_event"})
                return
            source, kind, reference = "feishu", "message", None
        else:
            if not self._service_auth():
                self._send_json({"status": "error", "reason": "invalid_service_token"}, 403)
                return
            if self.path == "/internal/reminder":
                from agent.storage.reminders import reminder_intake
                try:
                    message, reference = reminder_intake(payload.get("scheduled_time"))
                except ValueError as exc:
                    self._send_json({"status": "error", "reason": str(exc)}, 400)
                    return
                source, kind = "timer", "reminder"
            else:
                source = payload.get("source")
                if source not in {"cli", "dsh", "console"}:
                    self._send_json({"status": "error", "reason": "invalid_source"}, 400)
                    return
                message = payload.get("message", {})
                kind = "presented" if self.path == "/internal/presented" else "message"
                reference = payload.get("reference")
        MESSAGE_ID.set(message.get("message_id"))
        config = load_config()
        if config.preset != "live":
            if self.path != "/webhook/feishu/events":
                self._send_json({"status": "error", "reason": "live_preset_required"}, 503)
                return
            from agent import run
            from agent.storage import create_store
            result = run(message, store=create_store(config), config=config)
            self._send_json({"status": "shadow_completed", "result_status": result["status"]})
            return
        try:
            pipeline = configured_pipeline(self.headers)
            if kind == 'reminder':
                from agent.storage.scf_dispatch import dispatch_event
                request_id = dispatch_event({'type':'calorie_reminder_check', **reference}, self.headers)
                self._send_json({'status':'accepted', 'request_id':request_id})
                return
            receipt = pipeline.accept(message, source=source, received_at=received_at, kind=kind, reference=reference)
            # Durable inbox followed by accepted asynchronous Invoke, then ACK.
            pipeline.wake(receipt, self.headers)
        except Exception as exc:
            print(json.dumps({"event": "message_not_accepted", "message_id": message.get("message_id"), "error": str(exc)[:300]}, ensure_ascii=False))
            self._send_json({"status": "error", "reason": "accept_or_handoff_failed"}, 503)
            return
        self._send_json({"status": "accepted", "receipt_id": receipt["receipt_id"],
                         "sequence": receipt["sequence"], "message_id": receipt["message"]["message_id"],
                         "chat_id": receipt["message"]["chat_id"]})


def ingress_handler(event, context=None):
    if event.get('type') == 'calorie_maintenance_pause':
        if load_config().preset!='live':return {'status':'skipped_preset'}
        from agent.storage.generations import ingress_pause
        pipeline = configured_pipeline()
        return ingress_pause(pipeline.objects,pipeline.prefix,event)
    """Event-SCF function URL adapter sharing the HTTP handler's auth/ACK path."""
    import base64
    import io
    from email.message import Message
    from urllib.parse import urlencode
    body = event.get("body") or ""
    if isinstance(body, dict):
        body = json.dumps(body, ensure_ascii=False)
    raw = base64.b64decode(body) if event.get("isBase64Encoded") else str(body).encode()
    request = object.__new__(Handler)
    request.path = event.get("rawPath") or event.get("path") or "/"
    query = event.get("rawQueryString") or urlencode(event.get("queryStringParameters") or event.get("queryString") or {})
    if query:
        request.path += "?" + query
    request.headers = Message()
    for key, value in (event.get("headers") or {}).items():
        request.headers[key] = str(value)
    request.headers.replace_header("Content-Length", str(len(raw))) if request.headers.get("Content-Length") else request.headers.__setitem__("Content-Length", str(len(raw)))
    request.rfile = io.BytesIO(raw)
    response = {}
    def respond(payload, status=200):
        response.update(statusCode=status, headers={"Content-Type": "application/json; charset=utf-8"},
                        body=json.dumps(payload, ensure_ascii=False), isBase64Encoded=False)
    request._send_json = respond
    method = event.get("httpMethod") or event.get("requestContext", {}).get("http", {}).get("method") or "POST"
    if method == "GET":
        request.do_GET()
    elif method == "POST":
        request.do_POST()
    else:
        respond({"status": "method_not_allowed"}, 405)
    return response


def message_handler(event, context=None):
    if load_config().preset!='live':return {'status':'skipped_preset'}
    events_token = EVENTS.set(list(event.get("ingress_events", [])))
    message_token = MESSAGE_ID.set(event.get("message_id"))
    try:
        from agent.storage.timeutil import execution_deadline
        limit_ms = context["time_limit_in_ms"] if isinstance(context, dict) else context.time_limit_in_ms if context is not None else 120000
        with execution_deadline(limit_ms / 1000 - 1):
            if event.get("type") not in {"calorie_message", "calorie_summary", "calorie_experience", "calorie_memory_backfill", "calorie_diet_goal", "calorie_reminder_check", "calorie_reminder_recover", "calorie_task", "calorie_maintenance_pause", "calorie_task_tick"}:
                from agent.storage.reminders import reminder_intake
                _,reference=reminder_intake(event.get('Time') or event.get('time'))
                return configured_pipeline().worker({'type':'calorie_reminder_check',**reference})
            return configured_pipeline().worker(event)
    finally:
        print(json.dumps({"event": "latency_worker", "receipt_id": event.get("receipt_id"),
                          "message_id": event.get("message_id"), "events": EVENTS.get()}, ensure_ascii=False))
        MESSAGE_ID.reset(message_token)
        EVENTS.reset(events_token)


def maintenance_handler(event, context=None):
    from agent.config import load_config
    from agent.storage.timeutil import execution_deadline
    from agent.storage.cos_archive import archive_pending
    from agent.storage.memory_background import run_next
    from agent.storage.diet_goal_runtime import run_next as run_goal
    from agent.storage.scf_dispatch import dispatch_event
    limit_ms = context["time_limit_in_ms"] if isinstance(context, dict) else context.time_limit_in_ms if context is not None else 120000
    remaining = limit_ms / 1000
    if load_config().preset!='live':return {'status':'skipped_preset'}
    with execution_deadline(remaining - 1) as deadline:
        pipeline = configured_pipeline()
        from agent.storage.generations import maintenance_tick
        maintenance = maintenance_tick(pipeline,dispatch=dispatch_event)
        if maintenance['status'] in {'awaiting_message_watchdog','watchdog_owns_resume'}:
            return {'maintenance':maintenance}
        recovery = pipeline.recover()
        store = pipeline.readonly_store()
        if store.load_document('reminder_delivery_active','system'):
            dispatch_event({'type':'calorie_reminder_recover'})
        progress = pipeline.sync_progress(generation=store.prefix)
        archive = archive_pending(store=store, progress=progress)
        summary = {"status": "skipped_maintenance_error"}
        goal = {"status": "skipped_maintenance_error"}
        if not archive["failed"] and not recovery.get("blocked"):
            from agent.storage.tasks import load as load_task
            active_tasks=store.load_document('task_active','system')
            pending_workers=store.load_document('worker_active','system')
            if active_tasks or pending_workers:dispatch_event({'type':'calorie_task_tick'})
            for user,ident in active_tasks.items():
                task = load_task(store,user,ident)
                if task.get('cycle',{}).get('router_pending'):
                    dispatch_event({'type':'calorie_task','operation':'resume','user_id':user,'task_id':ident})
                for wid in task.get('cycle',{}).get('attempts',[]):
                    dispatch_event({'type':'calorie_task','operation':'report','user_id':user,'task_id':ident,'worker_id':wid})
            goal = run_goal(store, progress, load_config(), dispatch=dispatch_event, remaining_seconds=deadline - time.monotonic())
            summary = run_next(store, progress, dispatch=dispatch_event, remaining_seconds=deadline - time.monotonic())
        return {"maintenance":maintenance,"recovery": recovery, "archive": archive, "summary": summary, "diet_goal": goal}


def reminder_handler(event, context=None):
    # All message writes, timer checks and manual recovery run in this function,
    # configured with platform concurrency one. Maintenance only wakes it.
    if event.get("type") in {"calorie_message", "calorie_diet_goal", "calorie_reminder_check", "calorie_reminder_recover", "calorie_task", "calorie_maintenance_pause", "calorie_task_tick"}:
        return message_handler(event, context)
    return message_handler(event,context)


def main():
    port = int(os.getenv("PORT") or os.getenv("SCF_CUSTOM_CONTAINER_PORT") or 9000)
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()


def compute_handler(event,context=None):
    from agent.storage.task_runtime import worker_handler
    return worker_handler(event,context)
