"""Section 6.1 real-model acceptance via normal CLI ingress and COS storage.

Uses the saved R10 state, never re-runs planning or specifies model tool choices.
Cloud credentials/model configuration are supplied by the operator's environment.
This runner creates isolated COS data; it never deploys or sends Feishu messages.
"""
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

from agent.config import load_config
from agent.storage.cos_store import CosStore
from agent.storage.cos_transport import CosObjects
from agent.storage.execution_pipeline import Pipeline
from agent.storage import conversation

PROMPTS = (
    "记录今天早餐：米饭100克、鸡蛋100克，均为可食克重。",
    "把刚才早餐的米饭改为50克。",
    "我今天一共吃了多少千卡？",
    "刚才那份早餐记上了吗？",
)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def event(receipt):
    return {"receipt_id": receipt["receipt_id"], "ingress_revision": receipt["ingress_revision"],
            **{k: receipt["message"][k] for k in ("message_id", "user_id", "chat_id")}}


def accept_chain(objects, prefix, report, save):
    if not prefix.startswith("cos-probe/") or len(prefix.rstrip("/").split("/")) < 3:
        raise ValueError("a dedicated cos-probe/.../.../ prefix is required")
    if objects.list(prefix):
        raise ValueError("acceptance prefix must be empty; preserve earlier runs")
    source_path = ROOT / "reports/menu-candidate-evidence-r10-20261104.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    prior = source["candidate"]["requests"][1]  # Delivered R10 replacement, before failed record.
    user, chat = source["manifest"]["user_id"], "cli:same-breakfast-acceptance"
    rows = source["frozen_before_original_prompt"]["ledger"]["items"]
    assert len(rows) == 2 and sum(r["nutrition"]["kcal"] for r in rows) == 255
    assert prior["raw_intake"]["items"] == rows
    store = CosStore(objects, prefix)
    # Restore exact business rows; do not create a replacement breakfast via tools.
    def seed():
        store._save({"source:foods": prior["food_sources_after"], "source:aliases": {},
                     **{"food:" + f["food_id"]: f for f in prior["food_sources_after"]},
                     **{"item:" + r["record_id"]: r for r in rows},
                     "day:" + user + ":" + rows[0]["date"]: [r["record_id"] for r in rows]})
        store.save_session(user, chat, prior["session_after"])
        settings = deepcopy(prior["settings_after"])
        settings.pop("feishu_chat_id", None)
        store.save_document("user_settings", user, "", settings)
        context = conversation.context(store, user, chat, prior["session_after"], write=True)
        for turn in context["turns"]:
            turn["complete"] = True  # Both historical responses were actually delivered in R10.
        store.save_document("conversation_context", user, chat, context)
        return {"seeded": True}
    store.commit_action("acceptance-seed", seed)
    def read():
        reader = CosStore(objects, prefix, readonly=True)
        return sorted(reader.intake_items(user, rows[0]["date"]), key=lambda r: r["record_id"])
    baseline = read()
    assert baseline == sorted(rows, key=lambda r: r["record_id"])
    report.update(source={"path": str(source_path.relative_to(ROOT)),
                          "sha256": hashlib.sha256(source_path.read_bytes()).hexdigest()},
                  prefix=prefix, user_id=user, chat_id=chat, business_date=rows[0]["date"],
                  baseline=baseline, history_sha256=digest(prior["session_after"]),
                  config=load_config().as_dict(), entry="local Pipeline.accept/worker, source=cli",
                  deployment_performed=False, feishu_delivery=False, requests=[])
    save()
    pipeline = Pipeline(objects, prefix)
    old = {r["record_id"]: r for r in baseline}
    new = {}
    previous = baseline
    for index, text in enumerate(PROMPTS):
        item = {"message_id": prefix.rstrip("/").split("/")[-1] + ":" + str(index + 1),
                "user": text, "before": previous}
        report["requests"].append(item)
        save()
        receipt = pipeline.accept({"message_id": item["message_id"], "user_id": user,
                                   "chat_id": chat, "text": text}, source="cli")
        item["receipt"] = receipt
        assert receipt["intake_date"] == report["business_date"], "restore on the original R10 business date"
        with contextlib.redirect_stdout(io.StringIO()):
            state = pipeline.worker(event(receipt))  # Production DeepSeekModel, unrestricted native tools.
        item["state"] = state
        item["execution"] = CosStore(objects, prefix, readonly=True).load_execution(receipt["message"])
        result = state["result"]
        # Display the saved actual reply before acknowledging it through normal CLI presentation.
        print(result["reply"], flush=True)
        item["displayed_at"] = time.time()
        reference = {"receipt_id": receipt["receipt_id"], "uuid": state["delivery"]["uuid"],
                     "result_digest": state["delivery"]["result_digest"], "presented_at": item["displayed_at"]}
        presented = pipeline.accept({"message_id": item["message_id"] + ":presented",
                                    "user_id": user, "chat_id": chat, "text": ""},
                                   source="cli", kind="presented", reference=reference)
        pipeline.worker(event(presented))
        current = read()  # Separate Store/Journal, never the worker's cached business result.
        item.update(after=current, actual_reply=result["reply"],
                    local_received_to_display_ms=round((item["displayed_at"] - receipt["received_at"]) * 1000, 3))
        save()  # Preserve failed output and ledger before any assertions.
        assert result["status"] == "ok", result.get("errors")
        actual = {r["record_id"]: r for r in current}
        assert all(actual[k] == v for k, v in old.items()), "old breakfast changed"
        assert len(current) == 4 and sum(r["nutrition"]["kcal"] for r in current) == (510 if index == 0 else 452)
        if index == 0:
            new = {k: v for k, v in actual.items() if k not in old}
            assert len(new) == 2 and {r["grams"] for r in new.values()} == {100}
            assert len({r["snapshot"]["meal_id"] for r in new.values()}) == 1
            assert {r["snapshot"]["meal_id"] for r in new.values()}.isdisjoint(r["snapshot"]["meal_id"] for r in old.values())
        elif index == 1:
            assert set(actual) == set(old) | set(new)
            for key, row in new.items():
                if row["food_name"] == "米饭":
                    assert actual[key]["grams"] == 50 and actual[key]["agent_revision"] == row["agent_revision"] + 1
                    assert actual[key]["nutrition"] == {"carbs": 13, "protein": 1.3, "fat": 0.2, "kcal": 58}
                    mutable = {"grams", "nutrition", "agent_revision"}
                    assert {k: v for k, v in actual[key].items() if k not in mutable} == {k: v for k, v in row.items() if k not in mutable}
                else:
                    assert actual[key] == row
        else:
            assert current == previous, "query changed ledger or versions"
        expected = (255, 197, 452, 197)[index]
        assert re.search(rf"(?<!\d){expected}(?:\.0)?\s*千卡", result["reply"]), result["reply"]
        if index < 3:
            assert result["reply_source"] == "tool_facts" and result["model_reply"] == "", "mainline needs the committed fact receipt"
        item["passed"] = True
        previous = current
        save()
    report["mainline_and_old_ledger_check_passed"] = True
    save()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    # Exclusive create prevents accidental loss of actual failed evidence.
    with args.output.open("x", encoding="utf-8") as stream:
        report = {"schema": "same-breakfast-acceptance.v1", "started_at": time.time()}
        def save():
            stream.seek(0)
            json.dump(report, stream, ensure_ascii=False, indent=2)
            stream.truncate()
            stream.flush()
        try:
            accept_chain(CosObjects(), args.prefix, report, save)
        except Exception as exc:
            report["failure"] = {"type": type(exc).__name__, "message": str(exc)}
            raise
        finally:
            report["finished_at"] = time.time()
            save()


if __name__ == "__main__":
    main()
