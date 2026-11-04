"""Section 6.1 A/B with the saved R11 scene, real model and normal CLI ingress."""
from copy import deepcopy
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent.storage.cos_store import CosStore
from agent.storage.cos_transport import CosObjects
from agent.storage.execution_pipeline import Pipeline
from agent.storage import conversation
from harness.eval.production_live import RawCommits

RECORD = "记录今天早餐：米饭100克、鸡蛋100克，均为可食克重。"
MIXED = RECORD + "顺便把上次菜单里的土豆泥改为300克，其他照旧。"


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def restore_seed(objects, prefix, chat):
    if not prefix.startswith("cos-probe/") or objects.list(prefix):
        raise ValueError("a new, empty isolated cos-probe prefix is required")
    reference = json.loads((ROOT / "reports/r11-cloud-ledger-20261104.json").read_text(encoding="utf-8"))["raw_evidence"]
    path = Path(reference["path"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == reference["sha256"]
    saved = json.loads(path.read_text(encoding="utf-8"))
    seed = deepcopy(saved["seed"])
    user = seed["isolated_rows"][0]["user_id"]
    foods = deepcopy(saved["candidate"]["requests"][0]["food_sources_after"])
    assert len(foods) == seed["food_sources"] == 125
    # Restore actual persisted R10 delivery receipts for the historical plan and revision.
    # No delivery wording or complete=True flag is added to the historical messages.
    r10 = json.loads((ROOT / "reports/menu-candidate-evidence-r10-20261104.json").read_text(encoding="utf-8"))
    deliveries = r10["candidate"]["requests"][:2]
    assert seed["session"] == r10["candidate"]["requests"][1]["session_after"]
    assert {f["food_id"]: f for f in foods} == {f["food_id"]: f for f in r10["candidate"]["requests"][1]["food_sources_after"]}
    store = CosStore(objects, prefix)
    def restore():
        store._save({"source:foods": foods, "source:aliases": {},
                     **{"food:" + f["food_id"]: f for f in foods}})
        for row in seed["isolated_rows"]:
            store._put_row(row)
        for delivery in deliveries:
            state = delivery["receipt_state"]
            assert state["status"] == "completed" and state["delivery"]["presented"]
            store.save_document("receipt_state", "system", delivery["receipt_id"], state)
        store.save_session(user, chat, seed["session"])
        settings = deepcopy(seed["settings"])
        settings.pop("feishu_chat_id", None)
        store.save_document("user_settings", user, "", settings)
        conversation.context(store, user, chat, seed["session"], write=True)
        return {"restored": True}
    store.commit_action("restore-r11-history-boundary", restore)
    raw = RawCommits(objects, prefix + "message/")
    baseline = raw.items(user)
    assert sorted(baseline["items"], key=lambda r: r["record_id"]) == sorted(seed["isolated_rows"], key=lambda r: r["record_id"])
    return {"user_id": user, "chat_id": chat, "baseline": baseline,
            "draft": seed["session"]["meal_draft"], "settings": seed["settings"],
            "source_archive": reference, "restored_delivery_receipts": [
                {"message_id": d["message_id"], "receipt_id": d["receipt_id"], "state_sha256": digest(d["receipt_state"]),
                 "presented": d["receipt_state"]["delivery"]["presented"]} for d in deliveries]}


def event(receipt):
    return {"receipt_id": receipt["receipt_id"], "ingress_revision": receipt["ingress_revision"],
            **{k: receipt["message"][k] for k in ("message_id", "user_id", "chat_id")}}


def run_contrast(objects, prefix, variant, report, save):
    seed = restore_seed(objects, prefix, "cli:history-boundary")
    report.update(seed=seed, prefix=prefix, variant=variant)
    pipeline = Pipeline(objects, prefix)
    user, chat = seed["user_id"], seed["chat_id"]
    source_store = pipeline.store()
    context = source_store.load_document("conversation_context", user, chat)
    old_sources = {t["id"]: source_store.load_document("dialogue_turn", user, t["id"]) for t in context["turns"]}
    # Unknown legacy status is checked through exactly the same projection, without a model call.
    actual_status = conversation.history(source_store, user, chat, for_model=True)[0]
    report["before_model_history"] = [m.model_dump() for m in actual_status]
    assert all(json.loads(m.content.split("：", 1)[1])["reply_status"] == "已回复" for m in actual_status if m.additional_kwargs.get("history_turn_id"))
    text = RECORD if variant == "A" else MIXED
    message = {"message_id": prefix.rstrip("/").split("/")[-1] + ":" + variant,
               "user_id": user, "chat_id": chat, "text": text}
    report["message"] = message
    save()
    receipt = pipeline.accept(message, source="cli")
    report["receipt"] = receipt
    assert receipt["intake_date"] == seed["baseline"]["items"][0]["date"]
    with contextlib.redirect_stdout(io.StringIO()):
        state = pipeline.worker(event(receipt))
    report["state"] = state
    reply = state["result"]["reply"]
    print(reply, flush=True)
    report["displayed_at"] = time.time()
    ack = pipeline.accept({**message, "message_id": message["message_id"] + ":presented", "text": ""},
        source="cli", kind="presented", reference={"receipt_id": receipt["receipt_id"],
        "uuid": state["delivery"]["uuid"], "result_digest": state["delivery"]["result_digest"], "presented_at": report["displayed_at"]})
    report["presentation_state"] = pipeline.worker(event(ack))
    reader = CosStore(objects, prefix, readonly=True)
    raw = RawCommits(objects, prefix + "message/")
    revision, index = raw.snapshot()
    report["after"] = raw.items(user)
    # Decode the saved session independently, not from the model's projected draft.
    from agent.storage.bitable_adapter import stable_uuid
    session_key = "document:" + stable_uuid(f"agent_session:{user}:{chat}")
    session = raw.document(index, session_key)["payload"]
    report["session_after"] = session
    execution = reader.load_execution(receipt["message"])
    report["execution"] = execution
    save()
    result = state["result"]
    assert result["status"] == "ok" and not result["errors"]
    original = {r["record_id"]: r for r in seed["baseline"]["items"]}
    rows = report["after"]["items"]
    current = {r["record_id"]: r for r in rows}
    assert len(rows) == 4 and sum(r["nutrition"]["kcal"] for r in rows) == 510
    assert all(current[k] == v for k, v in original.items())
    assert len(current.keys() - original.keys()) == 2
    assert re.search(r"(?<!\d)255(?:\.0)?\s*千卡", reply)
    for ident, saved in old_sources.items():
        assert reader.load_document("dialogue_turn", user, ident) == saved
    for step in execution["payload"]["model_steps"]:
        body = step["request_body"]
        assert [m["content"] for m in body["messages"] if m["role"] == "user"] == [text]
        groups = [json.loads(m["content"].split("：", 1)[1]) for m in body["messages"] if m["content"].startswith("历史轮次背景（")]
        assert len(groups) == 2 and all(g["reply_status"] == "已回复" for g in groups)
        assert any(g["dialogue"][0]["content"] == "把鸡胸换成鸡蛋" for g in groups)
    if variant == "A":
        assert session["meal_draft"] == seed["draft"]
        assert not any(word in reply for word in ("鸡胸", "土豆", "菜单", "2143"))
    else:
        before = {i["item_id"]: i for i in seed["draft"]["items"]}
        after = {i["item_id"]: i for i in session["meal_draft"]["items"]}
        potato = next(k for k, i in before.items() if i.get("food_query") == "土豆泥")
        assert before.keys() == after.keys() and before[potato]["grams"] == 400 and after[potato]["grams"] == 300
        assert all(before[k] == after[k] for k in before if k != potato)
        assert seed["draft"]["constraints"] == session["meal_draft"]["constraints"]
        assert session["meal_draft"]["draft_id"] != seed["draft"]["draft_id"]
        assert len(re.findall(r"^- 土豆泥[^\n]*300克", reply, re.M)) == 1
        assert reply.count("日常完整菜单（") == 1
    report["passed"] = True
    save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--variant", choices=("A", "B"), required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.output.open("x", encoding="utf-8") as stream:
        report = {"schema": "history-boundary-contrast.v1", "started_at": time.time()}
        def save():
            stream.seek(0); json.dump(report, stream, ensure_ascii=False, indent=2); stream.truncate(); stream.flush()
        try:
            run_contrast(CosObjects(), args.prefix, args.variant, report, save)
        except Exception as exc:
            report["failure"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            report["finished_at"] = time.time(); save()


if __name__ == "__main__":
    main()
