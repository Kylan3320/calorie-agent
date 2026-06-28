from __future__ import annotations

import re
from types import ModuleType
from typing import Any

from .. import legacy_app


def undo_intake(
    message: dict[str, str],
    target: dict[str, Any],
    legacy: ModuleType = legacy_app,
) -> dict[str, Any]:
    mode = str(target.get("mode") or "ordinal_group")
    items = legacy._load_today_intake_summary(message).get("items", [])
    if not isinstance(items, list) or not items:
        return {"status": "nothing_to_undo", "target": target, "items": []}

    if mode in {"ordinal", "ordinal_group"}:
        return _undo_ordinal_group(message, items, target, legacy)
    if mode == "contains_food":
        return _undo_contains_food(message, items, target, legacy)
    if mode == "meal_type":
        return _undo_meal_type(message, items, target, legacy)
    return {"status": "unsupported_target", "target": target}


def _undo_ordinal_group(
    message: dict[str, str],
    items: list[dict[str, Any]],
    target: dict[str, Any],
    legacy: ModuleType,
) -> dict[str, Any]:
    groups = _message_groups(items)
    ordinal = _int(target.get("ordinal"), -1)
    index = ordinal if ordinal < 0 else ordinal - 1
    if abs(index) > len(groups):
        return {"status": "nothing_to_undo", "target": target, "candidate_groups": groups}
    return _delete_items(message, groups[index]["items"], target, legacy)


def _undo_contains_food(
    message: dict[str, str],
    items: list[dict[str, Any]],
    target: dict[str, Any],
    legacy: ModuleType,
) -> dict[str, Any]:
    query = _norm(target.get("food_query") or target.get("food_name") or target.get("name"))
    if not query:
        return {"status": "invalid_target", "target": target}

    matches = [item for item in items if query in _norm(item.get("food_name")) or _norm(item.get("food_name")) in query]
    if not matches:
        return {"status": "nothing_to_undo", "target": target, "items": []}

    if str(target.get("group_scope") or "") == "message_group":
        groups = [group for group in _message_groups(items) if any(item in matches for item in group["items"])]
        if len(groups) > 1 and not target.get("pick_latest"):
            return {"status": "ambiguous", "target": target, "candidate_groups": groups}
        return _delete_items(message, groups[-1]["items"], target, legacy)

    if len(matches) > 1 and target.get("require_confirmation"):
        return {"status": "ambiguous", "target": target, "items": matches}
    return _delete_items(message, [matches[-1]], target, legacy)


def _undo_meal_type(
    message: dict[str, str],
    items: list[dict[str, Any]],
    target: dict[str, Any],
    legacy: ModuleType,
) -> dict[str, Any]:
    meal_type = str(target.get("meal_type") or "").strip()
    if not meal_type:
        return {"status": "invalid_target", "target": target}
    groups = [
        group
        for group in _message_groups(items)
        if any(str(item.get("meal_type") or "") == meal_type for item in group["items"])
    ]
    if not groups:
        return {"status": "nothing_to_undo", "target": target, "candidate_groups": []}
    if len(groups) > 1 and not target.get("pick_latest"):
        return {"status": "ambiguous", "target": target, "candidate_groups": groups}
    return _delete_items(message, groups[-1]["items"], target, legacy)


def _delete_items(
    message: dict[str, str],
    items: list[dict[str, Any]],
    target: dict[str, Any],
    legacy: ModuleType,
) -> dict[str, Any]:
    if callable(getattr(legacy, "_v3_mysql_write_enabled", None)) and legacy._v3_mysql_write_enabled():
        return legacy._v3_soft_delete_intake_items(message, items, target)

    settings = legacy._settings()
    app_token = settings["bitable_app_token"]
    table_id = settings["bitable_intake_table_id"]
    if not app_token or not table_id:
        raise RuntimeError("BITABLE_APP_TOKEN and BITABLE_INTAKE_TABLE_ID are not configured.")

    status_field = legacy._intake_field(settings, "status")
    updates = [
        legacy._update_bitable_record(app_token, table_id, item["record_id"], {status_field: "deleted"})
        for item in items
        if item.get("record_id")
    ]
    return {
        "status": "deleted" if updates else "nothing_to_undo",
        "target": target,
        "deleted_count": len(updates),
        "items": items,
        "update_results": updates,
    }


def _message_groups(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, dict[str, Any]] = {}
    for index, item in enumerate(items):
        key = _group_key(item)
        group = groups.setdefault(key, {"group_key": key, "items": [], "created_at": 0.0, "last_index": index})
        group["items"].append(item)
        group["created_at"] = max(float(group["created_at"]), float(item.get("created_at") or 0.0))
        group["last_index"] = index
    return sorted(groups.values(), key=lambda group: (group["created_at"], group["last_index"], group["group_key"]))


def _group_key(item: dict[str, Any]) -> str:
    message_id = str(item.get("message_id") or "")
    return re.sub(r":agent:\d+$", "", message_id) or str(item.get("record_id") or "")


def _norm(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or "").lower())


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
