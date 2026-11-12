from copy import deepcopy
import json
import os
from unittest.mock import patch

from agent.graph import mark_reply_presented, run
from agent.storage import experience, memory, memory_backfill
from agent.storage.fake_store import FakeStore
from harness.eval.runtime_cases import CONFIG, NativeFake, call
from langchain_core.messages import AIMessage


def send(store, message_id, chat, text, *responses, user="user", presented=True):
    result = run({"message_id": message_id, "user_id": user, "chat_id": chat, "text": text}, store=store,
                 config=CONFIG, model=NativeFake(responses=[*responses, AIMessage(content="已处理。")]))
    if presented:
        mark_reply_presented(store, user, chat, result)
    return result


def feedback_fixture():
    from harness.eval.stage_a_cases import first_feedback_fixture

    store, snapshot, text = first_feedback_fixture()
    ref = snapshot["materials"][0]["ref"]
    candidate = {"user_id": "user", "batch_id": "fixture-1", "base_revision": snapshot["base_revision"],
        "source_stamp": experience.source_stamp(snapshot),
        "materials": [ref], "attempts": 1, "status": "ready", "changes": [
            {"kind": "note", "key": "separate-packing", "status": "usable", "text": "带饭时把米饭和配菜分开打包。",
             "context": "带午饭并在用餐处加热", "supports": [ref], "refutes": [], "conditions": [],
             "quotes": [{"ref": ref, "text": text}]},
            {"kind": "theme", "key": "meal-prep", "status": "usable", "title": "带饭和加热",
             "text": "有带午饭时分开打包的反馈。", "links": [{"kind": "note", "key": "separate-packing"}]}],
        "entries": [{"key": "meal-prep"}]}
    return store, snapshot, candidate

def PM01():
    """反馈先交付后可复盘，跨用户读取拒绝，迟到候选不能复活撤回。"""
    store, snapshot, candidate = feedback_fixture()
    feedback = snapshot["materials"][0]
    with patch("agent.storage.experience.local_day", return_value=store.today):
        event = {"candidate": candidate}
        assert experience.apply_candidate(store, event, CONFIG)["status"] == "applied"
        assert experience.apply_candidate(store, event, CONFIG)["status"] == "applied"
    state = memory.state(store, "user")
    theme = state["entries"][0]
    note = memory.get(store, "user", theme)[0]["links"][0]
    assert memory.context_view(store, "user", store.today, "new", {})["themes"][0]["ref"] == theme
    assert memory.get(store, "other", note)[0] is None
    assert memory.get(store, "user", note)[0]["supports"] == [feedback["ref"]]
    suggestion = "带午饭并在用餐处加热，怎么备餐？"

    def adopt(messages):
        dynamic = next(m.content for m in messages if m.content.startswith("可信会话事实："))
        learned = json.loads(dynamic.split("：", 1)[1])["personal_memory"]["experiences"]
        assert learned[0]["ref"] == note
        return AIMessage(content=learned[0]["text"])

    next_turn = send(store, "apply-experience", "new-chat", suggestion, adopt)
    assert "分开打包" in next_turn["reply"] and next_turn["model_calls"] == 1
    from harness.eval.stage_a_cases import review_response
    later = "后来那次分开打包也很方便。"
    result = send(store, "feedback2", "A", later, AIMessage(content=review_response("收到。",
        {"ref": "current_turn", "reason": "新使用结果"})))
    late_ref = memory.reference("user", "turn", result["dialogue_turn_id"])
    late_snapshot = experience.snapshot(store, "user", [late_ref], store.today)
    late = deepcopy(candidate)
    late.update(batch_id="fixture-2", base_revision=state["experience_revision"],
                source_stamp=experience.source_stamp(late_snapshot), materials=[late_ref])
    send(store, "withdraw", "B", "忘掉刚才的带饭经验", call("update_user_settings", {"kind": "memory", "values": {"ref": note}}))
    with patch("agent.storage.experience.local_day", return_value=store.today):
        assert experience.apply_candidate(store, {"candidate": late}, CONFIG)["status"] == "obsolete"
    view = memory.context_view(store, "user", store.today, "after-withdrawal", {}, suggestion)
    assert not view.get("themes") and not view.get("experiences")
    assert not store.load_document("user_settings", "user") and not store.intake


def PM02():
    """跨入口读取已交付菜单，按本次日期改稿，撤回条件与设置后不再进入旧稿。"""
    store = FakeStore()
    store.today = "2026-11-12"
    quote = "这周出差只有微波炉"
    with patch.dict(os.environ, {"USER_ID_ALIASES": "feishu-user:user,cli-user:user"}):
        condition = send(store, "condition", "A", quote, call("update_user_settings", {"kind": "condition",
            "values": {"operation": "save", "quote": quote, "text": "只能使用微波炉", "period": "this_week"}}), user="feishu-user")
        ref = condition["tool_results"][0]["data"]["ref"]
        repeat = send(store, "repeat", "A", quote + "，顺便聊聊午餐", user="feishu-user")
        original = send(store, "original", "A", "安排当天菜单", call("calculate_meal", {"scope": "remaining",
            "condition_refs": [ref], "items": [{"item_id": "rice", "food_query": "米饭", "grams": 100},
                                                {"item_id": "egg", "food_query": "鸡蛋", "grams": 80}]}),
            user="feishu-user", presented=False)
        draft_id = original["presented_candidates"]["meal_draft_candidate"]
        draft_ref = memory.reference("user", "draft", draft_id)
        assert memory.get(store, "user", draft_ref)[0] is None
        mark_reply_presented(store, "feishu-user", "A", original)
        assert memory.get(store, "user", draft_ref)[0]["draft"]["food_sources"]
        other = send(store, "other", "B", "找出那份菜单", call("query_memory", {"operation": "read", "ref": draft_ref}), user="other")
        assert other["tool_results"][0]["status"] == "pending"
        store.today = "2026-11-16"
        revised = send(store, "revised", "B", "继续旧菜单，把米饭改为120克", call("query_memory", {"operation": "read", "ref": draft_ref}, "read"),
            call("calculate_meal", {"draft_ref": draft_ref, "replacements": [{"item_id": "rice", "grams": 120}], "finish_after_tools": True}, "calculate"), user="cli-user")
        plan = revised["tool_results"][-1]["data"]
        assert revised["reply_source"] == "tool_facts" and revised["model_calls"] == 2
        assert plan["scope"] == "remaining" and plan["date"] == "2026-11-16"
        assert [item["grams"] for item in plan["items"]] == [120, 80] and plan["constraints"] == []
        assert store.load_session("user", "A")["meal_draft"]["draft_id"] == draft_id and not store.intake
        send(store, "forget", "B", "忘掉微波炉限制", call("update_user_settings", {"kind": "memory", "values": {"ref": ref}}), user="cli-user")
        results = send(store, "read-after", "B", "看看历史菜单和当时的聊天", call("query_memory", {"operation": "read", "ref": draft_ref}, "draft"),
            call("query_memory", {"operation": "read", "ref": memory.reference("user", "turn", repeat["dialogue_turn_id"])}, "turn"), user="cli-user")
        history = json.dumps([fact["data"] for fact in results["tool_results"]], ensure_ascii=False)
        assert "只能使用微波炉" not in history and quote not in history and "顺便聊聊午餐" in history
        send(store, "setting", "A", "长期口味选清淡", call("update_user_settings", {"kind": "preferences", "values": {"taste": "清淡"}}))
        first = memory.reference("user", "setting", ["preferences", "", "taste"])
        send(store, "forget-setting", "B", "忘掉原口味", call("update_user_settings", {"kind": "memory", "values": {"ref": first}}))
        send(store, "new-setting", "B", "现在长期喜欢酸口", call("update_user_settings", {"kind": "preferences", "values": {"taste": "酸口"}}))
        second = memory.reference("user", "setting", ["preferences", "", "taste"], 2)
        assert memory.get(store, "user", first)[1] == "withdrawn" and memory.get(store, "user", second)[0]["value"] == "酸口"


def PM03():
    """旧资料按页续作，只有可证实的交付进入索引，缺失旧稿可定位。"""
    from agent.storage.execution_pipeline import Pipeline
    from harness.eval.production_store_cases import Objects
    from langchain_core.messages import HumanMessage, messages_to_dict

    pipeline = Pipeline(Objects(), "memory-backfill-fixture/")
    for ident, user in (("old-visible", "user"), ("old-missing", "user"), ("other-visible", "other")):
        receipt = pipeline.accept({"user_id": user, "chat_id": "old-chat", "message_id": ident, "text": "原始备注"}, source="cli")
        store = pipeline.store()
        store.save_document("receipt_state", "system", receipt["receipt_id"], {"delivery": {"presented": True},
            "result": {"presented_candidates": {"meal_draft_candidate": "missing-draft"} if ident == "old-missing" else {}}})
        store.save_document("dialogue_turn", user, "calorie-user:" + ident, {"id": "calorie-user:" + ident,
            "messages": messages_to_dict([HumanMessage(content="原始备注")])})
    first = memory_backfill.backfill_user(pipeline.store(), pipeline.inbox(readonly=True), "user", 1)
    assert not first["done"] and first["cursor"]
    progress = memory_backfill.backfill_user(pipeline.store(), pipeline.inbox(readonly=True), "user", 1)
    assert not progress["done"] and first["cursor"] != progress["cursor"]
    done = memory_backfill.backfill_user(pipeline.store(), pipeline.inbox(readonly=True), "user", 1)
    store = pipeline.store()
    assert done["done"] and done["indexed"] == 2 and set(done["uncovered"].values()) == {"structured_draft_missing"}
    assert not store.load_document("memory_index", "user")["coverage"]["complete"]
    assert not store.load_document("memory_index", "other") and not store.load_document("memory_work", "user")
    assert memory_backfill.backfill_user(store, pipeline.inbox(readonly=True), "user", 1) == done


CHECKS = {"PM01": PM01, "PM02": PM02, "PM03": PM03}
