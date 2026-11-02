"""Boohee food search: normalized per-100g facts, TTL cache and explicit failures."""

from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
import urllib.error
from collections.abc import Callable
from typing import Any
from .timeutil import remaining_timeout

BOOHEE_SEARCH_URL = "https://api.boohee.com/open-apis/v1/food/search"
BOOHEE_TIMEOUT_SECONDS = 10.0
_CACHE_TTL_SECONDS = 600.0
_MAX_CANDIDATES = 3

_cache: dict[str, tuple[float, list[dict[str, Any]]]] = {}
_TRANSPORT: HttpGet | None = None

HttpGet = Callable[[str, dict[str, str]], dict[str, Any]]


class BooheeError(RuntimeError):
    pass


def set_transport(transport: HttpGet | None) -> None:
    """Test seam: override the HTTP transport (eval must not touch the network, D24)."""
    global _TRANSPORT
    _TRANSPORT = transport
    _cache.clear()


def search_food(keyword: str, *, per_page: int = 5, transport: HttpGet | None = None) -> list[dict[str, Any]]:
    """Search Boohee foods; returns normalized candidates:
    [{boohee_id, name, carbs_per_100g, protein_per_100g, fat_per_100g}]."""
    keyword = str(keyword or "").strip()
    if not keyword or len(keyword) > 30:
        return []
    api_key = os.getenv("BOOHEE_API_KEY", "")
    if not api_key and _TRANSPORT is None and transport is None:
        return []
    cache_key = f"{keyword.lower()}:{per_page}"
    now = time.monotonic()
    cached = _cache.get(cache_key)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return [dict(item) for item in cached[1]]

    getter = transport or _TRANSPORT or _default_http_get
    url = f"{BOOHEE_SEARCH_URL}?{urllib.parse.urlencode({'keyword': keyword, 'per_page': per_page})}"
    try:
        data = getter(url, {"X-Api-Key": api_key, "Accept": "*/*"})
    except Exception as exc:
        raise BooheeError(f"{type(exc).__name__}: {exc}") from exc
    if not isinstance(data, dict) or data.get("code") != 0:
        raise BooheeError(f"unexpected response: {str(data)[:200]}")
    foods = (data.get("data") or {}).get("foods") if isinstance(data.get("data"), dict) else None
    candidates = [item for item in (_normalize_food(f) for f in (foods or []) if isinstance(f, dict)) if item]
    candidates = candidates[:_MAX_CANDIDATES]
    _cache[cache_key] = (now, [dict(item) for item in candidates])
    return [dict(item) for item in candidates]


def _normalize_food(food: dict[str, Any]) -> dict[str, Any] | None:
    name = str(food.get("name") or "").strip()
    carbs = _number(food.get("carbohydrate"))
    protein = _number(food.get("protein"))
    fat = _number(food.get("fat"))
    boohee_id = str(food.get("code") or "").strip()
    if not name or carbs is None or protein is None or fat is None or not boohee_id:
        return None
    return {
        "boohee_id": boohee_id,
        "name": name,
        "carbs_per_100g": carbs,
        "protein_per_100g": protein,
        "fat_per_100g": fat,
        "kcal_per_100g": _nonnegative(food.get("calories")),
        "source": "boohee",
    }


def _number(value: Any) -> float | None:
    if isinstance(value, dict):
        value = value.get("value")
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if 0 <= result <= 100 else None


def _nonnegative(value):
    import math
    if isinstance(value, dict):
        value = value.get("value")
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and result >= 0 else None


def food_detail(code: str) -> dict[str, Any]:
    """Detail and unit requests use the food code, never a search result index."""
    getter = _TRANSPORT or _default_http_get
    headers = {"X-Api-Key": os.getenv("BOOHEE_API_KEY", ""), "Accept": "application/json"}
    base = BOOHEE_SEARCH_URL.rsplit("/", 1)[0]
    query = urllib.parse.urlencode({"code": code, "with_units": "true"})
    data = getter(f"{base}/detail?{query}", headers)
    if data.get("code") != 0:
        raise BooheeError(f"detail unavailable: code={data.get('code')} message={data.get('message', '')}")
    body = data.get("data") or {}
    food = body.get("food", body)
    normalized = _normalize_food(food)
    if normalized is None:
        raise BooheeError("detail missing nutrition facts")
    units = food.get("units", body.get("units"))
    if units is None:
        response = getter(f"{base}/units?{urllib.parse.urlencode({'code': code})}", headers)
        if response.get("code") != 0:
            raise BooheeError(f"units unavailable: code={response.get('code')}")
        unit_data = response.get("data") or {}
        units = unit_data.get("units", []) if isinstance(unit_data, dict) else unit_data
    normalized.update(units=units, detail_loaded=True)
    return normalized


def _default_http_get(url: str, headers: dict[str, str]) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers, method="GET")
    for attempt in range(2):  # one retry on 429/5xx, mirroring feishu_client discipline
        try:
            with urllib.request.urlopen(request, timeout=remaining_timeout(BOOHEE_TIMEOUT_SECONDS)) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as exc:
            retryable = not isinstance(exc, urllib.error.HTTPError) or exc.code == 429 or 500 <= exc.code < 600
            if attempt == 0 and retryable:
                time.sleep(0.5)
                continue
            raise


__all__ = ["BooheeError", "food_detail", "search_food", "set_transport"]
