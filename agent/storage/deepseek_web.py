"""Portion lookup through DeepSeek's official Search and Fetch endpoints."""

from functools import lru_cache
import ipaddress
import os
import re
from urllib.parse import urlsplit, urlunsplit

import httpx

from .model_transport import create_client, deadline_scope


OFFICIAL_BASE = "https://api.deepseek.com"


class WebLookupError(Exception):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


@lru_cache(maxsize=1)
def _client():
    return create_client()


def public_url(url):
    """Canonicalize a public HTTP(S) address without doing local DNS or HTTP."""
    if not isinstance(url, str) or len(url) > 2048 or re.search(r"[\s\\\x00-\x1f\x7f]", url):
        return None
    try:
        parts = urlsplit(url)
        if (parts.scheme not in {"https", "http"} or not parts.hostname or parts.username is not None
                or parts.password is not None or parts.port not in {None, 80, 443}):
            return None
        host = parts.hostname.rstrip(".").encode("idna").decode("ascii").lower()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            labels = host.split(".")
            if (len(labels) < 2 or not re.fullmatch(r"(?:[a-z]{2,63}|xn--[a-z0-9-]+)", labels[-1])
                    or labels[-1] in {"local", "localhost", "internal", "home", "lan", "test", "invalid", "example", "onion"}
                    or any(not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels)):
                return None
        else:
            if not address.is_global or address.is_multicast:
                return None
            host = f"[{address}]" if address.version == 6 else str(address)
        if parts.port is not None and parts.port != (443 if parts.scheme == "https" else 80):
            host += f":{parts.port}"
        return urlunsplit((parts.scheme, host, parts.path or "/", parts.query, ""))
    except (ValueError, UnicodeError):
        return None


def _post(operation, body, deadline, reserve):
    key = os.getenv("DEEPSEEK_SEARCH_API_KEY")
    inference_base = os.getenv("DEEPSEEK_API_BASE", OFFICIAL_BASE).rstrip("/")
    if not key and inference_base in {OFFICIAL_BASE, f"{OFFICIAL_BASE}/v1"}:
        key = os.getenv("DEEPSEEK_API_KEY")
    if not key:
        raise WebLookupError("official_key_unconfigured")
    with deadline_scope(deadline):
        # The shared client disables transport retries and redirect following.
        # Reserve immediately before the one possible HTTP request.
        reserve()
        try:
            response = _client().post(f"{OFFICIAL_BASE}/v1/tools/{operation}",
                                      headers={"Authorization": f"Bearer {key}"}, json=body)
            if operation == "fetch" and response.status_code in (403, 404):
                return {}
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPStatusError as exc:
            raise WebLookupError(f"provider_http_{exc.response.status_code}") from exc
        except (httpx.RequestError, TimeoutError, OSError) as exc:
            raise WebLookupError("provider_unreachable") from exc
        except ValueError as exc:
            raise WebLookupError("provider_invalid_response") from exc
    if not isinstance(payload, dict):
        raise WebLookupError("provider_invalid_response")
    return payload


def search(query, *, deadline, reserve):
    rows = _post("search", {"query": query, "count": 6, "need_content": False}, deadline, reserve).get("results")
    if not isinstance(rows, list):
        raise WebLookupError("provider_invalid_response")
    results = {}
    for row in rows[:6]:
        if not isinstance(row, dict) or not (url := public_url(row.get("url"))):
            continue
        results.setdefault(url, {"url": url, "title": str(row.get("title") or "")[:160],
                                 "summary": str(row.get("summary") or row.get("snippet") or "")[:240]})
    return list(results.values())


def fetch(url, *, deadline, reserve):
    payload = _post("fetch", {"url": url}, deadline, reserve)
    text = payload.get("content")
    if not isinstance(text, str) or not text.strip():
        return None
    return {"title": str(payload.get("title") or "")[:160], "text": text[:5500],
            "truncated": len(text) > 5500}
