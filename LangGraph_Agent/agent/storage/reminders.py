"""SCF minute entry: fixed facts, stable UUID and durable delivery receipt. No LLM."""
from datetime import datetime, timezone, timedelta
import json
from .bitable_adapter import BitableStore, stable_uuid, reply_uuid
from . import feishu_client
from ..config import load_config
from ..policy import resolve_preset

MEALS = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐", "snack": "加餐"}

def check_reminders(store, scheduled_time, send=None):
    send = send or feishu_client.send_text
    local = scheduled_time.astimezone(timezone(timedelta(hours=getattr(store, "offset_hours", 8))))
    day, minute = local.date().isoformat(), local.strftime("%H:%M")
    sent = 0
    for doc in store.list_documents("user_settings"):
        user_id, settings = doc["user_id"], doc["payload"]
        chat_id = settings.get("feishu_chat_id")
        if not chat_id:
            continue
        for rule_id,rule in settings.get("reminders", {}).items():
            if not rule.get("enabled") or rule.get("time") != minute or local.weekday() not in rule.get("weekdays", list(range(7))):
                continue
            rows = store.intake_items(user_id, day, (local.date()+timedelta(days=1)).isoformat())
            if any(r["meal_type"] == rule["meal_type"] for r in rows):
                continue
            key = f"reminder:{rule_id}:{day}"
            receipt = store.load_document("reminder_receipt", user_id, key)
            if receipt.get("sent"):
                continue
            message = {"message_id": key, "chat_id": chat_id, "user_id": user_id, "text": ""}
            if not receipt:
                receipt = {"chat_id": chat_id, "reply": f"今天还没有{MEALS[rule['meal_type']]}记录，吃过后可以告诉我。", "uuid": reply_uuid(message), "sent": False}
                store.save_document("reminder_receipt", user_id, key, receipt)
            result = send(receipt["chat_id"], receipt["reply"], uuid_key=receipt["uuid"])
            receipt.update(sent=True, send_result=result)
            store.save_document("reminder_receipt", user_id, key, receipt)
            sent += 1
    return {"scheduled_minute": local.isoformat(timespec="minutes"), "sent": sent}

def handler(event, context=None):
    config = load_config()
    preset = resolve_preset(config.preset, config.allowed_tools)
    if not preset.send_reply:
        return {"sent": 0, "reason": "preset_does_not_send"}
    # Tencent timer Time is the scheduled event timestamp, not the delayed execution clock.
    scheduled = event.get("Time") or event.get("time")
    if not scheduled:
        raise ValueError("SCF timer scheduled Time is required")
    stamp = datetime.fromisoformat(scheduled.replace("Z", "+00:00"))
    result = check_reminders(BitableStore(offset_hours=config.timezone_offset_hours), stamp)
    print(json.dumps({"event": "reminder_check", **result}, ensure_ascii=False))
    return result
