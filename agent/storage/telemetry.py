"""Request-local timings; one aggregate is emitted with the existing trace."""
from contextlib import contextmanager
from contextvars import ContextVar
import json
import time
import uuid
from functools import wraps

PARENT = ContextVar("latency_parent",default=None)
MESSAGE_ID = ContextVar("latency_message_id",default=None)
EVENTS = ContextVar("latency_events", default=None)
INSTANCE_ID = uuid.uuid4().hex


def log_receipt_latency(receipt, state, revision):
    received = receipt.get("received_at")
    delivery = state["delivery"]
    events = EVENTS.get() or []
    manifests = [event for event in events if event["kind"] == "cos_create"
                 and "/message/commits/" in event.get("key", "")]
    print(json.dumps({"event": "latency_receipt_commit", "receipt_id": receipt["receipt_id"],
        "message_id": receipt["message"]["message_id"], "instance_id": INSTANCE_ID,
        "revision": revision, "model_calls": state["result"].get("model_calls"),
        "model_http_ms": round(sum(e["duration_ms"] for e in events if e["kind"] == "model_http"), 3),
        "worker_from_accept_ms": round((state["worker_started_at"] - received) * 1000, 3) if received is not None else None,
        "sent_from_accept_ms": delivery["t_service_ms"], "send_ms": delivery["send_duration_ms"],
        "confirmation_ms": round((time.time() - delivery["sent_at"]) * 1000, 3),
        "durable_from_accept_ms": round((time.time() - received) * 1000, 3) if received is not None else None,
        "manifests_before_send": sum(e["started_epoch_ms"] < delivery["sent_at"] * 1000 for e in manifests),
        "manifests_after_send": sum(e["started_epoch_ms"] >= delivery["sent_at"] * 1000 for e in manifests)}))

@contextmanager
def span(kind, **fields):
    started = time.monotonic()
    epoch_ms = time.time()*1000
    ident = uuid.uuid4().hex[:12]
    parent = PARENT.get()
    token = PARENT.set(ident)
    outcome = "ok"
    try:
        yield fields
    except BaseException:
        outcome = "error"
        raise
    finally:
        PARENT.reset(token)
        events = EVENTS.get()
        if events is not None:
            events.append({"kind": kind, "id":ident, "parent":parent, "instance_id":INSTANCE_ID,
                           "message_id":MESSAGE_ID.get(),
                           "started_epoch_ms":epoch_ms, "started_monotonic": started,
                           "duration_ms": round((time.monotonic()-started)*1000, 3),
                           "outcome": outcome, **fields})

def message_scope(fn):
    @wraps(fn)
    def wrapped(message,*args,**kwargs):
        events_token = EVENTS.set([]) if EVENTS.get() is None else None
        token = MESSAGE_ID.set(message.get("message_id"))
        try:
            with span(fn.__name__): return fn(message,*args,**kwargs)
        finally:
            MESSAGE_ID.reset(token)
            if events_token is not None: EVENTS.reset(events_token)
    return wrapped
