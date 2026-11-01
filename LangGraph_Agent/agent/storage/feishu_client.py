"""Feishu HTTP client: tenant token cache, raw JSON helpers, message send.

Ported from V2 legacy_app.py (754-800). V4 adds one retry with backoff for
429/5xx/network errors, which V2 lacked.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.parse
from typing import Any
from .timeutil import remaining_timeout
import httpx
_CLIENT = httpx.Client()

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


def _http_request(url: str, payload: dict[str, Any] | None, headers: dict[str, str], method: str) -> dict[str, Any]:
    last_error = None
    from .telemetry import span
    resource = "send" if "/im/v1/messages" in url else "other"
    if "/tables/" in url:
        table_id = url.split("/tables/",1)[1].split("/",1)[0].split("?",1)[0]
        resource = next((name.lower() for name in ("PENDING","INTAKE","FOOD","STANDARD","USAGE","KNOWLEDGE")
                         if os.getenv("BITABLE_"+name+"_TABLE_ID")==table_id),"bitable_other")
    for attempt in (1, 2):
        try:
            with span("feishu_http", method=method, attempt=attempt,
                      resource=resource,
                      request_bytes=len(_json_bytes(payload)) if payload is not None else 0) as timing:
                response = _CLIENT.request(method, url, content=_json_bytes(payload) if payload is not None else None,
                    headers={"Content-Type":"application/json; charset=utf-8", **headers},
                    timeout=remaining_timeout(_HTTP_TIMEOUT_SECONDS))
                response.raise_for_status()
                timing["response_bytes"] = len(response.content)
                parsed = response.json() if response.content else {"code":0,"data":{}}
                return parsed if isinstance(parsed,dict) else {}
        except (httpx.HTTPStatusError, httpx.RequestError) as exc:
            last_error = exc
            retry = isinstance(exc,httpx.RequestError) or exc.response.status_code == 429 or exc.response.status_code >= 500
            if attempt == 1 and retry:
                time.sleep(min(_RETRY_SLEEP_SECONDS, remaining_timeout(_RETRY_SLEEP_SECONDS)))
                continue
            status = exc.response.status_code if isinstance(exc,httpx.HTTPStatusError) else type(exc).__name__
            raise RuntimeError(f"Feishu HTTP {method} {resource} failed: {status}") from exc
    raise RuntimeError(f"Feishu HTTP failed: {last_error}")


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
