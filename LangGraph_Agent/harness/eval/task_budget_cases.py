"""Real native tool limits and durable control/business argument parity."""
from copy import deepcopy
from unittest.mock import patch
from langchain_core.messages import AIMessage, ToolMessage, messages_from_dict, messages_to_dict
from agent import run
from agent.graph import mark_reply_presented
from harness.eval.personal_cases import CONFIG, NativeFake, call, setup
from harness.eval.production_pipeline_cases import fixture, event, message


def over_batch():
    return AIMessage(content="", tool_calls=[{"id": f"limit-{n}", "name": "query_today", "args": {}, "type": "tool_call"} for n in range(10)])


def TB01():
    """原生超批仍只执行8工具，确定终止可交付并推进队列，不进入JSON错误恢复。"""
    _, pipeline, config = fixture()
    receipt = pipeline.accept(message("limit-native-batch"))
    model = NativeFake(responses=[over_batch()])
    sends = []
    with patch("agent.config.load_config", return_value=config):
        state = pipeline.worker(event(receipt), execute=lambda msg, **kw: run(msg, model=model, **kw),
            send=lambda *a, **k: sends.append(a) or {"data": {"message_id": "limit-final"}})
        result = state["result"]
        assert state["status"] == "completed" and state["work_status"] == "terminated"
        assert result["termination_reason"] == "tool_call_limit" and result["model_calls"] == 1
        assert [fact["status"] for fact in result["tool_results"]] == ["ok"] * 8 + ["limit"] * 2
        saved = pipeline.readonly_store().load_execution(receipt["message"])["payload"]
        assert len(saved["termination"]["unexecuted_action_ids"]) == 2
        assert all(e.get("terminated") == "tool_call_limit" for e in saved["calls"][8:])
        assert pipeline.worker(event(receipt), execute=lambda *a, **k: (_ for _ in ()).throw(AssertionError("replanned")))["duplicate"]
        next_receipt = pipeline.accept(message("after-limit-batch"))
        done = pipeline.worker(event(next_receipt), execute=lambda msg, **kw: run(msg, model=NativeFake(responses=[call("query_today", {"finish_after_tools": True})]), **kw),
            send=lambda *a, **k: {"data": {"message_id": "next-final"}})
        assert done["status"] == "completed" and done["result"]["reply_source"] == "tool_facts"


def TB02():
    """旧已保存终态带框架文本error消息，恢复只修清洁来源不重开模型或动作。"""
    from agent.storage import conversation
    store = setup()
    msg = {"message_id": "legacy-limit-text", "user_id": "user", "chat_id": "test", "text": "多个查询"}
    stage = conversation.stage_turn
    def interrupted(*args, **kw):
        if not kw.get("reserve"):
            raise OSError("after final result saved before clean stage")
        return stage(*args, **kw)
    with patch.object(conversation, "stage_turn", side_effect=interrupted):
        try:
            run(msg, store=store, config=CONFIG, model=NativeFake(responses=[over_batch()]))
        except OSError:
            pass
        else:
            raise AssertionError("stage crash missing")
    task = store.load_execution(msg)
    payload = deepcopy(task["payload"])
    messages = messages_from_dict(payload["messages"])
    for index, item in enumerate(messages):
        if isinstance(item, ToolMessage) and item.tool_call_id == "limit-9":
            messages[index] = item.model_copy(update={"status": "error", "content": "Tool call limit exceeded. Do not call again."})
    payload["messages"] = messages_to_dict(messages)
    store.save_execution(task, payload, completed=True)
    fresh = deepcopy(store)
    model = NativeFake(responses=[])
    result = run(msg, store=fresh, config=CONFIG, model=model)
    assert result["terminal"] and result["reply"] == payload["result"]["reply"] and not model.requests
    mark_reply_presented(fresh, "user", "test", result)
    assert conversation.context(fresh, "user", "test")["turns"][-1]["complete"]


def TB03():
    """批次控制剥离后原生handler与持久业务参数一致，其他未知参数仍校验错误。"""
    for extra, expected in (({"finish_after_tools": False, "action_note": "查资料"}, "ok"), ({"bogus": True}, "error")):
        store = setup()
        result = run({"message_id": "control-" + expected, "user_id": "user", "chat_id": "test", "text": "查询夹具A100克"},
            store=store, config=CONFIG, model=NativeFake(responses=[call("query_food_nutrition", {"food_query": "夹具A", "grams": 100, **extra}), AIMessage(content="查询结果见上。")]))
        assert result["tool_results"][0]["status"] == expected
        if expected == "ok":
            assert result["tool_results"][0]["data"]["items"][0]["nutrition"]["kcal"] == 60
            assert "finish_after_tools" not in result["native_calls"][0]["args"]
        else:
            assert "invalid_tool_arguments" in result["tool_results"][0]["error"]


def TB04():
    """真实来源单位为数值字符串时，普通展示及模型失败异常回执均可交付。"""
    from harness.eval.personal_cases import A
    for failed in (False, True):
        foods = [{**deepcopy(A), "food_id": "raw" if n == 0 else "cooked", "name": name,
                  "units": [{"unit": "克", "eat_weight": "1.00", "weight": "1.00"}], "detail_loaded": True}
                 for n, name in enumerate(("鸡胸肉（生）", "鸡胸肉（熟）"))]
        from agent import FakeStore
        store = FakeStore(foods=foods, today="2026-11-04")
        def response(messages):
            if failed:
                raise RuntimeError("model unavailable after source query")
            return AIMessage(content="请选要查询的鸡胸肉来源。")
        with patch("agent.storage.boohee_client.search_food", return_value=[]):
            result = run({"message_id": "string-unit-" + str(failed), "user_id": "user", "chat_id": "test", "text": "查鸡胸肉营养"},
                store=store, config=CONFIG, model=NativeFake(responses=[call("query_food_nutrition", {"food_query": "鸡胸肉"}), response]))
        assert result["terminal"] == failed and "1克" in result["reply"]
        assert "鸡胸肉（生）" in result["reply"] and "鸡胸肉（熟）" in result["reply"]
        assert not store.intake


def TB05():
    """同轮营养候选投影仅给可执行source_ref和完整单位，可直接算稿；记餐choice保护保持。"""
    import json
    from agent import FakeStore
    from agent.replies import model_view
    from harness.eval.personal_cases import A
    foods = [{**deepcopy(A), "food_id": str(n), "name": name, "detail_loaded": True,
              "units": [{"unit": "克", "eat_weight": "1.00"}, {"unit_name": "碗", "eat_weight": "200.00"}]}
             for n, name in enumerate(("米饭（蒸）", "米饭（糙米）"))]
    store = FakeStore(foods=foods, today="2026-11-04")
    def calculate_from_candidates(messages):
        data = json.loads(next(m.content for m in reversed(messages) if isinstance(m, ToolMessage)))["data"]
        candidates = data["unfinished"][0]["candidates"]
        assert len(candidates) == 2 and all("choice" not in c for c in candidates)
        assert candidates[0]["features"]["units"] == [{"unit_name": "克", "eat_weight": "1.00"}, {"unit_name": "碗", "eat_weight": "200.00"}]
        return call("calculate_meal", {"scope": "daily", "items": [{"source_ref": candidates[0]["source_ref"],
            "grams": 250, "servings": 2, "portion_basis": "assumed_takeout", "assumption": "固定离线组成假设"}]}, cid="calculate-from-source")
    with patch("agent.storage.boohee_client.search_food", return_value=[]):
        result = run({"message_id": "same-turn-source", "user_id": "user", "chat_id": "test", "text": "按米饭资料估算两份菜单"},
            store=store, config=CONFIG, model=NativeFake(responses=[call("query_food_nutrition", {"food_query": "米饭"}),
            calculate_from_candidates, AIMessage(content="已按明确来源的组成假设计算。")]))
    assert result["status"] == "ok" and len(result["native_calls"]) == 2
    data = result["tool_results"][1]["data"]
    assert data["complete"] and data["total"] == {"carbs": 50, "protein": 10, "fat": 5, "kcal": 300}
    assert not store.intake and not store.load_session("user", "test").get("meal_draft")
    # Same-turn choice is still invalid; eliminating misleading choice fields
    # must not loosen user-confirmed offer or intake-pending boundaries.
    wrong = run({"message_id": "same-turn-choice-invalid", "user_id": "user", "chat_id": "test", "text": "第一个"},
        store=store, config=CONFIG, model=NativeFake(responses=[call("query_food_nutrition", {"choice": 1}), AIMessage(content="请引用已展示的选项。")]))
    assert wrong["tool_results"][0]["status"] == "pending"
    projected = model_view({"tool": "record_meal", "status": "pending", "data": {"unfinished": [{"candidates": foods}]}})
    assert [c["choice"] for c in projected["data"]["unfinished"][0]["candidates"]] == [1, 2]


CHECKS = {"TB01": TB01, "TB02": TB02, "TB03": TB03, "TB04": TB04, "TB05": TB05}
