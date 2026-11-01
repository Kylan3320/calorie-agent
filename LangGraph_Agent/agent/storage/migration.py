"""Explicit release-only additive schema update and legacy fact migration."""
import json
from copy import deepcopy
from . import feishu_client
from .bitable_adapter import _record_url, _pending_field, _list_records
from .timeutil import extract_text

def ensure_schema(store):
    """Add only missing fields; never replace tables or alter existing columns."""
    auth = {"Authorization": "Bearer " + feishu_client.get_tenant_access_token()}
    added = []
    fields = {store.food_table: [("kcal_per_100g", 2), ("units", 1), ("detail_loaded", 1)],
              store.intake_table: [("snapshot", 1)], store.pending_table: [(_pending_field("payload"), 1)]}
    for table, wanted in fields.items():
        url = _record_url(store.app_token, table).rsplit("/", 1)[0] + "/fields"
        current = []
        page = ""
        while True:
            result = feishu_client.http_json_get(url+"?page_size=100"+("&page_token="+page if page else ""), auth)
            if result.get("code") != 0:
                raise RuntimeError(f"list fields failed: {result.get('code')}")
            body = result["data"]
            current.extend(f["field_name"] for f in body.get("items", []))
            if not body.get("has_more"): break
            page = body["page_token"]
        for name, kind in wanted:
            if name in current: continue
            result = feishu_client.http_json_post(url, {"field_name": name, "type": kind}, auth)
            if result.get("code") != 0:
                raise RuntimeError(f"create field {name} failed: {result.get('code')}")
            added.append({"table": table, "field": name})
    return added

def migrate_legacy_pending(store, *, dry_run=True):
    """Run after old inbox/executions drain. Only waiting facts enter new sessions."""
    actions = []
    for rid, fields in _list_records(store.app_token, store.pending_table):
        task = store._pending_task(rid, fields)
        if task["status"] != "pending": continue
        intent = task["intent"]
        if intent in {"message_received", "message_execution"}:
            if not task["payload"].get("calls") and (intent == "message_received" or task["payload"].get("plan")):
                raise RuntimeError("old accepted execution/reply must be drained before migration")
            continue
        if intent not in {"missing_grams", "missing_food", "confirm_undo"}: continue
        from .timeutil import now_millis
        if task["expires_at"] and task["expires_at"] < now_millis():
            actions.append({"pending_id": task["pending_id"], "intent": intent, "expired": True})
            if not dry_run:
                store.mark_pending_status(task, "cancelled")
            continue
        session = store.load_session(task["user_id"], task["chat_id"])
        p = task["payload"]
        if intent in {"missing_grams", "missing_food"}:
            candidates = [deepcopy(c.get("food", c)) for c in p.get("candidates", [])]
            fact = {"pending_id": task["pending_id"], "food_query": task["food_name"],
                    "raw_text": task["raw_text"], "date": p.get("intake_date") or p.get("date"),
                    "meal_type": task["meal_type"], "candidates": candidates, "reason": "请补全原餐"}
            if not fact["date"]:
                from datetime import datetime, timedelta, timezone
                fact["date"] = datetime.fromtimestamp(task["created_at"]/1000, timezone(timedelta(hours=store.offset_hours))).date().isoformat()
            if task["grams"] is not None: fact["grams"] = task["grams"]
            session.setdefault("unfinished", {})[task["pending_id"]] = fact
        elif p.get("target"):
            session.setdefault("refs", []).append(p["target"]["record_id"])
        actions.append({"pending_id": task["pending_id"], "intent": intent})
        if not dry_run:
            store.save_session(task["user_id"], task["chat_id"], session)
            store.mark_pending_status(task, "cancelled")
    return actions
