"""Tencent SCF webhook entry for Calorie Agent V4 (ported from V2 app/legacy_app Handler).

Endpoints:
  GET  /health                  -> liveness
  POST /webhook/feishu/events   -> verify/challenge, quick ACK, process in request thread

P0 intentionally has no /tasks/* cron endpoints (see REQUIREMENTS.md OUT OF SCOPE).
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
_DEPS = ROOT / "deps"
if _DEPS.exists():
    sys.path.insert(0, str(_DEPS))  # vendored dependencies built by deploy/build_zip.sh

from agent import load_config, run  # noqa: E402
from agent.policy import resolve_preset  # noqa: E402
from agent.storage import (  # noqa: E402
    BitableStore,
    FakeStore,
    feishu_client,
    message_key,
    reply_uuid,
    usage_event_token,
)

SERVICE_NAME = "calorie-agent-v4"
_MESSAGE_STATE_LOCK = threading.Lock()
_MESSAGE_STATE: dict[str, tuple[str, float]] = {}
_MESSAGE_STATE_TTL_SECONDS = 3600
_STORE: Any = None
_STORE_LOCK = threading.Lock()


def _get_store() -> Any:
    global _STORE
    with _STORE_LOCK:
        if _STORE is None:
            config = load_config()
            preset = resolve_preset(config.preset, config.allowed_tools)
            if preset.store_kind == "fake":
                _STORE = FakeStore()
            else:
                _STORE = BitableStore(offset_hours=config.timezone_offset_hours)
        return _STORE


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
    }


def _message_state_key(message: dict[str, str]) -> str:
    return message_key(message)


def _prune_message_state(now: float) -> None:
    expired = [key for key, (_, timestamp) in _MESSAGE_STATE.items() if now - timestamp > _MESSAGE_STATE_TTL_SECONDS]
    for key in expired:
        _MESSAGE_STATE.pop(key, None)


def _try_mark_message_processing(message: dict[str, str]) -> bool:
    key = _message_state_key(message)
    if not key:
        return True
    now = time.monotonic()
    with _MESSAGE_STATE_LOCK:
        _prune_message_state(now)
        existing = _MESSAGE_STATE.get(key)
        if existing:
            status, timestamp = existing
            if status == "failed":
                _MESSAGE_STATE[key] = ("processing", now)
                return True
            return False
        _MESSAGE_STATE[key] = ("processing", now)
        return True


def _mark_message_done(message: dict[str, str], status: str) -> None:
    key = _message_state_key(message)
    if not key:
        return
    with _MESSAGE_STATE_LOCK:
        _MESSAGE_STATE[key] = (status, time.monotonic())


def _env_check() -> dict[str, bool]:
    names = [
        "FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_VERIFICATION_TOKEN",
        "DEEPSEEK_API_KEY", "BITABLE_APP_TOKEN", "BITABLE_FOOD_TABLE_ID",
        "BITABLE_INTAKE_TABLE_ID", "BITABLE_STANDARD_TABLE_ID",
        "BITABLE_PENDING_TABLE_ID", "BITABLE_USAGE_TABLE_ID",
    ]
    return {name: bool(os.getenv(name, "").strip()) for name in names}


class Handler(BaseHTTPRequestHandler):
    def _send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path in ("/", "/health"):
            self._send_json({
                "status": "ok",
                "service": SERVICE_NAME,
                "version": "v4.0.1",
                "env": os.getenv("APP_ENV", "production"),
                "env_check": _env_check(),
            })
        else:
            self._send_json({"status": "not_found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/webhook/feishu/events":
            self._send_json({"status": "not_found"}, status=404)
            return

        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError:
            self._send_json({"status": "ignored", "reason": "invalid_json"})
            return
        if not isinstance(payload, dict):
            self._send_json({"status": "ignored", "reason": "invalid_payload"})
            return
        if "encrypt" in payload:
            self._send_json({"status": "error", "reason": "encrypt_mode_not_supported"}, status=400)
            return

        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        token = payload.get("token") or header.get("token")
        if not _verify_rollover_style_token(token):
            self._send_json({"status": "error", "reason": "invalid_token"}, status=403)
            return

        challenge = _extract_challenge(payload)
        if challenge:
            self._send_json({"challenge": challenge})
            return

        message = _parse_text_message(payload)
        if message is None:
            self._send_json({"status": "ignored", "reason": "not_text_event"})
            return

        try:
            config = load_config()
            preset = resolve_preset(config.preset, config.allowed_tools)
            store = _get_store()
            message = {**message, "user_id": _resolve_user_alias(store, message.get("user_id", ""))}
            receipt = store.create_pending(message, "message_received", "", None, "unknown",
                                           payload={"message": message, "intake_date": store.today_string()}) if preset.allow_write else None
            if receipt and receipt["status"] == "not_configured":
                raise RuntimeError("pending_tasks is required before accepting messages")
        except Exception as exc:
            _log_event("message_not_accepted", message, {"error": str(exc)[:300]})
            self._send_json({"status": "error", "reason": "accept_failed"}, status=503)
            return
        self._send_json({"status": "accepted", "message_id": message["message_id"], "chat_id": message["chat_id"]})
        if not receipt or receipt["status"] != "completed":
            _process_message_synchronously(message, receipt)


def _resolve_user_alias(store: Any, user_id: str) -> str:
    return store.load_user_aliases().get(user_id, user_id) if user_id else user_id


def _process_message_synchronously(message: dict[str, str], receipt: dict | None = None) -> bool:
    if not _try_mark_message_processing(message):
        _log_event("duplicate_skipped", message, {})
        return False
    started = time.monotonic()
    result, store, config = {}, None, None
    reply_sent = False
    error = ""
    for attempt in range(2):
        try:
            config = load_config()
            preset = resolve_preset(config.preset, config.allowed_tools)
            store = _get_store()
            if receipt:
                message = receipt["payload"]["message"]
            else:
                message = {**message, "user_id": _resolve_user_alias(store, message.get("user_id", ""))}
            result = run(message, store=store, config=config,
                         intake_date=receipt["payload"]["intake_date"] if receipt else None)
            if result["status"] == "error":
                raise RuntimeError(";".join(result["errors"]))
            if preset.send_reply and result["reply"] and message.get("chat_id"):
                feishu_client.send_text(message["chat_id"], result["reply"], uuid_key=reply_uuid(message))
                reply_sent = True
            if receipt:
                store.mark_pending_status(receipt, "completed")
            _mark_message_done(message, "done")
            error = ""
            break
        except Exception as exc:
            error = f"{type(exc).__name__}:{exc}"
            _log_event("message_attempt_failed", message, {"attempt": attempt + 1, "error": error[:300]})
    if error:
        _mark_message_done(message, "failed")
    latency_ms = int((time.monotonic() - started) * 1000)
    if store and config:
        store.append_usage_event({
            "event_id": usage_event_token(message_key(message)), "event_type": "message_processed",
            "message_id": message_key(message), "user_id": message.get("user_id", ""),
            "preset": config.preset, "planner": config.planner, "status": "error" if error else "ok",
            "error_type": error[:200], "action_count": len(result.get("plan", {}).get("actions", [])),
            "reply_sent": reply_sent, "latency_ms": latency_ms})
    _log_event("message_failed" if error else "message_processed", message,
               {"status": "error" if error else "ok", "reply_sent": reply_sent,
                "error": error[:300], "latency_ms": latency_ms})
    return not error


def retry_pending_messages() -> int:
    store = _get_store()
    failed = 0
    for task in store.load_unfinished_executions():
        if task["intent"] == "message_received":
            failed += not _process_message_synchronously(task["payload"]["message"], task)
    return int(failed)


def _log_event(event_type: str, message: dict[str, str], fields: dict[str, Any]) -> None:
    payload = {
        "event": event_type,
        "service": SERVICE_NAME,
        "message_id": message.get("message_id", ""),
        "ts": int(time.time() * 1000),
        **fields,
    }
    print(json.dumps(payload, ensure_ascii=False))


def main() -> None:
    port = int(os.getenv("PORT") or os.getenv("SCF_CUSTOM_CONTAINER_PORT") or 9000)
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(json.dumps({"event": "server_started", "service": SERVICE_NAME, "port": port}))
    server.serve_forever()


if __name__ == "__main__":
    if "--retry-pending" in sys.argv:
        raise SystemExit(1 if retry_pending_messages() else 0)
    main()
