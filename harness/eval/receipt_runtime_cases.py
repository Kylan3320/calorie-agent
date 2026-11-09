"""Actual native agent-loop receipt completion, using local tools and a fake model."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from unittest.mock import patch

from langchain_core.messages import AIMessage, ToolMessage

from agent import run
from agent.graph import mark_reply_presented
from agent.storage import boohee_client
from harness.eval.personal_cases import CONFIG, MEAL, NativeFake, call, setup, transport


def _send(store, text, responses, ident):
    message = {"message_id": ident, "user_id": "user", "chat_id": "receipt-runtime", "text": text}
    model = NativeFake(responses=responses)
    with patch.dict("os.environ", {"TRACE_ENABLED": "false"}), redirect_stdout(io.StringIO()):
        result = run(message, store=store, config=CONFIG, model=model)
    assert result["status"] == "ok", result["errors"]
    return result, model, message


def _finish(name, args, ident="c1"):
    return call(name, {**deepcopy(args), "finish_after_tools": True}, ident)


def _direct(result, model, decisions=1):
    assert result["reply_source"] == "tool_facts", result
    assert result["model_reply"] == "" and result["reply"]
    assert result["model_calls"] == len(model.requests) == decisions
    assert result["completion"]["adopted"] is True
    assert result["guard_result"]["fallback_used"] is False
    assert result["guard_result"]["checked_scope"] == "committed_tool_facts_and_batch_completion"
    for entry in result["native_calls"]:
        assert entry["batch_id"] and "finish_after_tools" not in entry["args"]


def _model_body(result, model, text, decisions=2):
    assert result["reply_source"] in {"agent", "agent_adjusted"}
    assert result["model_reply"] == text and text in result["reply"]
    assert result["model_calls"] == len(model.requests) == decisions
    assert result["guard_result"]["fallback_used"] is False
    assert result["completion"]["adopted"] is False


def _current_fact(messages, tool):
    facts = [json.loads(message.content) for message in messages if isinstance(message, ToolMessage)]
    return next(fact for fact in reversed(facts) if fact.get("tool") == tool and fact.get("status") != "historical")


def RR01():
    """run入口普通270→210→105→315均一决策事实回执，部分/跨日/候选/取消不丢原餐。"""
    store = setup()
    result, model, message = _send(store, "记录午餐夹具A200克和夹具B2个", [_finish("record_meal", MEAL)], "rr-meal")
    _direct(result, model)
    assert [(row["grams"], row["nutrition"]["kcal"]) for row in store.intake] == [(200, 120), (100, 150)]
    assert result["tool_results"][0]["data"]["total"] == {"carbs": 20, "protein": 16, "fat": 12, "kcal": 270}
    assert "270千卡" in result["reply"] and "估算" in result["reply"]
    durable = store.load_execution(message)["payload"]
    assert durable["calls"][0]["finish_after_tools"] is True
    assert durable["calls"][0]["batch_id"] == result["native_calls"][0]["batch_id"]
    assert "finish_after_tools" not in durable["calls"][0]["args"]
    assert durable["calls"][0]["result"] == result["tool_results"][0]
    # A saved direct reply replays with no model and no duplicate meal.
    replay, replay_model, _ = _send(store, message["text"], [], message["message_id"])
    assert not replay_model.requests and replay["reply"] == result["reply"] and len(store.intake) == 2

    result, model, _ = _send(store, "夹具A改成100克", [_finish("revise_intake", {"record_id": store.intake[0]["record_id"], "grams": 100})], "rr-revise")
    _direct(result, model)
    assert store.intake[0]["nutrition"]["kcal"] == 60
    assert result["tool_results"][0]["data"]["meal_totals"][0]["total"]["kcal"] == 210
    assert "210千卡" in result["reply"] and "270千卡" not in result["reply"]

    result, model, _ = _send(store, "再记录半份刚才的午餐", [_finish("record_meal", {"reuse": {"latest": True, "ratio": .5}})], "rr-reuse")
    _direct(result, model)
    assert [(row["grams"], row["nutrition"]["kcal"]) for row in store.intake[2:]] == [(50, 30), (50, 75)]
    assert result["tool_results"][0]["data"]["total"]["kcal"] == 105
    assert "105千卡" in result["reply"] and "315千卡" not in result["reply"]

    result, model, _ = _send(store, "今天一共记录多少热量", [_finish("query_today", {})], "rr-today")
    _direct(result, model)
    assert store.today_summary("user")["totals"] == {"carbs": 15, "protein": 21, "fat": 16.5, "kcal": 315}
    assert "315千卡" in result["reply"] and "估算" in result["reply"]

    # Candidate presentation is acknowledged through the real delivery binding.
    store = setup()
    partial = {"meal_type": "lunch", "items": [{"food_query": "夹具A", "grams": 200}, {"food_query": "鱼", "grams": 100}, {"food_query": "夹具B"}]}
    result, model, _ = _send(store, "午餐记录A200克、鱼100克和B", [_finish("record_meal", partial)], "rr-partial")
    _direct(result, model)
    assert result["completion_status"] == "awaiting_user" and len(store.intake) == 1
    assert all(text in result["reply"] for text in ("120千卡", "尚未记录", "100克", "请选择食物候选", "1. 鱼（生）", "夹具B"))
    mark_reply_presented(store, "user", "receipt-runtime", result)
    pending = store.load_session("user", "receipt-runtime")["unfinished"]
    fish = next(item for item in pending.values() if item["food_query"] == "鱼")
    b = next(item for item in pending.values() if item["food_query"] == "夹具B")
    assert fish["offered_message_id"] == "rr-partial"
    store.today = "2026-11-01"

    def fish_transport(url, headers):
        if "code=fish" in url:
            return {"code": 0, "data": {"code": "fish", "name": "鱼（生）", "carbohydrate": 0, "protein": 20, "fat": 2, "calories": 110, "units": []}}
        return transport(url, headers)

    boohee_client.set_transport(fish_transport)
    completion = {"items": [{"food_query": "夹具B", "pending_id": b["pending_id"], "quantity": 2, "unit": "个"},
                             {"food_query": "鱼", "pending_id": fish["pending_id"], "choice": 1}]}
    result, model, _ = _send(store, "B是2个，鱼选第1个", [_finish("record_meal", completion)], "rr-complete")
    _direct(result, model)
    assert len(store.intake) == 3 and {row["date"] for row in store.intake} == {"2026-10-31"}
    assert result["tool_results"][0]["data"]["total"]["kcal"] == 260
    assert "380千卡" in result["reply"] and "260千卡" not in result["reply"] and "2026-10-31午餐" in result["reply"]
    assert not store.load_session("user", "receipt-runtime")["unfinished"]

    result, model, _ = _send(store, "补记10月30日夹具B2袋", [_finish("record_meal", {"date": "2026-10-30", "items": [{"food_query": "夹具B", "quantity": 2, "unit": "袋"}]})], "rr-cancel-source")
    _direct(result, model)
    assert "尚未记录" in result["reply"] and "2袋" in result["reply"]
    pending_id = next(iter(store.load_session("user", "receipt-runtime")["unfinished"]))
    result, model, _ = _send(store, "取消这项待补充记录", [_finish("record_meal", {"cancel_pending": [pending_id]})], "rr-cancel")
    _direct(result, model)
    assert all(text in result["reply"] for text in ("已取消未完成项", "2026-10-30", "夹具B", "2袋"))
    assert len(store.intake) == 3 and not store.load_session("user", "receipt-runtime")["unfinished"]


def RR02():
    """完整批次与两批依赖不截断；建议/缺声明/不一致/proposal/error继续模型正文。"""
    store = setup()
    store.save_document("user_settings", "user", "", {"targets": {"kcal": 2000}})

    def query_after_record(messages):
        first = _current_fact(messages, "record_meal")
        assert first["status"] == "ok" and first["data"]["meal_totals"][0]["total"]["kcal"] == 270
        return _finish("query_today", {"detail": "summary"}, "c2")

    result, model, _ = _send(store, "记录这份午餐，再查询今天还剩多少热量", [call("record_meal", {**MEAL, "finish_after_tools": False}), query_after_record], "rr-record-remaining")
    _direct(result, model, 2)
    assert [entry["tool"] for entry in result["native_calls"]] == ["record_meal", "query_today"]
    assert [entry["finish_after_tools"] for entry in result["native_calls"]] == [False, True]
    assert len({entry["batch_id"] for entry in result["native_calls"]}) == 2
    assert result["tool_results"][1]["data"]["remaining"]["kcal"] == 1730
    assert "剩余：热量1730千卡" in result["reply"] and "270千卡" in result["reply"]

    # A history items lookup supplies the opaque ID used by the next decision.
    store.today = "2026-11-01"

    def revise_after_history(messages):
        history = _current_fact(messages, "query_history")
        assert history["data"]["detail"] == "items" and history["data"]["count"] == 1
        row = history["data"]["items"][0]
        assert row["food_name"] == "夹具A" and row["date"] == "2026-10-31"
        return _finish("revise_intake", {"record_id": row["record_id"], "grams": 100}, "c2")

    result, model, _ = _send(store, "查询10月31日夹具A，把那项改为100克", [call("query_history", {"start_date": "2026-10-31", "end_date": "2026-11-01", "food_query": "夹具A", "detail": "items", "finish_after_tools": False}), revise_after_history], "rr-history-revise")
    _direct(result, model, 2)
    assert [entry["tool"] for entry in result["native_calls"]] == ["query_history", "revise_intake"]
    assert [entry["finish_after_tools"] for entry in result["native_calls"]] == [False, True]
    assert "210千卡" in result["reply"] and "2026-10-31" in result["reply"] and "270千卡" not in result["reply"]
    assert store.intake[0]["grams"] == 100 and store.intake[0]["nutrition"]["kcal"] == 60

    # A successful preliminary read does not require another model-written receipt.
    store = setup()
    result, model, _ = _send(store, "记录A200克", [
        call("query_food_nutrition", {"food_query": "夹具A", "grams": 100}),
        _finish("record_meal", {"items": [{"food_query": "夹具A", "grams": 200}]}, "c2"),
    ], "rr-nutrition-record")
    _direct(result, model, 2)
    assert len(result["tool_results"]) == 2 and result["tool_results"][0]["data"]["items"][0]["nutrition"]["kcal"] == 60
    assert len(store.intake) == 1 and "120千卡" in result["reply"]

    # Earlier writes and failed reads still need a reply, even when the last
    # ledger batch alone would have a supported result shape.
    for name, args, first_status, text in (
        ("update_user_settings", {"kind": "targets", "values": {"kcal": 1900}}, "ok", "目标已设为1900千卡，A已记录120千卡。"),
        ("query_food_nutrition", {"food_query": "夹具A", "grams": -1}, "error", "营养查询参数有误；A已记录120千卡。"),
    ):
        store = setup()
        result, model, _ = _send(store, text, [
            call(name, args),
            _finish("record_meal", {"items": [{"food_query": "夹具A", "grams": 200}]}, "c2"),
            AIMessage(content=text),
        ], "rr-prior-" + name)
        _model_body(result, model, text, 3)
        assert result["tool_results"][0]["status"] == first_status and len(store.intake) == 1
        if name == "update_user_settings":
            assert store.get_standard("user")["kcal"] == 1900

    store = setup()
    advice = "这餐已记录，合计270千卡，份量含估算。下一餐可以搭配蔬菜。"
    result, model, _ = _send(store, "记录午餐并给个下一餐搭配建议", [call("record_meal", {**MEAL, "finish_after_tools": False}), AIMessage(content=advice)], "rr-record-advice")
    _model_body(result, model, advice)
    assert len(store.intake) == 2 and "搭配蔬菜" in result["reply"]

    store = setup()
    answer = "该范围未记录。"
    result, model, _ = _send(store, "查询今天", [call("query_today", {}), AIMessage(content=answer)], "rr-missing-declaration")
    _model_body(result, model, answer)
    assert result["native_calls"][0]["finish_after_tools"] is False

    inconsistent = AIMessage(content="", tool_calls=[
        {"name": "record_meal", "args": {"items": [{"food_query": "夹具A", "grams": 200}], "finish_after_tools": True}, "id": "c1", "type": "tool_call"},
        {"name": "query_today", "args": {"finish_after_tools": False}, "id": "c2", "type": "tool_call"}])
    answer = "夹具A200克已记录，今天合计120千卡。"
    result, model, _ = _send(store, "记录A200克然后查今天", [inconsistent, AIMessage(content=answer)], "rr-inconsistent-declarations")
    _model_body(result, model, answer)
    assert len(result["tool_results"]) == 2 and result["tool_results"][1]["data"]["total"]["kcal"] == 120
    assert len({entry["batch_id"] for entry in result["native_calls"]}) == 1
    assert result["completion"]["declared"] == [True, False]

    store = setup()
    explanation = "可以先考虑这份搭配。"
    result, model, _ = _send(store, "先建议记录这份午餐，等我确认", [_finish("record_meal", {**MEAL, "mode": "propose"}), AIMessage(content=explanation)], "rr-proposal")
    _model_body(result, model, explanation)
    assert result["tool_results"][0]["status"] == "proposal" and result["proposal_id"] and not store.intake
    assert "回复确认" in result["reply"]
    assert store.load_session("user", "receipt-runtime")["proposal"]["presented"] is False

    store = setup()
    correction = "份量需要大于零，请重新提供可食克重。"
    result, model, _ = _send(store, "记录A负1克", [_finish("record_meal", {"items": [{"food_query": "夹具A", "grams": -1}]}), AIMessage(content=correction)], "rr-tool-error")
    _model_body(result, model, correction)
    assert result["tool_results"][0]["status"] == "error" and not store.intake
    assert result["completion"]["declared"] == [True]


CHECKS = {"RR01": RR01, "RR02": RR02}
