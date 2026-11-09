from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any


AUTO_MATCH_THRESHOLD = 0.84
CONFIRM_MATCH_THRESHOLD = 0.62
AMBIGUOUS_DELTA = 0.05


@dataclass(frozen=True)
class FoodCandidate:
    food: dict[str, Any]
    score: float
    reason: str
    matched_key: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "food": self.food,
            "score": round(self.score, 3),
            "reason": self.reason,
            "matched_key": self.matched_key,
        }


def normalize_food_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    return re.sub(r"[\s,，、。；;:：|/\\()（）\[\]{}]+", "", text)


def match_food_candidates(query: str, foods: list[dict[str, Any]], limit: int = 5) -> list[FoodCandidate]:
    normalized_query = normalize_food_name(query)
    if not normalized_query:
        return []
    candidates = [_score_food(normalized_query, food) for food in foods]
    candidates = [candidate for candidate in candidates if candidate.score >= CONFIRM_MATCH_THRESHOLD]
    return sorted(candidates, key=lambda item: (-item.score, str(item.food.get("name") or "")))[:limit]


def choose_food_match(query: str, foods: list[dict[str, Any]]) -> dict[str, Any]:
    candidates = match_food_candidates(query, foods)
    if not candidates:
        return {"status": "not_found", "query": query, "candidates": []}

    top = candidates[0]
    second_score = candidates[1].score if len(candidates) > 1 else 0.0
    if top.score >= AUTO_MATCH_THRESHOLD and (
        top.reason in {"exact", "alias_exact"} or top.score - second_score >= AMBIGUOUS_DELTA
    ):
        return {"status": "auto_matched", "query": query, "candidate": top.to_dict(), "candidates": [top.to_dict()]}

    return {
        "status": "needs_confirmation",
        "query": query,
        "candidate": top.to_dict(),
        "candidates": [candidate.to_dict() for candidate in candidates],
    }


def _score_food(query: str, food: dict[str, Any]) -> FoodCandidate:
    best = FoodCandidate(food=food, score=0.0, reason="none", matched_key="")
    for key, is_alias in _food_keys(food):
        score, reason = _score_key(query, key, is_alias)
        if score > best.score:
            best = FoodCandidate(food=food, score=score, reason=reason, matched_key=key)
    return best


def _food_keys(food: dict[str, Any]) -> list[tuple[str, bool]]:
    keys = [(normalize_food_name(food.get("name")), False)]
    keys.extend((normalize_food_name(alias), True) for alias in food.get("aliases", []) if alias)
    return [(key, is_alias) for key, is_alias in keys if key]


def _score_key(query: str, key: str, is_alias: bool) -> tuple[float, str]:
    if query == key:
        return (0.99 if is_alias else 1.0, "alias_exact" if is_alias else "exact")
    if query in key:
        if key.startswith(query) and len(query) >= 2:
            return (0.9, "prefix")
        return (0.82 if len(query) >= 2 else 0.65, "contains")
    if key in query and len(key) >= 2:
        return (0.88, "query_contains")
    ratio = _edit_similarity(query, key)
    if ratio >= 0.7:
        return (ratio, "edit_distance")
    return (0.0, "none")


def _edit_similarity(left: str, right: str) -> float:
    longest = max(len(left), len(right))
    if not longest:
        return 0.0
    return 1.0 - (_levenshtein(left, right) / longest)


def _levenshtein(left: str, right: str) -> int:
    if left == right:
        return 0
    if len(left) < len(right):
        left, right = right, left
    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, 1):
        current = [left_index]
        for right_index, right_char in enumerate(right, 1):
            current.append(
                min(
                    current[-1] + 1,
                    previous[right_index] + 1,
                    previous[right_index - 1] + (left_char != right_char),
                )
            )
        previous = current
    return previous[-1]
