"""Time and number helpers shared by the storage layer (ported from V2 legacy_app)."""

from __future__ import annotations

import math
import re
import time
import unicodedata
from contextvars import ContextVar
from typing import Any

DEFAULT_TIMEZONE_OFFSET_HOURS = 8.0
PROCESSING_DEADLINE = ContextVar("processing_deadline", default=None)

def remaining_timeout(default):
    deadline = PROCESSING_DEADLINE.get()
    if deadline is None:
        return default
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("message processing deadline reached")
    return min(default, remaining)


def timezone_offset_seconds(offset_hours: float | None = None) -> int:
    if offset_hours is None:
        offset_hours = DEFAULT_TIMEZONE_OFFSET_HOURS
    try:
        return int(float(offset_hours) * 3600)
    except (TypeError, ValueError):
        return int(DEFAULT_TIMEZONE_OFFSET_HOURS) * 3600


def today_string(offset_hours: float | None = None) -> str:
    return time.strftime("%Y-%m-%d", time.gmtime(time.time() + timezone_offset_seconds(offset_hours)))


def today_start_millis(offset_hours: float | None = None) -> int:
    offset = timezone_offset_seconds(offset_hours)
    shifted = time.time() + offset
    start_shifted = math.floor(shifted / 86400) * 86400
    return int((start_shifted - offset) * 1000)


def now_millis() -> int:
    return int(time.time() * 1000)


def round_number(value: Any) -> float:
    """Round to 0.1 with an epsilon guard against float drift (V2 behavior)."""
    return round(float(value) + 1e-9, 1)


def number_or_none(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def extract_text(value: Any) -> str:
    """Flatten a Bitable cell value into plain text (ported from V2)."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, (int, float, bool)):
        return str(value)
    if isinstance(value, list):
        parts = [extract_text(item) for item in value]
        return " ".join(part for part in parts if part).strip()
    if isinstance(value, dict):
        for key in ("text", "name", "value", "link"):
            if key in value:
                text = extract_text(value[key])
                if text:
                    return text
    return str(value).strip()


def extract_number(value: Any) -> float | None:
    """Flatten a Bitable cell value into a number (ported from V2)."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return round_number(float(value))
    if isinstance(value, str):
        try:
            return round_number(float(value.strip()))
        except ValueError:
            return None
    if isinstance(value, list) and value:
        return extract_number(value[0])
    if isinstance(value, dict):
        for key in ("value", "text", "number"):
            if key in value:
                number = extract_number(value[key])
                if number is not None:
                    return number
    return None


def parse_grams_from_text(text: str) -> float | None:
    """Parse a grams answer like "200g" / "0.5kg" / "一斤" (ported from V2)."""
    normalized = unicodedata.normalize("NFKC", str(text or "")).strip().lower()
    unit_patterns = [
        (r"(\d+(?:\.\d+)?)\s*(?:kg|公斤|千克)", 1000),
        (r"(\d+(?:\.\d+)?)\s*(?:斤)", 500),
        (r"(\d+(?:\.\d+)?)\s*(?:g|克|公克)", 1),
    ]
    for pattern, multiplier in unit_patterns:
        match = re.search(pattern, normalized)
        if match:
            return round_number(float(match.group(1)) * multiplier)
    if re.fullmatch(r"\d+(?:\.\d+)?", normalized):
        return round_number(float(normalized))
    return None
