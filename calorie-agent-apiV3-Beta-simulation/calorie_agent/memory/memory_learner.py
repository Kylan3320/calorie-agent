from __future__ import annotations

from dataclasses import dataclass, field
import os
from types import ModuleType
from typing import Any

from .. import legacy_app
from ..domain import preferences
from . import memory_store
from .learning_rules import (
    LearningEvent,
    MemoryCandidate,
    RuleOptions,
    food_lookup_from_legacy,
    generate_memory_candidates,
)


@dataclass(frozen=True)
class LearningOptions:
    enabled: bool = field(default_factory=lambda: _bool_env("MEMORY_LEARNER_ENABLED", True))
    min_confidence: float = field(default_factory=lambda: _float_env("MEMORY_LEARNER_MIN_CONFIDENCE", 0.8))
    require_confirm_alias: bool = field(default_factory=lambda: _bool_env("MEMORY_LEARNER_REQUIRE_CONFIRM_ALIAS", True))
    default_grams_min_samples: int = field(default_factory=lambda: _int_env("MEMORY_LEARNER_DEFAULT_GRAMS_MIN_SAMPLES", 3))
    auto_confirm_explicit: bool = field(default_factory=lambda: _bool_env("MEMORY_LEARNER_AUTO_CONFIRM_EXPLICIT", True))
    debug: bool = field(default_factory=lambda: _bool_env("MEMORY_LEARNER_DEBUG", False))


def learn_from_event(
    event: LearningEvent | dict[str, Any],
    *,
    store: Any | None = None,
    options: LearningOptions | None = None,
    legacy: ModuleType = legacy_app,
) -> list[MemoryCandidate]:
    opts = options or LearningOptions()
    if not opts.enabled:
        return []

    learning_event = event if isinstance(event, LearningEvent) else LearningEvent(**dict(event or {}))
    if not str(learning_event.user_id or "").strip():
        return []

    include_trace_based = _user_learning_enabled(learning_event.user_id, legacy=legacy)
    candidates = generate_memory_candidates(
        learning_event,
        options=RuleOptions(
            min_confidence=opts.min_confidence,
            require_confirm_alias=opts.require_confirm_alias,
            default_grams_min_samples=opts.default_grams_min_samples,
            auto_confirm_explicit=opts.auto_confirm_explicit,
        ),
        food_lookup=food_lookup_from_legacy(legacy),
        include_trace_based=include_trace_based,
    )
    if not include_trace_based:
        candidates = [candidate for candidate in candidates if candidate.source == "explicit_user"]

    for candidate in candidates:
        _safe_save(candidate, store=store, legacy=legacy)
    return candidates


def _safe_save(candidate: MemoryCandidate, *, store: Any | None, legacy: ModuleType) -> dict[str, Any]:
    try:
        if store is not None and hasattr(store, "save_memory_candidate"):
            return store.save_memory_candidate(candidate)
        return memory_store.save_memory_candidate(candidate, legacy=legacy)
    except Exception as exc:
        return {"status": "error", "reason": type(exc).__name__}


def _user_learning_enabled(user_id: str, *, legacy: ModuleType) -> bool:
    try:
        prefs = preferences.get_preference_map({"user_id": str(user_id or "")}, legacy=legacy)
    except Exception:
        return True
    for key in ("learning_enabled", "auto_learning", "personalization_enabled"):
        if key in prefs:
            return _bool_value(prefs[key], True)
    return True


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return _bool_value(value, default)


def _bool_value(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled", "enable"}:
        return True
    if text in {"0", "false", "no", "off", "disabled", "disable"}:
        return False
    return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default
