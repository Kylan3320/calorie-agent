from __future__ import annotations

from types import ModuleType
from typing import Any

from .. import legacy_app
from . import memory
from .food_match import choose_food_match, normalize_food_name


def apply_user_aliases(
    foods: list[dict[str, Any]],
    message: dict[str, str],
    legacy: ModuleType = legacy_app,
) -> list[dict[str, Any]]:
    settings = legacy._settings()
    rows = memory.list_user_memories(message, settings=settings, source="alias", legacy=legacy)
    if not rows:
        return foods

    copied = [{**food, "aliases": list(food.get("aliases", []))} for food in foods]
    for row in rows:
        alias = str(row.get("alias") or "").strip()
        if not alias:
            continue
        target = _find_food(copied, row)
        if target is None:
            continue
        aliases = target.setdefault("aliases", [])
        if alias not in aliases:
            aliases.append(alias)
    return copied


def learn_alias_candidate(
    message: dict[str, str],
    food_match: dict[str, Any],
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    settings = legacy._settings()
    if not memory.memory_table_enabled(settings):
        return {"status": "disabled", "reason": "memory_table_not_configured"}
    if not _truthy(settings.get("auto_alias_learning_enabled", "true")):
        return {"status": "disabled", "reason": "auto_alias_learning_disabled"}
    if not _auto_learning_allowed(message, legacy):
        return {"status": "disabled", "reason": "user_auto_learning_disabled"}

    candidate = food_match.get("candidate") if isinstance(food_match.get("candidate"), dict) else {}
    food = candidate.get("food") if isinstance(candidate.get("food"), dict) else {}
    reason = str(candidate.get("reason") or "")
    score = _float(candidate.get("score"), 0.0)
    alias = str(food_match.get("query") or "").strip()
    if reason in {"exact", "alias_exact"}:
        return {"status": "skipped", "reason": "exact_match"}
    if score < memory.confidence_threshold(settings):
        return {"status": "skipped", "reason": "low_confidence", "score": score}
    if not alias or normalize_food_name(alias) == normalize_food_name(food.get("name")):
        return {"status": "skipped", "reason": "empty_or_same_alias"}

    result = memory.upsert_memory(
        message,
        {
            "source": "alias_candidate",
            "food_id": food.get("food_id", ""),
            "food_name": food.get("name", ""),
            "alias": alias,
            "confidence": score,
            "sample_count": 1,
            "status": "candidate",
        },
        settings=settings,
        legacy=legacy,
    )
    return {**result, "alias": alias, "food": food}


def confirm_alias(
    message: dict[str, str],
    alias: str,
    food_query: str,
    confidence: float = 1.0,
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    settings = legacy._settings()
    if not memory.memory_table_enabled(settings):
        return {"status": "disabled", "reason": "memory_table_not_configured"}
    alias = str(alias or "").strip()
    if not alias:
        return {"status": "invalid", "reason": "alias_required"}

    food_match = choose_food_match(food_query, legacy._load_food_database())
    if food_match.get("status") not in {"auto_matched", "needs_confirmation"}:
        return {"status": "food_not_found", "food_query": food_query}
    candidate = food_match.get("candidate") if isinstance(food_match.get("candidate"), dict) else {}
    food = candidate.get("food") if isinstance(candidate.get("food"), dict) else {}
    if not food:
        return {"status": "food_not_found", "food_query": food_query}

    result = memory.upsert_memory(
        message,
        {
            "source": "alias",
            "food_id": food.get("food_id", ""),
            "food_name": food.get("name", ""),
            "alias": alias,
            "confidence": confidence,
            "sample_count": 1,
            "status": "active",
        },
        settings=settings,
        legacy=legacy,
    )
    return {**result, "alias": alias, "food": food}


def list_aliases(message: dict[str, str], legacy: ModuleType = legacy_app) -> dict[str, Any]:
    settings = legacy._settings()
    rows = memory.list_user_memories(message, settings=settings, source="alias", legacy=legacy)
    return {"status": "ok", "items": rows, "count": len(rows)}


def delete_alias(message: dict[str, str], target: dict[str, Any], legacy: ModuleType = legacy_app) -> dict[str, Any]:
    return memory.soft_delete_memories(message, {**target, "source": "alias"}, legacy=legacy)


def _find_food(foods: list[dict[str, Any]], row: dict[str, Any]) -> dict[str, Any] | None:
    food_id = str(row.get("food_id") or "")
    food_name = normalize_food_name(row.get("food_name"))
    for food in foods:
        if food_id and str(food.get("food_id") or "") == food_id:
            return food
        if food_name and normalize_food_name(food.get("name")) == food_name:
            return food
    return None


def _auto_learning_allowed(message: dict[str, str], legacy: ModuleType) -> bool:
    try:
        from .preferences import get_preference_map

        prefs = get_preference_map(message, legacy=legacy)
    except Exception:
        return True
    return str(prefs.get("auto_learning", "true")).strip().lower() not in {"false", "0", "off", "no"}


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
