"""Real-model knowledge A/B from the archived R14 scene, through CLI intake.

Only retrieval is replayed, as permitted by experience spec section 6. Business
tools read the restored isolated COS state; the model chooses every native call.
"""
from copy import deepcopy
import argparse
import contextlib
import hashlib
import io
import json
from pathlib import Path
import re
import time
from unittest.mock import patch

from agent.storage.cos_store import CosStore
from agent.storage.cos_transport import CosObjects
from agent.storage.execution_pipeline import Pipeline
from agent.storage import conversation
from agent.storage.bitable_adapter import stable_uuid
from harness.eval.production_live import RawCommits
from harness.eval.history_boundary_acceptance import ROOT, event

TEXTS = {
    "A": "减脂时碳水化合物需要完全不吃吗？请查资料解释，并给出这次检索的原文依据和出处。",
    "B": "我今天设置的碳水目标是多少？请查资料说明，这个量适合我减脂吗？",
}


def restore(objects, prefix, chat):
    if not prefix.startswith("cos-probe/") or objects.list(prefix):
        raise ValueError("a fresh isolated cos-probe prefix is required")
    reference = json.loads((ROOT / "reports/history-boundary-cloud-20261104.json").read_text(encoding="utf-8"))["raw_evidence"]
    path = Path(reference["path"])
    assert hashlib.sha256(path.read_bytes()).hexdigest() == reference["sha256"]
    saved = json.loads(path.read_text(encoding="utf-8"))
    prior = saved["candidate"]["requests"][4]
    assert prior["step"] == "replace"
    user = saved["seed"]["isolated_rows"][0]["user_id"]
    rows, session = deepcopy(prior["raw_intake"]["items"]), deepcopy(prior["session_after"])
    settings = deepcopy(prior["settings_after"])
    settings.pop("feishu_chat_id", None)
    foods = deepcopy(prior["food_sources_after"])
    old = json.loads((ROOT / "reports/menu-candidate-evidence-r10-20261104.json").read_text(encoding="utf-8"))
    deliveries = [*old["candidate"]["requests"][:2], *saved["candidate"]["requests"][:5]]
    store = CosStore(objects, prefix)
    def apply():
        store._save({"source:foods": foods, "source:aliases": {}, **{"food:" + f["food_id"]: f for f in foods}})
        # This is snapshot restoration, not a new business write. _put_row starts
        # new identities at v1, so restore the saved v2 and day indexes verbatim.
        changes = {"item:" + row["record_id"]: row for row in rows}
        for row in rows:
            changes.setdefault("day:" + user + ":" + row["date"], []).append(row["record_id"])
        store._save(changes)
        for delivery in deliveries:
            state = delivery["receipt_state"]
            assert state["status"] == "completed" and state["delivery"]["presented"]
            store.save_document("receipt_state", "system", delivery["receipt_id"], state)
        store.save_session(user, chat, session)
        store.save_document("user_settings", user, "", settings)
        conversation.context(store, user, chat, session, write=True)
        return {"restored": True}
    store.commit_action("restore-r14-before-knowledge", apply)
    baseline = RawCommits(objects, prefix + "message/").items(user)
    assert sorted(baseline["items"], key=lambda r: r["record_id"]) == sorted(rows, key=lambda r: r["record_id"])
    return {"user_id": user, "chat_id": chat, "baseline": baseline, "session": session,
            "settings": settings, "source_archive": reference}, saved["candidate"]["requests"][5]["receipt_state"]["result"]["tool_results"]


def run(objects, prefix, variant, report, save):
    seed, facts = restore(objects, prefix, "cli:knowledge-scope")
    report.update(seed=seed, prefix=prefix, variant=variant, retrieval_mode="replayed R14 entries; real production model")
    retrievals = []
    def retrieve(query, entries):
        # Replay the actual returned entry batches, not an answer or a model plan.
        result = deepcopy(facts[min(len(retrievals), len(facts) - 1)]["data"])
        retrievals.append({"query": query, "returned": result})
        return result
    pipeline = Pipeline(objects, prefix)
    message = {"message_id": prefix.rstrip("/").split("/")[-1] + ":" + variant,
               "user_id": seed["user_id"], "chat_id": seed["chat_id"], "text": TEXTS[variant]}
    report["message"] = message
    receipt = pipeline.accept(message, source="cli")
    report["receipt"] = receipt
    assert receipt["intake_date"] in seed["settings"]["daily_targets"], "restore scene must match the real business date"
    with patch("agent.tools.reads.build_knowledge_retriever", return_value=retrieve), contextlib.redirect_stdout(io.StringIO()):
        state = pipeline.worker(event(receipt))
    report.update(state=state, retrievals=retrievals)
    save()
    reply = state["result"]["reply"]
    print(reply, flush=True)
    report["displayed_at"] = time.time()
    ack = pipeline.accept({**message, "message_id": message["message_id"] + ":presented", "text": ""},
        source="cli", kind="presented", reference={"receipt_id": receipt["receipt_id"],
            "uuid": state["delivery"]["uuid"], "result_digest": state["delivery"]["result_digest"], "presented_at": report["displayed_at"]})
    report["presentation_state"] = pipeline.worker(event(ack))
    raw = RawCommits(objects, prefix + "message/")
    _, index = raw.snapshot()
    session = raw.document(index, "document:" + stable_uuid(f"agent_session:{seed['user_id']}:{seed['chat_id']}"))["payload"]
    reader = CosStore(objects, prefix, readonly=True)
    report.update(after=raw.items(seed["user_id"]), session_after=session,
        settings_after=reader.load_document("user_settings", seed["user_id"]), execution=reader.load_execution(receipt["message"]))
    save()
    result = state["result"]
    assert result["status"] == "ok" and not result["errors"]
    assert not re.search(r"K\d{3,}", reply), "raw source identity leaked into receipt"
    assert report["after"]["items"] == seed["baseline"]["items"]
    assert report["settings_after"] == seed["settings"] and session["meal_draft"] == seed["session"]["meal_draft"]
    calls = result["native_calls"]
    assert all(c["tool"] in {"query_knowledge", "query_today"} for c in calls)
    assert "https://dg.cnsoc.org/article/04/J4-AsD_DR3OLQMnHG0-jZA.html" in reply
    if variant == "A":
        assert "知识库" in reply or "条目" in reply
        assert [c["tool"] for c in calls] == ["query_knowledge"]
        assert not re.search(r"(?<!\d)(?:180|250|1800|2230|150|70)(?!\d)", reply)
    else:
        current = [f for f in result["tool_results"] if f["tool"] == "query_today"]
        assert len(current) == 1 and current[0]["data"]["targets"]["carbs"] == 180
        assert any(c["tool"] == "query_today" and c["args"].get("detail") == "summary" for c in calls)
        assert re.search(r"180\s*(?:克|g)", reply, re.I)
        assert not re.search(r"(?<!\d)250(?!\d)", reply), "unqueried long-term goal leaked into today answer"
    report["structural_checks_passed"] = True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--variant", choices=TEXTS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with args.output.open("x", encoding="utf-8") as stream:
        report = {"schema": "knowledge-scope-contrast.v1", "started_at": time.time()}
        def save():
            stream.seek(0); json.dump(report, stream, ensure_ascii=False, indent=2); stream.truncate(); stream.flush()
        try:
            run(CosObjects(), args.prefix, args.variant, report, save)
        except Exception as exc:
            report["failure"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            report["finished_at"] = time.time(); save()


if __name__ == "__main__":
    main()
