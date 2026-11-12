"""Optional control metadata carried in the assistant's normal response."""
from dataclasses import replace
import json
import re


_REVIEW = re.compile(r"(?m)^\s*<review_sources>")
_MAX_MARKS = 4


def extract_review_sources(response):
    """Return the public/native response plus bounded review source marks."""
    message = response.result[0]
    content = message.content
    if not isinstance(content, str) or not (marker := _REVIEW.search(content)):
        return response, []
    public = message.model_copy(update={"content": content[:marker.start()].rstrip()})
    clean = replace(response, result=[public, *response.result[1:]])
    raw = content[marker.end():].strip()
    end = "</review_sources>"
    if not raw.endswith(end) or len(raw) > 4096:
        return clean, []
    try:
        marks = json.loads(raw[:-len(end)])
    except ValueError:
        return clean, []
    if not isinstance(marks, list):
        return clean, []
    result = []
    for item in marks[:_MAX_MARKS]:
        if not isinstance(item, dict) or set(item) != {"ref", "reason"}:
            continue
        ref, reason = item["ref"], item["reason"]
        if isinstance(ref, str) and 0 < len(ref) <= 96 and isinstance(reason, str) and 0 < len(reason.strip()) <= 240:
            result.append({"ref": ref, "reason": reason.strip()})
    return clean, result
