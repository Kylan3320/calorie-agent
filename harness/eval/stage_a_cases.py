import json

from langchain_core.messages import AIMessage, ToolMessage

from agent.graph import mark_reply_presented, run
from agent.storage import experience, memory
from agent.storage.fake_store import FakeStore
from harness.eval.runtime_cases import CONFIG, NativeFake, call


def review_response(public, *marks):
    return public + "\n<review_sources>" + json.dumps(marks, ensure_ascii=False) + "</review_sources>"


def first_feedback_fixture():
    store = FakeStore()
    text = "米饭和配菜分开打包以后，午休时间够我热饭了。"
    message = {"user_id": "user", "chat_id": "work-lunch", "message_id": "first-feedback", "text": text}
    response = AIMessage(content=review_response("我明白了。", {"ref": "current_turn", "reason": "有实际使用结果"}))
    result = run(message, store=store, config=CONFIG, model=NativeFake(responses=[response]))
    assert result["reply"] == "我明白了。" and not result["native_calls"] and result["model_calls"] == 1
    assert not store.load_document("memory_work", "user")
    mark_reply_presented(store, "user", message["chat_id"], result)
    source = memory.reference("user", "turn", result["dialogue_turn_id"])
    snapshot = experience.snapshot(store, "user", [source], store.today)
    assert snapshot["materials"][0]["dialogue"][0]["text"] == text
    assert snapshot["materials"][0]["review_sources"][0]["ref"] == source
    return store, snapshot, text


def PA01():
    """First feedback can be marked in prose or native tool batches and queues on delivery only."""
    store, snapshot, _ = first_feedback_fixture()
    assert list(store.load_document("memory_work", "user")["pending"]) == [snapshot["materials"][0]["ref"]]
    foreign = memory.reference("other", "turn", "calorie-user:private")
    response = call("query_today", {"finish_after_tools": True}).model_copy(update={"content": review_response("",
        {"ref": foreign, "reason": "无权访问的来源"}, {"ref": "current_turn", "reason": "当轮实际变化"})})
    result = run({"user_id": "user", "chat_id": "work-lunch", "message_id": "feedback-with-tool", "text": "今天合计多少？"},
                 store=store, config=CONFIG, model=NativeFake(responses=[response]))
    assert result["reply_source"] == "tool_facts" and result["model_calls"] == 1
    assert "review_sources" not in result["reply"] and "review_sources" not in result["model_steps"][0]
    mark_reply_presented(store, "user", "work-lunch", result)
    mark_reply_presented(store, "user", "work-lunch", result)
    ref = memory.reference("user", "turn", result["dialogue_turn_id"])
    assert store.load_document("memory_review", "user", ref)["sources"] == [{"ref": ref, "reason": "当轮实际变化"}]
    assert len(store.load_document("memory_work", "user")["pending"]) == 2


def PA02():
    """A natural confirmation binds to the preceding displayed proposal and current user words."""
    for case in ("confirm", "different_amount", "foreign_quote", "stale_target"):
        store = FakeStore()

        def ask(mid, text, *responses):
            result = run({"user_id": "user", "chat_id": "chat", "message_id": mid, "text": text}, store=store,
                         config=CONFIG, model=NativeFake(responses=[*responses, AIMessage(content="处理完毕。")]))
            mark_reply_presented(store, "user", "chat", result)
            return result

        proposal = ask("offer", "先建议长期目标，等我确认", call("update_user_settings", {"mode": "propose",
            "kind": "targets", "values": {"kcal": 1900}}))
        text = "就照你刚才提的热量目标开始执行吧。"
        args = {"mode": "confirm", "kind": "targets", "proposal_id": proposal["proposal_id"], "authorization_quote": text}
        if case == "different_amount":
            args["values"] = {"kcal": 1700}
        if case == "foreign_quote":
            args["authorization_quote"] = "旧会话里同意过"
        if case == "stale_target":
            store.save_document("user_settings", "user", "", {"targets": {"kcal": 2100}})
        result = ask("respond", text, call("update_user_settings", args))
        assert result["tool_results"][0]["status"] == ("ok" if case == "confirm" else "pending")
        assert (store.get_standard("user")["kcal"] == 1900) == (case == "confirm")
        assert not store.load_session("user", "chat").get("proposal")


def PA03():
    """An evaluated non-default menu is the only version published when the model selects it."""
    store = FakeStore()
    text = "给我一份可以接着改的午饭菜单，今天优先好装盒，再看热量差距。"
    plan = {"scope": "daily", "selection_mode": "compare", "use_default_calibration": False,
            "items": [{"item_id": "rice", "food_query": "米饭", "grams": 120}],
            "adjustment": {"additions": [{"item_id": "egg", "food_query": "鸡蛋", "grams": 80}]}}

    def choose(messages):
        tool = next(message for message in reversed(messages) if isinstance(message, ToolMessage))
        fact = json.loads(tool.content)
        options = fact["data"]["selection"]
        assert fact["feedback"]["outcome"] == "unresolved"
        assert len(options["candidates"]) == 2 and options["awaiting_choice"]
        choice = next(row for row in options["candidates"] if row["candidate_ref"] != options["default_ref"])
        return call("calculate_meal", {"candidate_ref": choice["candidate_ref"], "finish_after_tools": True}, "adopt")

    result = run({"user_id": "user", "chat_id": "chat", "message_id": "choose", "text": text}, store=store,
                 config=CONFIG, model=NativeFake(responses=[call("calculate_meal", plan), choose]))
    assert result["reply_source"] == "tool_facts" and result["model_calls"] == 2, result["errors"]
    picked = result["tool_results"][-1]["data"]
    initial = result["tool_results"][0]["data"]["selection"]
    assert picked["selection"]["reason"] == "model_choice"
    assert picked["selection"]["selected"] != next(item["variant"] for item in initial["candidates"]
                                                  if item["candidate_ref"] == initial["default_ref"])
    assert not store.load_session("user", "chat").get("meal_draft") and not store.intake
    mark_reply_presented(store, "user", "chat", result)
    assert store.load_session("user", "chat")["meal_draft"]["draft_id"] == picked["draft_id"]


def PA04():
    """A theme outside the small context window remains pageable and reachable by a rewritten query."""
    from types import SimpleNamespace

    store = FakeStore()
    refs = []
    for index in range(10):
        theme = memory.save(store, "user", "theme", str(index), {"status": "usable", "text": f"主题材料 {index}",
                            "title": str(index), "date": store.today, "links": []})
        refs.append(theme["ref"])
    target = memory.save(store, "user", "theme", "new-lunch", {"status": "usable", "text": "便当分装方便加热",
                         "title": "便当", "date": store.today, "links": []})["ref"]
    view = memory.state(store, "user")
    view["entries"] = refs
    store.save_document("memory_state", "user", "", view)
    assert target not in {item["ref"] for item in memory.context_view(store, "user", store.today, "new", {})["themes"]}
    rt = SimpleNamespace(store=store, user_id="user", day=store.today)
    cursor, seen = "", set()
    for _ in range(6):
        data = memory.query(rt, {"operation": "search", "kinds": ["theme"], "limit": 2, "cursor": cursor})["data"]
        page = {item["ref"] for item in data["results"]}
        assert not (seen & page)
        seen.update(page)
        cursor = data["next_cursor"]
        if not cursor:
            break
    assert target in seen and len(seen) == 11
    empty = memory.query(rt, {"operation": "search", "match": "phrase", "query": "饭菜怎么分装才好热"})["data"]
    rewritten = memory.query(rt, {"operation": "search", "query": "分装 加热"})["data"]
    assert empty["search_status"] == "no_matches" and rewritten["results"][0]["ref"] == target
    assert memory.query(rt, {"operation": "read", "ref": target})["data"]["item"]["title"] == "便当"


CHECKS = {"PA01": PA01, "PA02": PA02, "PA03": PA03, "PA04": PA04}
