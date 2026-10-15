"""Feishu HTTP client: tenant token cache, raw JSON helpers, message send.

Ported from V2 legacy_app.py (754-800). V4 adds one retry with backoff for
429/5xx/network errors, which V2 lacked.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

DEFAULT_FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
_HTTP_TIMEOUT_SECONDS = 10
_RETRY_SLEEP_SECONDS = 0.6

_TOKEN_LOCK = threading.Lock()
_TENANT_ACCESS_TOKEN = ""
_TENANT_ACCESS_TOKEN_EXPIRES_AT = 0.0


def feishu_api_base() -> str:
    return os.getenv("FEISHU_API_BASE", DEFAULT_FEISHU_API_BASE).rstrip("/")


def _json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _should_retry(exc: BaseException) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code == 429 or 500 <= exc.code < 600
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, TimeoutError):
        return True
    return False


def _http_request(url: str, payload: dict[str, Any] | None, headers: dict[str, str], method: str) -> dict[str, Any]:
    data = _json_bytes(payload) if payload is not None else None
    last_error: BaseException | None = None
    for attempt in (1, 2):
        request = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json; charset=utf-8", **headers},
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS) as response:
                raw = response.read()
            if not raw:
                return {"code": 0, "data": {}}
            parsed = json.loads(raw.decode("utf-8"))
            return parsed if isinstance(parsed, dict) else {}
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            last_error = exc
            if attempt == 1 and _should_retry(exc):
                time.sleep(_RETRY_SLEEP_SECONDS)
                continue
            raise RuntimeError(f"HTTP {exc.code} {method} {url}: {body}") from exc
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt == 1 and _should_retry(exc):
                time.sleep(_RETRY_SLEEP_SECONDS)
                continue
            raise RuntimeError(f"HTTP {method} {url} failed: {exc}") from exc
    raise RuntimeError(f"HTTP {method} {url} failed after retry: {last_error}")


def http_json_post(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    return _http_request(url, payload, headers or {}, "POST")


def http_json_get(url: str, headers: dict[str, str] | None = None) -> dict[str, Any]:
    return _http_request(url, None, headers or {}, "GET")


def http_json_put(url: str, payload: dict[str, Any], headers: dict[str, str] | None = None) -> dict[str, Any]:
    return _http_request(url, payload, headers or {}, "PUT")


def get_tenant_access_token() -> str:
    global _TENANT_ACCESS_TOKEN, _TENANT_ACCESS_TOKEN_EXPIRES_AT

    with _TOKEN_LOCK:
        if _TENANT_ACCESS_TOKEN and time.monotonic() < _TENANT_ACCESS_TOKEN_EXPIRES_AT:
            return _TENANT_ACCESS_TOKEN

        app_id = os.getenv("FEISHU_APP_ID", "")
        app_secret = os.getenv("FEISHU_APP_SECRET", "")
        if not app_id or not app_secret:
            raise RuntimeError("FEISHU_APP_ID and FEISHU_APP_SECRET are not configured.")

        data = http_json_post(
            f"{feishu_api_base()}/auth/v3/tenant_access_token/internal",
            {"app_id": app_id, "app_secret": app_secret},
        )
        if data.get("code") != 0:
            raise RuntimeError(f"Failed to get tenant_access_token: {data}")

        token = str(data.get("tenant_access_token") or "")
        if not token:
            raise RuntimeError("Feishu response does not include tenant_access_token.")

        expires_in = int(data.get("expire") or 7200)
        _TENANT_ACCESS_TOKEN = token
        _TENANT_ACCESS_TOKEN_EXPIRES_AT = time.monotonic() + max(expires_in - 60, 60)
        return token


def send_text(chat_id: str, text: str, uuid_key: str = "") -> dict[str, Any]:
    """Send a text message to a chat. uuid_key makes the send idempotent."""
    token = get_tenant_access_token()
    query = urllib.parse.urlencode({"receive_id_type": "chat_id"})
    payload: dict[str, Any] = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    }
    if uuid_key:
        payload["uuid"] = uuid_key
    data = http_json_post(
        f"{feishu_api_base()}/im/v1/messages?{query}",
        payload,
        {"Authorization": f"Bearer {token}"},
    )
    if data.get("code") != 0:
        raise RuntimeError(f"Failed to send Feishu message: {data}")
    return data
