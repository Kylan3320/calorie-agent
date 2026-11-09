from __future__ import annotations

from types import ModuleType
from typing import Any

from .. import legacy_app
from ..domain import combos, memory, preferences


MAX_ALIASES = 20
MAX_DEFAULT_GRAMS = 12
MAX_COMBOS = 8


def build_memory_context(
    message: dict[str, str],
    settings: dict[str, str],
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    if not memory.personal_memory_enabled(settings):
        return {"enabled": False, "reason": "personal_memory_disabled"}

    context: dict[str, Any] = {
        "enabled": True,
        "memory_table_enabled": memory.memory_table_enabled(settings),
        "combo_table_enabled": combos.combo_table_enabled(settings),
        "preference_table_enabled": preferences.preference_table_enabled(settings),
        "aliases": [],
        "default_grams": [],
        "combos": [],
        "preferences": {},
    }
    if not (context["memory_table_enabled"] or context["combo_table_enabled"] or context["preference_table_enabled"]):
        context["enabled"] = False
        context["reason"] = "memory_tables_not_configured"
        return context

    if context["memory_table_enabled"]:
        rows = memory.list_user_memories(message, settings=settings, legacy=legacy)
        context["aliases"] = [
            {
                "alias": row.get("alias"),
                "food_name": row.get("food_name"),
                "food_id": row.get("food_id"),
                "confidence": row.get("confidence"),
            }
            for row in rows
            if row.get("source") == "alias" and row.get("alias")
        ][:MAX_ALIASES]
        context["default_grams"] = [
            {
                "food_name": row.get("food_name"),
                "food_id": row.get("food_id"),
                "grams": row.get("default_grams"),
                "sample_count": row.get("sample_count"),
                "confidence": row.get("confidence"),
            }
            for row in rows
            if row.get("source") == "default_grams" and row.get("default_grams") is not None
        ][:MAX_DEFAULT_GRAMS]

    if context["combo_table_enabled"]:
        context["combos"] = [
            {
                "combo_name": row.get("combo_name"),
                "meal_type": row.get("meal_type"),
                "items": row.get("items", [])[:6],
                "use_count": row.get("use_count"),
            }
            for row in combos.list_combos(message, legacy=legacy)
        ][:MAX_COMBOS]

    if context["preference_table_enabled"]:
        context["preferences"] = preferences.get_preference_map(message, legacy=legacy)

    return context
