from __future__ import annotations

from types import ModuleType
from typing import Any

from .. import legacy_app
from ..domain import aliases, combos, memory, preferences
from .learning_rules import MemoryCandidate


def save_memory_candidate(
    candidate: MemoryCandidate,
    *,
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    try:
        return _save_memory_candidate(candidate, legacy=legacy)
    except Exception as exc:
        return {"status": "error", "reason": type(exc).__name__}


def confirm_memory(
    candidate_id: str,
    *,
    user_id: str = "",
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    row = _find_memory_row(candidate_id, user_id=user_id, legacy=legacy)
    if not row:
        return {"status": "not_found", "candidate_id": candidate_id}
    message = {"user_id": row.get("user_id") or user_id}
    payload = {
        "memory_id": row.get("memory_id") or candidate_id,
        "source": row.get("source") or "alias_candidate",
        "food_id": row.get("food_id", ""),
        "food_name": row.get("food_name", ""),
        "alias": row.get("alias", ""),
        "default_grams": row.get("default_grams"),
        "meal_type": row.get("meal_type") or "unknown",
        "sample_count": row.get("sample_count") or 1,
        "confidence": row.get("confidence") or 1.0,
        "status": "confirmed",
    }
    return memory.upsert_memory(message, payload, legacy=legacy)


def reject_memory(
    candidate_id: str,
    *,
    user_id: str = "",
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    row = _find_memory_row(candidate_id, user_id=user_id, legacy=legacy)
    if not row:
        return {"status": "not_found", "candidate_id": candidate_id}
    message = {"user_id": row.get("user_id") or user_id}
    payload = {
        "memory_id": row.get("memory_id") or candidate_id,
        "source": row.get("source") or "alias_candidate",
        "food_id": row.get("food_id", ""),
        "food_name": row.get("food_name", ""),
        "alias": row.get("alias", ""),
        "default_grams": row.get("default_grams"),
        "meal_type": row.get("meal_type") or "unknown",
        "sample_count": row.get("sample_count") or 1,
        "confidence": row.get("confidence") or 0.0,
        "status": "rejected",
    }
    return memory.upsert_memory(message, payload, legacy=legacy)


def delete_memory(
    user_id: str,
    memory_type: str,
    key: str,
    *,
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    message = {"user_id": str(user_id or "")}
    kind = str(memory_type or "").strip()
    if kind == "combo":
        return combos.delete_combo(message, key, legacy=legacy)
    if kind == "preference":
        return preferences.delete_preferences(message, {"key": key}, legacy=legacy)
    if kind == "alias":
        return aliases.delete_alias(message, {"alias": key}, legacy=legacy)
    return memory.soft_delete_memories(message, {"memory_type": kind, "name": key}, legacy=legacy)


def list_memories(
    user_id: str,
    memory_type: str | None = None,
    *,
    legacy: ModuleType = legacy_app,
) -> list[dict[str, Any]]:
    message = {"user_id": str(user_id or "")}
    kind = str(memory_type or "").strip()
    if kind == "combo":
        return combos.list_combos(message, legacy=legacy)
    if kind == "preference":
        return preferences.list_preferences(message, legacy=legacy)
    if kind == "alias":
        return memory.list_user_memories(message, source="alias", legacy=legacy)
    return memory.list_user_memories(message, legacy=legacy)


def disable_learning(user_id: str, *, legacy: ModuleType = legacy_app) -> dict[str, Any]:
    return preferences.update_preference({"user_id": str(user_id or "")}, "learning_enabled", "false", legacy=legacy)


def enable_learning(user_id: str, *, legacy: ModuleType = legacy_app) -> dict[str, Any]:
    return preferences.update_preference({"user_id": str(user_id or "")}, "learning_enabled", "true", legacy=legacy)


class InMemoryMemoryStore:
    def __init__(self, configured: bool = True) -> None:
        self.configured = configured
        self.rows: list[dict[str, Any]] = []

    def save_memory_candidate(self, candidate: MemoryCandidate) -> dict[str, Any]:
        if not self.configured:
            return {"status": "disabled", "reason": "memory_table_not_configured"}
        row = candidate.to_dict()
        existing = self._find_duplicate(row)
        if existing is not None:
            existing.update(row)
            existing["sample_count"] = int(existing.get("sample_count") or 1) + 1
            return {"status": "updated", "candidate_id": candidate.candidate_id, "memory": dict(existing)}
        row["sample_count"] = int(candidate.value.get("sample_count") or 1)
        self.rows.append(row)
        return {"status": "created", "candidate_id": candidate.candidate_id, "memory": dict(row)}

    def confirm_memory(self, candidate_id: str, *, user_id: str = "") -> dict[str, Any]:
        row = self._find_by_id(candidate_id, user_id=user_id)
        if row is None:
            return {"status": "not_found", "candidate_id": candidate_id}
        row["status"] = "confirmed"
        return {"status": "confirmed", "candidate_id": candidate_id, "memory": dict(row)}

    def reject_memory(self, candidate_id: str, *, user_id: str = "") -> dict[str, Any]:
        row = self._find_by_id(candidate_id, user_id=user_id)
        if row is None:
            return {"status": "not_found", "candidate_id": candidate_id}
        row["status"] = "rejected"
        return {"status": "rejected", "candidate_id": candidate_id, "memory": dict(row)}

    def delete_memory(self, user_id: str, memory_type: str, key: str) -> dict[str, Any]:
        matches = [
            row
            for row in self.rows
            if row.get("user_id") == user_id and row.get("memory_type") == memory_type and row.get("key") == key
        ]
        for row in matches:
            row["status"] = "deleted"
        return {"status": "deleted" if matches else "not_found", "deleted_count": len(matches), "items": [dict(row) for row in matches]}

    def list_memories(self, user_id: str, memory_type: str | None = None) -> list[dict[str, Any]]:
        return [
            dict(row)
            for row in self.rows
            if row.get("user_id") == user_id
            and row.get("status") != "deleted"
            and (not memory_type or row.get("memory_type") == memory_type)
        ]

    def disable_learning(self, user_id: str) -> dict[str, Any]:
        candidate = MemoryCandidate(
            candidate_id=f"{user_id}:learning_enabled",
            user_id=user_id,
            memory_type="preference",
            key="learning_enabled",
            value={"key": "learning_enabled", "value": "false"},
            confidence=1.0,
            source="explicit_user",
            status="confirmed",
        )
        return self.save_memory_candidate(candidate)

    def enable_learning(self, user_id: str) -> dict[str, Any]:
        candidate = MemoryCandidate(
            candidate_id=f"{user_id}:learning_enabled",
            user_id=user_id,
            memory_type="preference",
            key="learning_enabled",
            value={"key": "learning_enabled", "value": "true"},
            confidence=1.0,
            source="explicit_user",
            status="confirmed",
        )
        return self.save_memory_candidate(candidate)

    def _find_by_id(self, candidate_id: str, *, user_id: str = "") -> dict[str, Any] | None:
        for row in self.rows:
            if row.get("candidate_id") == candidate_id and (not user_id or row.get("user_id") == user_id):
                return row
        return None

    def _find_duplicate(self, candidate: dict[str, Any]) -> dict[str, Any] | None:
        for row in self.rows:
            if (
                row.get("user_id") == candidate.get("user_id")
                and row.get("memory_type") == candidate.get("memory_type")
                and row.get("key") == candidate.get("key")
                and row.get("value") == candidate.get("value")
            ):
                return row
        return None


def _save_memory_candidate(candidate: MemoryCandidate, *, legacy: ModuleType) -> dict[str, Any]:
    if candidate.memory_type == "alias":
        return _save_alias(candidate, legacy=legacy)
    if candidate.memory_type == "default_grams":
        return _save_default_grams(candidate, legacy=legacy)
    if candidate.memory_type == "combo":
        return _save_combo(candidate, legacy=legacy)
    if candidate.memory_type == "preference":
        return _save_preference(candidate, legacy=legacy)
    if candidate.memory_type == "query_pattern":
        return _save_query_pattern(candidate, legacy=legacy)
    return {"status": "invalid", "reason": "unsupported_memory_type"}


def _save_alias(candidate: MemoryCandidate, *, legacy: ModuleType) -> dict[str, Any]:
    value = candidate.value
    source = "alias" if candidate.status == "confirmed" else "alias_candidate"
    status = "active" if candidate.status == "confirmed" else candidate.status
    return memory.upsert_memory(
        {"user_id": candidate.user_id, "message_id": candidate.source_message_id},
        {
            "memory_id": candidate.candidate_id,
            "source": source,
            "food_id": value.get("food_id", ""),
            "food_name": value.get("food_name", ""),
            "alias": value.get("alias", candidate.key),
            "confidence": candidate.confidence,
            "sample_count": int(value.get("sample_count") or 1),
            "status": status,
        },
        legacy=legacy,
    )


def _save_default_grams(candidate: MemoryCandidate, *, legacy: ModuleType) -> dict[str, Any]:
    value = candidate.value
    return memory.upsert_memory(
        {"user_id": candidate.user_id, "message_id": candidate.source_message_id},
        {
            "memory_id": candidate.candidate_id,
            "source": "default_grams",
            "food_id": value.get("food_id", ""),
            "food_name": value.get("food_name", candidate.key),
            "default_grams": value.get("grams"),
            "confidence": candidate.confidence,
            "sample_count": int(value.get("sample_count") or 1),
            "status": candidate.status,
        },
        legacy=legacy,
    )


def _save_combo(candidate: MemoryCandidate, *, legacy: ModuleType) -> dict[str, Any]:
    value = candidate.value
    if candidate.status != "confirmed":
        return {"status": "skipped", "reason": "combo_candidate_requires_confirmation"}
    return combos.create_combo(
        {"user_id": candidate.user_id},
        combo_name=str(value.get("combo_name") or candidate.key),
        meal_type=str(value.get("meal_type") or "unknown"),
        items=value.get("items") if isinstance(value.get("items"), list) else [],
        confidence=candidate.confidence,
        legacy=legacy,
    )


def _save_preference(candidate: MemoryCandidate, *, legacy: ModuleType) -> dict[str, Any]:
    value = candidate.value
    if candidate.status != "confirmed":
        return {"status": "skipped", "reason": "preference_candidate_requires_confirmation"}
    return preferences.update_preference(
        {"user_id": candidate.user_id},
        key=str(value.get("key") or candidate.key),
        value=value.get("value"),
        confidence=candidate.confidence,
        legacy=legacy,
    )


def _save_query_pattern(candidate: MemoryCandidate, *, legacy: ModuleType) -> dict[str, Any]:
    value = candidate.value
    return preferences.update_preference(
        {"user_id": candidate.user_id},
        key=f"query_pattern:{candidate.key}",
        value=value.get("tool", ""),
        confidence=candidate.confidence,
        legacy=legacy,
    )


def _find_memory_row(candidate_id: str, *, user_id: str, legacy: ModuleType) -> dict[str, Any] | None:
    rows = memory.list_user_memories({"user_id": user_id}, include_deleted=True, legacy=legacy)
    for row in rows:
        if str(row.get("memory_id") or "") == str(candidate_id):
            return row
    return None
