"""Explicit release-only additive schema update and legacy fact migration."""
import json
import time
from copy import deepcopy
from . import feishu_client
from .bitable_adapter import _record_url, _pending_field, _list_records
from .timeutil import extract_text

def ensure_schema(store):
    """Add only missing fields; never replace tables or alter existing columns."""
    auth = {"Authorization": "Bearer " + feishu_client.get_tenant_access_token()}
    added = []
    fields = {store.food_table: [("kcal_per_100g", 2), ("units", 1), ("detail_loaded", 1), ("source", 1), ("boohee_id", 1)],
              store.intake_table: [("snapshot", 1), ("agent_item_id", 1), ("agent_revision", 2)],
              store.pending_table: [(_pending_field("payload"), 1)]}
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


def export_cos_baseline(store):
    """Read the drained legacy ledger without mutating any source row.

    The caller must hold the maintenance window. All intake statuses, task
    identities, original nutrition, and pending delivery facts are retained.
    """
    from datetime import datetime, timedelta, timezone
    from .bitable_adapter import _intake_field, _std_field
    from .timeutil import extract_number
    from .bitable_adapter import stable_uuid
    zone = timezone(timedelta(hours=store.offset_hours))

    def day(value):
        number = extract_number(value)
        return datetime.fromtimestamp(number / 1000, zone).date().isoformat() if number is not None else extract_text(value)

    intake = []
    for rid, fields in store._iter_intake_rows():
        row = store._intake_summary_item(rid, fields)
        row.update(date=day(fields.get(_intake_field("date"))),
                   user_id=extract_text(fields.get(_intake_field("user_id"))),
                   raw_text=extract_text(fields.get(_intake_field("raw_text"))),
                   food_id=extract_text(fields.get(_intake_field("food_id"))),
                   status=extract_text(fields.get(_intake_field("status"))), feishu_record_id=rid)
        intake.append(row)
    pending = [store._pending_task(rid, fields) for rid, fields in _list_records(store.app_token, store.pending_table)]
    for task in pending:
        if task["intent"] in {"message_received", "message_execution"} and task["status"] == "pending":
            raise RuntimeError("maintenance window must drain accepted executions and replies before export")
    sessions = {(t["user_id"], t["chat_id"]): t for t in pending if t["intent"] == "agent_session"}
    for task in pending:
        if task["status"] != "pending" or task["intent"] not in {"missing_grams", "missing_food", "confirm_undo"}:
            continue
        identity = task["user_id"], task["chat_id"]
        if identity not in sessions:
            uid, sid = identity
            key = f"agent_session:{uid}:{sid}"
            session_task = {**deepcopy(task), "record_id": "", "pending_id": stable_uuid(f"calorie-agent:pending:{key}:agent_session:"),
                            "intent": "agent_session", "source_message_id": key, "payload": {}, "expires_at": 0}
            sessions[identity] = session_task
        session = sessions[identity]["payload"]
        p = task["payload"]
        if not task["expires_at"] or task["expires_at"] >= time.time() * 1000:
            if task["intent"] in {"missing_grams", "missing_food"}:
                fact = {"pending_id": task["pending_id"], "food_query": task["food_name"], "raw_text": task["raw_text"],
                        "date": p.get("intake_date") or p.get("date") or day(task["created_at"]),
                        "meal_type": task["meal_type"], "candidates": [deepcopy(c.get("food", c)) for c in p.get("candidates", [])],
                        "reason": "请补全原餐"}
                if task["grams"] is not None:
                    fact["grams"] = task["grams"]
                session.setdefault("unfinished", {})[task["pending_id"]] = fact
            elif p.get("target"):
                refs = session.setdefault("refs", [])
                if p["target"]["record_id"] not in refs:
                    refs.append(p["target"]["record_id"])
        task["status"] = "cancelled"
    existing = {t["pending_id"] for t in pending}
    pending.extend(task for task in sessions.values() if task["pending_id"] not in existing)
    standards = []
    if store.standard_table:
        for _, fields in _list_records(store.app_token, store.standard_table):
            standards.append({"user_id": extract_text(fields.get(_std_field("user_id"))),
                              "date": day(fields.get(_std_field("date"))) if fields.get(_std_field("date")) else "",
                              **{key: extract_number(fields.get(_std_field(key))) for key in ("kcal", "carbs", "protein", "fat")
                                 if extract_number(fields.get(_std_field(key))) is not None}})
    # Source snapshots bypass the request cache during the maintenance window.
    foods = [{**row, "feishu_record_id": row["record_id"]} for rid, fields in _list_records(store.app_token, store.food_table)
             if (row := store._food_record_from_bitable({"record_id": rid, "fields": fields})) is not None]
    return {"schema": "calorie-agent.baseline.v1", "intake": intake, "foods": foods,
            "knowledge": store.load_knowledge() if store.knowledge_table else [], "aliases": store.load_user_aliases(),
            "standards": standards, "pending": pending}


def baseline_manifest(data):
    """Independent source/destination audit including deleted IDs and dedup state."""
    import hashlib
    from decimal import Decimal
    from .bitable_adapter import _is_valid_intake_status
    rows = data["intake"]
    if len({r["record_id"] for r in rows}) != len(rows):
        raise ValueError("baseline contains duplicate intake IDs")
    totals = {}
    for row in rows:
        if _is_valid_intake_status(row["status"]):
            key = row["user_id"] + ":" + row["date"]
            totals[key] = totals.get(key, Decimal(0)) + Decimal(str(row["nutrition"]["kcal"]))
    ledger = [{key: row.get(key) for key in ("record_id", "user_id", "date", "message_id", "grams", "nutrition", "status", "snapshot")}
              for row in sorted(rows, key=lambda r: r["record_id"])]
    pending = [{key: row.get(key) for key in ("pending_id", "user_id", "chat_id", "source_message_id", "intent", "status", "payload")}
               for row in sorted(data.get("pending", []), key=lambda r: r["pending_id"])]
    digest = lambda value: hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return {"schema": "calorie-agent.baseline-audit.v1", "intake_count": len(rows),
            "valid_count": sum(_is_valid_intake_status(r["status"]) for r in rows),
            "daily_kcal": {key: str(value) for key, value in sorted(totals.items())},
            "ledger_sha256": digest(ledger), "pending_sha256": digest(pending),
            "pending_count": len(pending), "source_sha256": digest(data)}


def import_cos_baseline(destination, data):
    """Create one baseline in an empty production prefix; verify the read source."""
    expected = baseline_manifest(data)
    fixed = deepcopy(data)
    fixed["baseline_id"] = expected["source_sha256"]
    destination.import_baseline(fixed)
    users = {row["user_id"] for row in data["intake"]}
    actual_rows = [row for uid in users for row in destination.intake_items(uid, include_deleted=True)]
    # Destination task API reads exact original IDs, including completed receipts.
    actual_tasks = [destination.get_pending(task["pending_id"]) for task in data.get("pending", [])]
    if any(task is None for task in actual_tasks):
        raise RuntimeError("baseline task/dedup identity readback failed")
    observed = baseline_manifest({**data, "intake": actual_rows, "pending": actual_tasks})
    for key in ("intake_count", "valid_count", "daily_kcal", "ledger_sha256", "pending_sha256", "pending_count"):
        if observed[key] != expected[key]:
            raise RuntimeError("baseline independent readback mismatch: " + key)
    if destination.load_user_aliases() != data.get("aliases", {}):
        raise RuntimeError("baseline alias snapshot readback mismatch")
    if sorted(destination.load_foods(), key=lambda row: row["food_id"]) != sorted(data.get("foods", []), key=lambda row: row["food_id"]):
        raise RuntimeError("baseline food snapshot readback mismatch")
    if destination.load_knowledge() != data.get("knowledge", []):
        raise RuntimeError("baseline knowledge snapshot readback mismatch")
    return {"source": expected, "verified": {key: observed[key] for key in ("intake_count", "valid_count", "daily_kcal", "ledger_sha256", "pending_sha256", "pending_count")}}
