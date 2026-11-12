"""Resumable backfill of confirmed delivery sources, run by the message writer."""
from copy import deepcopy

from . import memory
from .bitable_adapter import message_key


def _old_draft(store, user, message, draft_id, source):
    staged = source.get("draft", {})
    if staged.get("draft_id") == draft_id:
        return staged
    execution = store.load_execution(message) or {}
    session = execution.get("payload", {}).get("session", {})
    for value in (session, store.load_session(user, message["chat_id"])):
        draft = value.get("meal_draft_candidate") or value.get("meal_draft", {})
        if draft.get("draft_id") != draft_id or draft.get("message_id") != message["message_id"]:
            continue
        draft = deepcopy(draft)
        foods = value.get("food_sources", {})
        draft.setdefault("food_sources", {part["source_ref"]: foods[part["source_ref"]]
                                           for part in draft.get("items", []) if part.get("source_ref") in foods})
        return draft
    return None


def backfill_user(store, inbox, user_id, limit=25):
    if not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError("backfill page size must be 1–100")
    user = store.load_user_aliases().get(user_id, user_id)
    prior = store.load_document("memory_backfill", user)
    if prior.get("done"):
        return prior
    watermark = prior.get("watermark", inbox.get("next_sequence", 0))
    keys = sorted(key for key in inbox.state if key.startswith("receipt:") and key > prior.get("cursor", ""))
    page = keys[:limit]
    cursor = page[-1] if page else prior.get("cursor", "")

    def commit():
        progress = {**prior, "watermark": watermark, "cursor": cursor, "done": len(keys) <= limit}
        missing = progress.setdefault("uncovered", {})
        missing_refs = progress.setdefault("uncovered_refs", {})
        for key in page:
            receipt = inbox.get(key)
            message = receipt["message"]
            owner = store.load_user_aliases().get(message["user_id"], message["user_id"])
            if owner != user or receipt.get("sequence", 0) > watermark or receipt.get("kind") != "message":
                continue
            delivered = store.load_document("receipt_state", "system", receipt["receipt_id"])
            if not delivered.get("delivery", {}).get("presented"):
                if delivered.get("migrated"):
                    missing[receipt["receipt_id"]] = "delivery_not_verifiable"
                continue
            turn_id = "calorie-user:" + message_key(message)
            turn_ref = memory.reference(user, "turn", turn_id)
            source = store.load_document("dialogue_turn", user, turn_id)
            if not source.get("messages"):
                missing[receipt["receipt_id"]] = "clean_source_missing"
                missing_refs[turn_ref] = "clean_source_missing"
                continue
            if memory.get(store, user, turn_ref)[1] == "not_found":
                memory.delivered_turn(store, user, message["chat_id"], turn_id,
                    {**source, "date": source.get("date") or receipt.get("intake_date", ""),
                     "received_at": source.get("received_at") or receipt.get("received_at")}, register=False)
                progress["indexed"] = progress.get("indexed", 0) + 1
            draft_id = delivered.get("result", {}).get("presented_candidates", {}).get("meal_draft_candidate")
            if draft_id and memory.get(store, user, memory.reference(user, "draft", draft_id))[1] == "not_found":
                draft = _old_draft(store, user, message, draft_id, source)
                if draft:
                    memory.delivered_draft(store, user, message["chat_id"], turn_id, draft, register=False)
                else:
                    missing[receipt["receipt_id"]] = "structured_draft_missing"
                    missing_refs[memory.reference(user, "draft", draft_id)] = "structured_draft_missing"
        catalog = store.load_document("memory_index", user)
        catalog["coverage"] = {"complete": progress["done"] and not missing,
                               "through_ingress_sequence": watermark, "missing": len(missing)}
        store.save_document("memory_index", user, "", catalog)
        store.save_document("memory_backfill", user, "", progress)
        return progress

    return store.commit_action("memory-backfill:" + memory.identity(user, "turn", [watermark, prior.get("cursor", ""), cursor]), commit)
