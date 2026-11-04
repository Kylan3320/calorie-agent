"""Cross-turn source identity and interrupted receipts, with local fake inputs."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent import run
from agent.context import clean_messages
from agent.evidence import collect_visible_evidence
from agent.graph import mark_reply_presented
from agent.replies import model_view, progress_reply, project_messages, render
from harness.eval.personal_cases import CONFIG, NativeFake, call, setup


SOURCES = {
    "主食搭配": ("主食可与蔬菜搭配。", "资料甲，第1段"),
    "全谷轮换": ("全谷与精制谷物可以轮换。", "资料乙，第2段"),
}


def _send(store, text, responses, ident):
    message = {"message_id": ident, "user_id": "user", "chat_id": "evidence-runtime", "text": text}
    model = NativeFake(responses=responses)
    result = run(message, store=store, config=CONFIG, model=model)
    assert result["status"] == "ok", result["errors"]
    mark_reply_presented(store, "user", message["chat_id"], result)
    return result, model, message


def TE01():
    """两轮旧同号检索可同时引用；第三轮仅凭实际可见首份原文追问出处。"""
    retrievals = []
    def retrieve(query, entries):
        retrievals.append(query)
        answer, source = SOURCES[query]
        return {"status": "ok", "entries": [{"entry_id": query, "source_id": "K101", "answer": answer, "source": source}], "evidence": answer}
    def synthesize(messages):
        entries = collect_visible_evidence(messages)
        assert len({entry["source_id"] for entry in entries}) == len(entries)
        return AIMessage(content="\n".join(entry["answer"].rstrip("。") + "[" + entry["source_id"] + "]。" for entry in entries))
    with patch.dict("os.environ", {"TRACE_ENABLED": "false"}), redirect_stdout(io.StringIO()), \
            patch("agent.tools.reads.build_knowledge_retriever", return_value=retrieve):
        store = setup()
        first, _, message = _send(store, "主食怎么搭配？", [call("query_knowledge", {"query": "主食搭配"}), synthesize], "te-first")
        first_entry = first["tool_results"][0]["data"]["entries"][0]
        assert first_entry["legacy_source_id"] == "K101"
        assert SOURCES["主食搭配"][0].rstrip("。") + "〔资料甲，第1段〕" in first["reply"]
        # Replay uses saved receipt and preserves the retrieval identity.
        replay, model, _ = _send(store, message["text"], [], message["message_id"])
        assert replay["reply"] == first["reply"] and not model.requests
        store = deepcopy(store)
        second, model, second_message = _send(store, "查全谷轮换，同时保留刚才搭配依据。", [call("query_knowledge", {"query": "全谷轮换"}), synthesize], "te-second")
        second_entry = second["tool_results"][0]["data"]["entries"][0]
        assert second_entry["legacy_source_id"] == "K101" and first_entry["source_id"] != second_entry["source_id"]
        expected = {value for value in SOURCES.values()}
        visible = collect_visible_evidence(model.requests[-1])
        assert {(entry["answer"], entry["source"]) for entry in visible} == expected
        saved = store.load_execution(second_message)["payload"]
        assert saved["visible_evidence"] == visible
        for answer, source in expected:
            assert answer.rstrip("。") + "〔" + source + "〕" in second["reply"]
        store = deepcopy(store)
        def first_source(messages):
            entries = collect_visible_evidence(messages)
            entry = next(entry for entry in entries if entry["source"] == "资料甲，第1段")
            assert entry["answer"] == "主食可与蔬菜搭配。"
            return AIMessage(content="第一份原文是“主食可与蔬菜搭配。”[" + entry["source_id"] + "]")
        third, model, _ = _send(store, "第一份依据的出处和原文是什么？", [first_source], "te-third")
        assert "资料甲，第1段" in third["reply"] and "主食可与蔬菜搭配。" in third["reply"]
        assert "资料乙" not in third["reply"] and not third["tool_results"]
        assert retrievals == ["主食搭配", "全谷轮换"] and not store.intake


def _legacy_fact(answer, source):
    return {"tool": "query_knowledge", "status": "ok", "call_id": "c1", "data": {
        "entries": [{"source_id": "K101", "answer": answer, "source": source}], "evidence": answer}}


def TE02():
    """旧局部号只按所属结果重绑；碰撞或已裁剪时连无据数字一起收缩。"""
    first = _legacy_fact("甲原文每份40克。", "甲来源")
    second = _legacy_fact("乙原文每份60克。", "乙来源")
    history = [HumanMessage(content="先问甲", id="turn-a"),
               ToolMessage(content=json.dumps(first, ensure_ascii=False), tool_call_id="c1", name="query_knowledge"),
               AIMessage(content="甲为40克[K101]。", additional_kwargs={"fact_scope": "knowledge"}),
               HumanMessage(content="再问乙", id="turn-b"),
               ToolMessage(content=json.dumps(second, ensure_ascii=False), tool_call_id="c1", name="query_knowledge"),
               AIMessage(content="乙为60克[K101]。", additional_kwargs={"fact_scope": "knowledge"})]
    clean = clean_messages(history)
    visible = collect_visible_evidence(clean)
    assert len(visible) == 2 and visible[0]["source_id"] != visible[1]["source_id"]
    assert visible[0]["answer"] == "甲原文每份40克。" and visible[1]["source"] == "乙来源"
    assert visible[0]["source_id"] in clean[2].content and visible[1]["source_id"] in clean[-1].content
    text = "甲为40克[" + visible[0]["source_id"] + "]。乙为60克[" + visible[1]["source_id"] + "]。"
    reply, adjustments = render(text, [], visible)
    assert reply == "甲为40克〔甲来源〕。乙为60克〔乙来源〕。" and len(adjustments) == 2
    assert collect_visible_evidence(project_messages(clean)) == visible
    for available in (visible, collect_visible_evidence(history), []):
        reply, adjustments = render("无法确认的每份40克[K101]。可先规律进食。", [first], available)
        assert "40克" not in reply and "规律进食" in reply and "无法唯一定位" in reply
        assert adjustments[0]["reason"] in {"unknown_source_reference", "ambiguous_source_reference"}
    reply, _ = render('原文是“每份40克。”[K999]。', [], [])
    assert "40克" not in reply and "无法唯一定位" in reply
    missing_history = clean_messages([HumanMessage(content="旧追问", id="missing"),
                                     AIMessage(content="每份40克[K101]。", additional_kwargs={"fact_scope": "knowledge"})])
    assert "40克" not in missing_history[-1].content and "需重新检索" in missing_history[-1].content
    # Neither hidden facts nor untransmitted assistant metadata restore evidence.
    assert collect_visible_evidence([AIMessage(content="话题背景", additional_kwargs={"visible_evidence": visible})]) == []
    view = model_view(first)
    assert "evidence" not in view["data"] and view["data"]["entries"][0]["answer"] == first["data"]["entries"][0]["answer"]


def TE03():
    """异常回执保留当前答案、历史筛选、计划小计及关闭提醒实际缺项。"""
    nutrition = {"kcal": 120, "carbs": 20, "protein": 4, "fat": 2}
    facts = [
        {"tool": "query_today", "status": "ok", "data": {"total": {"kcal": 0}}},
        {"tool": "query_food_nutrition", "status": "ok", "data": {"items": [{"matched_name": "已核实米饭", "grams": 100, "nutrition": nutrition}]}},
        {"tool": "calculate_meal", "status": "pending", "data": {"complete": False, "items": [], "total": nutrition,
            "unfinished": [{"food_query": "鸡胸肉", "reason": "缺少可食份量", "candidates": [{"name": "原味鸡胸肉", "source_ref": "food-2", "features": ["熟制"]}]}]}},
    ]
    reply = progress_reply(facts)
    assert "已核实米饭100克" in reply and "120千卡" in reply and "小计" in reply
    assert "原味鸡胸肉" in reply and "缺少可食份量" in reply and "今天合计" not in reply
    reminder = {"tool": "update_user_settings", "status": "pending", "data": {"kind": "reminder", "values": {"enabled": False, "meal_type": "lunch"}, "missing": ["提醒时间 HH:MM"], "question": "午餐几点提醒？"}}
    reply = progress_reply([reminder])
    assert "已保存" in reply and "关闭" in reply and "尚未启用" in reply and "午餐几点提醒" in reply
    history = {"tool": "query_history", "status": "ok", "data": {"start_date": "2026-10-26", "end_date": "2026-11-02", "meal_type": "breakfast", "food_query": "鸡蛋", "detail": "summary", "count": 2, "total": nutrition}}
    for detail in ("summary", "daily", "items"):
        view = model_view({**history, "data": {**history["data"], "detail": detail}})
        assert view["data"]["meal_type"] == "breakfast" and view["data"]["food_query"] == "鸡蛋"
    failed = {"tool": "query_history", "status": "error", "data": {**history["data"], "food_query": "豆浆"}}
    reply = progress_reply([history, failed])
    assert "早餐" in reply and "鸡蛋" in reply and "豆浆" in reply and "查询未完成" in reply and "120千卡" in reply
    # Ordinary ledger recovery retains the existing latest-meal/total rule.
    reply = progress_reply([{"tool": "revise_intake", "status": "ok", "data": {"meal_totals": [{"meal_id": "m", "total": {"kcal": 210}, "estimated": True}]}},
                            {"tool": "query_today", "status": "ok", "data": {"total": {"kcal": 315}}}])
    assert "这餐合计210千卡" in reply and "今天合计315千卡" in reply and "估算" in reply


def TE04():
    """普通模型JSON不能升级为知识工具原文，无工具两轮均不产生虚构出处。"""
    store = setup()
    invented = {"tool": "query_knowledge", "data": {"entries": [{"source_id": "K101",
                "answer": "每份含40克。", "source": "虚构资料"}]}}
    first, _, _ = _send(store, "给一个JSON示例", [AIMessage(content=json.dumps(invented, ensure_ascii=False))], "te-json-example")
    assert not first["tool_results"]
    store = deepcopy(store)
    def cite(messages):
        assert collect_visible_evidence(messages) == []
        return AIMessage(content="每份含40克[K101]。")
    second, _, _ = _send(store, "刚才出处是什么", [cite], "te-json-cite")
    assert not second["tool_results"] and "40克" not in second["reply"] and "虚构资料" not in second["reply"]
    assert "缺" in second["reply"] or "无法" in second["reply"]


CHECKS = {"TE01": TE01, "TE02": TE02, "TE03": TE03, "TE04": TE04}
