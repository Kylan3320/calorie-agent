from __future__ import annotations

from dataclasses import asdict, dataclass, field
import os
from typing import Any

from .profile_store import empty_profile_sources, load_profile_sources


REPLY_STYLES = {"compact", "normal", "detailed"}
MEAL_INFERENCE_MODES = {"conservative", "time_based", "disabled"}
MEAL_TYPES = {"breakfast", "lunch", "dinner", "snack", "unknown"}


@dataclass(frozen=True)
class AliasHint:
    alias: str
    food_name: str
    confidence: float
    source: str = "memory"


@dataclass(frozen=True)
class MealPatternHint:
    phrase: str
    meal_type: str
    confidence: float


@dataclass(frozen=True)
class PlannerProfile:
    user_id: str
    reply_style: str = "normal"
    default_meal_inference: str = "conservative"
    prefer_confirm_default_grams: bool = True
    auto_use_default_grams: bool = False
    common_food_aliases: list[AliasHint] = field(default_factory=list)
    common_meal_patterns: list[MealPatternHint] = field(default_factory=list)
    common_query_patterns: list[str] = field(default_factory=list)
    preferred_units: list[str] = field(default_factory=lambda: ["g"])
    learning_enabled: bool = True
    confidence: float = 0.0
    updated_at: int = 0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ProfileOptions:
    include_aliases: bool = True
    include_meal_patterns: bool = True
    include_query_patterns: bool = True
    include_trace_stats: bool = field(default_factory=lambda: _bool_env("PLANNER_PROFILE_INCLUDE_TRACE_STATS", True))
    max_aliases: int = field(default_factory=lambda: _int_env("PLANNER_PROFILE_MAX_ALIASES", 20))
    max_patterns: int = field(default_factory=lambda: _int_env("PLANNER_PROFILE_MAX_PATTERNS", 10))
    min_confidence: float = field(default_factory=lambda: _float_env("PLANNER_PROFILE_MIN_CONFIDENCE", 0.7))
    enabled: bool = field(default_factory=lambda: _bool_env("PLANNER_PROFILE_ENABLED", True))
    allow_candidate_hints: bool = False
    profile_sources: dict[str, Any] | None = None


def load_planner_profile(user_id: str, options: ProfileOptions | None = None) -> PlannerProfile:
    opts = options or ProfileOptions()
    user_id = str(user_id or "").strip()
    if not opts.enabled:
        return _default_profile(user_id, learning_enabled=False)

    sources = opts.profile_sources
    if sources is None:
        sources = load_profile_sources(user_id, include_trace_stats=opts.include_trace_stats)
    if not isinstance(sources, dict):
        sources = empty_profile_sources()

    preferences = _preference_map(sources.get("preferences"))
    learning_enabled = _bool_pref(preferences, ("learning_enabled", "auto_learning", "personalization_enabled"), True)
    trace_stats = sources.get("trace_stats") if learning_enabled else {}
    trace_stats = trace_stats if isinstance(trace_stats, dict) else {}

    auto_use = _bool_pref(
        preferences,
        ("auto_use_default_grams", "auto_default_grams"),
        _bool_value(_settings_value(sources, "auto_use_default_grams"), False),
    )
    prefer_confirm = _bool_pref(
        preferences,
        ("prefer_confirm_default_grams", "confirm_default_grams"),
        not auto_use,
    )

    aliases = _alias_hints(user_id, sources, opts) if opts.include_aliases else []
    meal_patterns = _meal_pattern_hints(trace_stats, opts) if opts.include_meal_patterns and learning_enabled else []
    query_patterns = _query_patterns(trace_stats, opts) if opts.include_query_patterns and learning_enabled else []
    confidence_values = [hint.confidence for hint in aliases] + [hint.confidence for hint in meal_patterns]

    return PlannerProfile(
        user_id=user_id,
        reply_style=_choice(_preference_value(preferences, "reply_style"), REPLY_STYLES, "normal"),
        default_meal_inference=_choice(
            _preference_value(preferences, "default_meal_inference"),
            MEAL_INFERENCE_MODES,
            "conservative",
        ),
        prefer_confirm_default_grams=prefer_confirm,
        auto_use_default_grams=auto_use,
        common_food_aliases=aliases,
        common_meal_patterns=meal_patterns,
        common_query_patterns=query_patterns,
        preferred_units=_preferred_units(preferences),
        learning_enabled=learning_enabled,
        confidence=round(max(confidence_values) if confidence_values else 0.0, 3),
        updated_at=_updated_at(sources),
    )


def render_profile_for_planner(profile: PlannerProfile) -> dict[str, Any]:
    return {
        "reply_style": profile.reply_style,
        "default_meal_inference": profile.default_meal_inference,
        "prefer_confirm_default_grams": profile.prefer_confirm_default_grams,
        "auto_use_default_grams": profile.auto_use_default_grams,
        "common_food_aliases": [
            {
                "alias": hint.alias,
                "food_name": hint.food_name,
                "confidence": round(float(hint.confidence), 3),
            }
            for hint in profile.common_food_aliases
        ],
        "common_meal_patterns": [
            {
                "phrase": hint.phrase,
                "meal_type": hint.meal_type,
                "confidence": round(float(hint.confidence), 3),
            }
            for hint in profile.common_meal_patterns
        ],
        "common_query_patterns": list(profile.common_query_patterns),
        "preferred_units": list(profile.preferred_units),
        "learning_enabled": profile.learning_enabled,
        "confidence": round(float(profile.confidence), 3),
    }


def _default_profile(user_id: str, learning_enabled: bool = True) -> PlannerProfile:
    return PlannerProfile(user_id=user_id, learning_enabled=learning_enabled)


def _alias_hints(user_id: str, sources: dict[str, Any], opts: ProfileOptions) -> list[AliasHint]:
    rows = sources.get("memory_aliases")
    if not isinstance(rows, list):
        rows = sources.get("aliases") if isinstance(sources.get("aliases"), list) else []
    hints: list[AliasHint] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_user = str(row.get("user_id") or row.get("owner_user_id") or "").strip()
        if row_user and user_id and row_user != user_id:
            continue
        alias = str(row.get("alias") or "").strip()
        food_name = str(row.get("food_name") or row.get("name") or "").strip()
        confidence = _float(row.get("confidence"), 0.0)
        if not alias or not food_name or confidence < opts.min_confidence:
            continue
        source = str(row.get("source") or "memory").strip()
        status = str(row.get("status") or "active").strip().lower()
        if status in {"deleted", "rejected"}:
            continue
        if not opts.allow_candidate_hints and (status == "candidate" or source == "alias_candidate"):
            continue
        if source in {"alias", "alias_candidate", "default_grams"}:
            source = "memory"
        if status in {"confirmed", "active"} and source == "confirmed":
            source = "confirmed"
        hints.append(AliasHint(alias=alias, food_name=food_name, confidence=round(confidence, 3), source=source))
    return sorted(hints, key=lambda item: (-item.confidence, item.alias, item.food_name))[: max(0, opts.max_aliases)]


def _meal_pattern_hints(trace_stats: dict[str, Any], opts: ProfileOptions) -> list[MealPatternHint]:
    rows = trace_stats.get("common_meal_patterns") if isinstance(trace_stats.get("common_meal_patterns"), list) else []
    hints: list[MealPatternHint] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        phrase = str(row.get("phrase") or "").strip()
        meal_type = _choice(row.get("meal_type"), MEAL_TYPES, "unknown")
        confidence = _float(row.get("confidence"), 0.0)
        if phrase and confidence >= opts.min_confidence:
            hints.append(MealPatternHint(phrase=phrase, meal_type=meal_type, confidence=round(confidence, 3)))
    return sorted(hints, key=lambda item: (-item.confidence, item.phrase))[: max(0, opts.max_patterns)]


def _query_patterns(trace_stats: dict[str, Any], opts: ProfileOptions) -> list[str]:
    rows = trace_stats.get("common_query_patterns") if isinstance(trace_stats.get("common_query_patterns"), list) else []
    result: list[str] = []
    for item in rows:
        text = str(item or "").strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= opts.max_patterns:
            break
    return result


def _preference_map(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {str(key): str(item) for key, item in value.items()}
    if not isinstance(value, list):
        return {}
    result: dict[str, str] = {}
    for row in value:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or "").strip()
        if key:
            result[key] = str(row.get("value") or "")
    return result


def _preference_value(preferences: dict[str, str], key: str) -> str:
    return str(preferences.get(key) or "").strip().lower()


def _bool_pref(preferences: dict[str, str], keys: tuple[str, ...], default: bool) -> bool:
    for key in keys:
        if key in preferences:
            return _bool_value(preferences[key], default)
    return default


def _bool_value(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled", "enable"}:
        return True
    if text in {"0", "false", "no", "off", "disabled", "disable"}:
        return False
    return default


def _settings_value(sources: dict[str, Any], key: str) -> Any:
    settings = sources.get("settings") if isinstance(sources.get("settings"), dict) else {}
    return settings.get(key)


def _choice(value: Any, allowed: set[str], default: str) -> str:
    text = str(value or "").strip().lower()
    aliases = {
        "simple": "compact",
        "brief": "compact",
        "short": "compact",
        "简洁": "compact",
        "简单": "compact",
        "详细": "detailed",
    }
    text = aliases.get(text, text)
    return text if text in allowed else default


def _preferred_units(preferences: dict[str, str]) -> list[str]:
    raw = str(preferences.get("preferred_units") or "g").strip()
    values = [item.strip() for item in raw.replace("，", ",").split(",") if item.strip()]
    return values[:5] or ["g"]


def _updated_at(sources: dict[str, Any]) -> int:
    values: list[int] = []
    for key in ("preferences", "memory_aliases", "default_grams", "combos"):
        rows = sources.get(key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if isinstance(row, dict):
                values.append(int(_float(row.get("updated_at") or row.get("created_at"), 0)))
    return max(values) if values else 0


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
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


def _bool_env(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return _bool_value(value, default)
