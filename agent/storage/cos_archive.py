"""Single-writer Bitable archive; never called from the message execution path.

Requests and versions are durable before transmission. An ambiguous create is
resolved by its stable business key before replaying the same client token.
Only independently read-back fields advance the per-item archive watermark.
"""
from copy import deepcopy
from decimal import Decimal, InvalidOperation
import json
import os
import re
import time
import urllib.parse

from . import feishu_client
from .bitable_adapter import (
    BitableStore, _encode_pending_payload, _food_candidate_names, _intake_field,
    _list_records, _pending_field, _record_url, stable_uuid,
)
from .timeutil import extract_text


class ArchiveBlocked(RuntimeError):
    """A configuration or identity problem requiring operator attention."""


def _blocked_error(error):
    if isinstance(error, ArchiveBlocked):
        return True
    # Existing Bitable adapters retain Feishu's JSON error code in RuntimeError.
    # Keep transient codes (1254002, 1254290/1, 1254607, 1255xxx except token
    # conflict) retryable; permission/schema/limit errors require intervention.
    match = re.search(r"[\"']?code[\"']?\s*:\s*(\d+)", str(error))
    if match and int(match[1]) in {
        1254000, 1254001, 1254003, 1254004, 1254040, 1254041, 1254042, 1254043,
        1254045, 1254103, 1254104, 1254105, 1254106, 1254130,
        1254301, 1254302, 1254303, 1254304, 1255006,
    }:
        return True
    cause = error
    while cause:
        response = getattr(cause, "response", None)
        if response is not None and response.status_code in {401, 403, 404}:
            return True
        cause = cause.__cause__
    return False


def _archive_number(value):
    """Decode a Bitable numeric cell without applying business-value rounding.

    Accept finite numeric scalars/numeric text and singleton list or
    value/text/number wrappers. Wrapper keys use that order; an invalid selected
    value or a multi-value list is not evidence of a matching archive field.
    Decimal(str(float)) preserves the decimal value submitted in our JSON.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, list):
        return _archive_number(value[0]) if len(value) == 1 else None
    if isinstance(value, dict):
        for key in ("value", "text", "number"):
            if key in value:
                return _archive_number(value[key])
        return None
    if not isinstance(value, (int, float, str, Decimal)):
        return None
    text = str(value).strip()
    if not re.fullmatch(r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?", text):
        return None
    try:
        number = Decimal(text)
    except InvalidOperation:
        return None
    return number if number.is_finite() else None


def _same_fields(actual, expected):
    for key, value in expected.items():
        observed = actual.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            expected_number = _archive_number(value)
            if expected_number is None or _archive_number(observed) != expected_number:
                return False
        elif extract_text(observed) != str(value):
            return False
    return True


class BitableArchive:
    """Production archive transport, using the existing HTTP and field adapters."""
    def __init__(self, store=None):
        self.store = store or BitableStore()

    def prepare(self, change):
        store, row = self.store, change["value"]
        kind, identity, revision = change["kind"], change["id"], change["revision"]
        known = row.get("feishu_record_id", row.get("archive_record_id", ""))
        if kind == "intake":
            table, stable_field, stable_value = store.intake_table, "agent_item_id", identity
            date = row["date"]
            if os.getenv("BITABLE_FIELD_INTAKE_DATE_VALUE_TYPE", "timestamp") != "text":
                date = store._date_to_start_millis(date)
            values = {**{k: row.get(k, "") for k in ("user_id", "message_id", "raw_text", "meal_type", "food_id", "food_name")},
                      "date": date, "grams": row["grams"], "status": row.get("status", "valid"),
                      "created_at": row["created_at"], "snapshot": json.dumps(row.get("snapshot", {}), ensure_ascii=False),
                      **row["nutrition"]}
            fields = {_intake_field(k): v for k, v in values.items()}
            fields.update(agent_item_id=identity, agent_revision=revision)
        elif kind == "food":
            table, stable_field, stable_value = store.food_table, _food_candidate_names("food_id")[0], identity
            fields = {_food_candidate_names(k)[0]: row.get(v, "") for k, v in
                      (("food_id", "food_id"), ("food_name", "name"), ("carbs", "carbs_per_100g"),
                       ("protein", "protein_per_100g"), ("fat", "fat_per_100g"))}
            fields.update({_food_candidate_names("alias")[0]: "、".join(row.get("aliases", [])),
                           _food_candidate_names("status")[0]: row.get("status", "active"),
                           "source": row.get("source", ""), "boohee_id": row.get("boohee_id", ""),
                           "units": json.dumps(row.get("units", []), ensure_ascii=False),
                           "detail_loaded": "true" if row.get("detail_loaded") else "false"})
            if row.get("kcal_per_100g") is not None:
                fields["kcal_per_100g"] = row["kcal_per_100g"]
        elif kind == "settings":
            user = change.get("user_id") or identity
            key = f"user_settings:{user}:"
            table, stable_field = store.pending_table, _pending_field("id")
            stable_value = stable_uuid(f"calorie-agent:pending:{key}:user_settings:")
            fields = {_pending_field(k): v for k, v in {
                "id": stable_value, "user_id": user, "chat_id": "", "source_message_id": key,
                "intent": "user_settings", "food_name": "", "meal_type": "unknown", "raw_text": "",
                "payload": _encode_pending_payload(row), "status": "completed", "expires_at": 0,
            }.items()}
        else:
            raise ArchiveBlocked("unknown archive kind: " + kind)
        if not store.app_token or not table:
            raise ArchiveBlocked("Bitable app/table is not configured for " + kind)
        # New preparations identify the destination and exact committed version.
        # Persisted inflight requests retain their original token on recovery.
        token_identity = json.dumps(["calorie-agent:archive:v2", store.app_token, table, kind, identity, revision],
                                    ensure_ascii=False, separators=(",", ":"))
        return {"kind": kind, "id": identity, "revision": revision, "table": table,
                "stable_field": stable_field, "stable_value": stable_value, "known_record_id": known,
                "fields": fields, "client_token": stable_uuid(token_identity)}

    def _find(self, request):
        if request["known_record_id"]:
            url = _record_url(self.store.app_token, request["table"]) + "/" + urllib.parse.quote(request["known_record_id"], safe="")
            response = feishu_client.http_json_get(url, {"Authorization": "Bearer " + feishu_client.get_tenant_access_token()})
            if response.get("code") != 0:
                raise RuntimeError("imported archive row is unavailable: " + str({"code": response.get("code")}))
            record = response["data"]["record"]
            observed_id = extract_text(record["fields"].get(request["stable_field"]))
            if observed_id and observed_id != request["stable_value"]:
                raise ArchiveBlocked("imported archive row has a different stable identity")
            return record["record_id"], record["fields"]
        formula = "CurrentValue.[" + request["stable_field"] + "]=" + json.dumps(request["stable_value"], ensure_ascii=False)
        rows = [(rid, fields) for rid, fields in _list_records(self.store.app_token, request["table"], filter_formula=formula)
                if extract_text(fields.get(request["stable_field"])) == request["stable_value"]]
        if len(rows) > 1:
            raise ArchiveBlocked("duplicate archive business identity: " + request["id"])
        return rows[0] if rows else None

    def apply_and_readback(self, request):
        existing = self._find(request)
        if existing and request["kind"] == "intake":
            version = _archive_number(existing[1].get("agent_revision"))
            # Revisions are integers; malformed/fractional cells cannot prove
            # that a newer committed version superseded this request.
            if version is not None and version == version.to_integral_value() and version > request["revision"]:
                return {"record_id": existing[0], "superseded_by": int(version)}
        if not existing:
            # The request and token are already saved by archive_pending.
            self.store._batch_create(request["table"], [{"fields": request["fields"]}], client_token=request["client_token"])
        elif not _same_fields(existing[1], request["fields"]):
            self.store._update_record(request["table"], existing[0], request["fields"])
        confirmed = self._find(request)
        if not confirmed or not _same_fields(confirmed[1], request["fields"]):
            raise RuntimeError("archive readback has not confirmed the requested version")
        return {"record_id": confirmed[0], "revision": request["revision"]}


def archive_pending(store=None, *, gateway=None, progress=None):
    """Read committed facts only; the sync writer cannot mutate message state."""
    if store is None:
        from .cos_store import CosStore
        store = CosStore(readonly=True)
    if progress is None:
        from .cos_journal import Journal
        progress = Journal(store.objects, store.prefix.rstrip("/") + "/sync/")
    gateway = gateway or BitableArchive()
    scan = progress.get('archive_scan', {})
    generation = store.prefix
    water = scan.get('revision', -1)
    if scan and scan.get('generation') != generation:
        raise RuntimeError('archive cursor requires generation conversion')
    discovered = {}
    for revision in range(water + 1, store.journal.revision + 1):
        for key in store.journal._read(revision)['changes']:
            if key.startswith('archive:'):
                discovered['archive_pending:' + key] = store.journal.get(key)
    # Discovery and watermark publish together. Inflight owns the original
    # absolute request until independent readback resolves its uncertainty.
    progress.commit({**discovered, 'archive_scan':{'generation':generation, 'revision':store.journal.revision}})
    changes = {key.removeprefix('archive_pending:'):progress.get(key) for key in progress.keys('archive_pending:')}
    completed, failed = [], []
    keys = sorted(changes)
    for key in keys:
        state = deepcopy(progress.get("item:" + key, {}))
        if state.get("status") == "blocked":
            failed.append(key)
            continue
        latest = changes.get(key)
        try:
            while state.get("inflight") or (latest and latest["revision"] > state.get("ack_revision", -1)):
                request = state.get("inflight")
                if request is None:
                    request = gateway.prepare(latest)
                    state.update(inflight=request, status="pending", last_error="")
                    progress.commit({"item:" + key: state})
                result = gateway.apply_and_readback(request)
                state.update(ack_revision=request["revision"], inflight=None, status="confirmed",
                             record_id=result["record_id"], confirmed_at=time.time(), last_error="")
                progress.commit({"item:" + key: state})
                completed.append({"key": key, "revision": request["revision"], **result})
                print(json.dumps({"event": "archive_confirmed", **completed[-1]}, ensure_ascii=False))
            if latest and state.get('ack_revision', -1) >= latest['revision'] and not state.get('inflight'):
                progress.commit({'archive_pending:' + key:None})
        except Exception as exc:
            state.update(status="blocked" if _blocked_error(exc) else "retry", last_error=f"{type(exc).__name__}:{exc}"[:500], failed_at=time.time())
            progress.commit({"item:" + key: state})
            failed.append(key)
            print(json.dumps({"event": "archive_failed", "key": key, "status": state["status"],
                              "ack_revision": state.get("ack_revision", -1),
                              "inflight_revision": (state.get("inflight") or {}).get("revision"),
                              "error": state["last_error"]}, ensure_ascii=False))
    return {"confirmed": completed, "failed": failed, "sync_revision": progress.revision}


def handler(event, context=None):
    # Maintenance owns recovery dispatch and runs before the independent archive.
    from app import maintenance_handler
    return maintenance_handler(event, context)
