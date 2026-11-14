"""SCF minute entry: fixed facts, stable UUID and durable delivery receipt. No LLM."""
from datetime import datetime, timezone, timedelta
import json
from .bitable_adapter import stable_uuid, reply_uuid
from . import feishu_client
from ..config import load_config
from ..policy import resolve_preset

MEALS = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐", "snack": "加餐"}

def _save_delivery(store, user, key, receipt, phase):
    def save():
        store.save_document('reminder_receipt',user,key,receipt)
        active=store.load_document('reminder_delivery_active','system')
        ident=stable_uuid(user+':'+key)
        if receipt.get('sent') or receipt.get('status')=='cancelled':active.pop(ident,None)
        else:active[ident]={'user_id':user,'key':key}
        store.save_document('reminder_delivery_active','system','',active)
        return {'status':receipt.get('status','pending')}
    store.commit_action('reminder:'+user+':'+key+':'+phase,save)


def _deliver(store,user,key,receipt,send,manual=False):
    if receipt.get('sent'):return 0
    attempts=receipt.get('attempts')
    # Legacy pending records have no reliable remaining automatic allowance.
    if (attempts is None or attempts>=3) and not manual:return 0
    attempt=(attempts or 0)+1
    receipt.update(attempts=attempt,status='sending')
    _save_delivery(store,user,key,receipt,'attempt:'+str(attempt))
    try:
        result=send(receipt['chat_id'],receipt['reply'],uuid_key=receipt['uuid'])
    except Exception as exc:
        receipt.update(status='needs_attention' if attempt>=3 else 'retry',error=type(exc).__name__)
        _save_delivery(store,user,key,receipt,'failure:'+str(attempt))
        raise
    receipt.update(sent=True,status='completed',send_result=result,completed_at=datetime.now(timezone.utc).timestamp())
    _save_delivery(store,user,key,receipt,'sent:'+str(attempt))
    return 1


def _eligible(store,user,rule,day,chat):
    settings=store.load_document('user_settings',user)
    current=settings.get('reminders',{}).get(rule['rule_id'],{})
    if settings.get('feishu_chat_id')!=chat or not current.get('enabled'):return False
    if any(current.get(field)!=rule.get(field) for field in ('time','meal_type')):return False
    date=datetime.fromisoformat(day).date()
    if date.weekday() not in current.get('weekdays',list(range(7))):return False
    rows=store.intake_items(user,day,(date+timedelta(days=1)).isoformat())
    return not any(row['meal_type']==rule['meal_type'] for row in rows)


def check_reminders(store, scheduled_time, send=None, *, manual=False):
    send = send or feishu_client.send_text
    local = scheduled_time.astimezone(timezone(timedelta(hours=getattr(store, 'offset_hours', 8))))
    day, minute = local.date().isoformat(), local.strftime('%H:%M')
    sent,failed = 0,[]
    for doc in store.list_documents('user_settings'):
        user,settings=doc['user_id'],doc['payload'];chat=settings.get('feishu_chat_id')
        if not chat:continue
        for rule_id,raw in settings.get('reminders',{}).items():
            rule={**raw,'rule_id':rule_id}
            if rule.get('time')!=minute or not _eligible(store,user,rule,day,chat):continue
            key=f'reminder:{rule_id}:{day}'
            receipt=store.load_document('reminder_receipt',user,key)
            if not receipt:
                message={'message_id':key,'chat_id':chat,'user_id':user,'text':''}
                receipt={'chat_id':chat,'reply':f"今天还没有{MEALS[rule['meal_type']]}记录，吃过后可以告诉我。",
                    'uuid':reply_uuid(message),'sent':False,'attempts':0,'status':'pending',
                    'day':day,'rule':rule,'scheduled_time':scheduled_time.isoformat()}
                _save_delivery(store,user,key,receipt,'prepare')
            try:sent+=_deliver(store,user,key,receipt,send,manual)
            except Exception as exc:failed.append(exc)
    if failed:raise failed[0]
    return {'scheduled_minute':local.isoformat(timespec='minutes'),'sent':sent}


def recover_deliveries(store,send=None,*,manual=False):
    """Retry only fixed prepared deliveries; one failure cannot starve another."""
    send=send or feishu_client.send_text
    sent,failed=0,0
    active=store.load_document('reminder_delivery_active','system')
    for entry in list(active.values()):
        user,key=entry['user_id'],entry['key']
        receipt=store.load_document('reminder_receipt',user,key)
        if not receipt or not receipt.get('rule') or not receipt.get('day'):continue
        if not _eligible(store,user,receipt['rule'],receipt['day'],receipt['chat_id']):
            receipt.update(status='cancelled')
            _save_delivery(store,user,key,receipt,'cancel');continue
        try:sent+=_deliver(store,user,key,receipt,send,manual)
        except Exception:failed+=1
    return {'sent':sent,'failed':failed}


def catch_up(store,paused_at,now,send=None):
    """Check original planned minutes, bounded to the latest fifteen minutes."""
    start=max(paused_at,now-15*60)
    stamp=int(start//60)*60
    sent,failed=0,0
    while stamp<=now:
        if now-stamp<=15*60:
            try:sent+=check_reminders(store,datetime.fromtimestamp(stamp,timezone.utc),send)['sent']
            except Exception:failed+=1
        stamp+=60
    return {'sent':sent,'failed':failed}


def reminder_intake(scheduled):
    """Canonical fixed timer identity, independent of delayed invocation time."""
    if not scheduled:
        raise ValueError("SCF timer scheduled Time is required")
    stamp = datetime.fromisoformat(str(scheduled).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("SCF timer scheduled Time must include timezone")
    scheduled = stamp.astimezone(timezone.utc).isoformat(timespec="seconds")
    return {"message_id": "reminder_tick:" + scheduled, "user_id": "system",
            "chat_id": "system:reminders", "text": ""}, {"scheduled_time": scheduled}


def handler(event, context=None):
    config = load_config()
    preset = resolve_preset(config.preset, config.allowed_tools)
    if not preset.send_reply:
        return {"sent": 0, "reason": "preset_does_not_send"}
    # Tencent timer Time is the scheduled event timestamp, not the delayed execution clock.
    _, reference = reminder_intake(event.get("Time") or event.get("time"))
    from .live_client import LiveClient
    result = LiveClient().submit_reminder(reference['scheduled_time'])
    print(json.dumps({"event": "reminder_accepted", **result}, ensure_ascii=False))
    return result
