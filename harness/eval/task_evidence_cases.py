"""Cross-turn source identity and interrupted receipts, with local fake inputs."""
from contextlib import redirect_stdout
from copy import deepcopy
import io
import json
from unittest.mock import patch

import httpx
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent import run
from agent.context import clean_messages
from agent.evidence import collect_visible_evidence
from agent.graph import mark_reply_presented
from agent.replies import model_view, progress_reply, project_messages, render
from harness.eval.personal_cases import CONFIG, NativeFake, call, send, setup


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
    reply, _ = render("甲为40克［" + visible[0]["source_id"] + "］。", [], visible)
    assert reply == "甲为40克〔甲来源〕。"
    reply, _ = render("无依据的40克［K999］。", [], visible)
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


def TE05():
    """Web weight must support the count; complete the original item within four decisions."""
    url = "https://example.org/portion"
    search = call("query_food_nutrition", {"portion_web": {"operation": "search", "query": "夹具B 可食份量"}}, "c2")
    fetch = call("query_food_nutrition", {"portion_web": {"operation": "fetch", "urls": [url]}}, "c3")
    first = call("record_meal", {"date": "2026-10-30", "meal_type": "lunch", "finish_after_tools": True, "items": [
        {"food_query": "夹具A", "grams": 100}, {"food_query": "夹具B", "quantity": 2, "unit": "个"}]})

    def fact(messages, ident):
        return json.loads(next(m.content for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == ident))

    for source_food, statement, food_type, rejected in [
        ("夹具B", "合计", "", ""),
        ("夹具B", "含钠", "", "未明确支持"),
        ("夹具B饼", "合计", "", "名称或别名无法对应"),
        ("生夹具B", "合计", "熟", "生熟口径不同"),
    ]:
        quote = f"2个{source_food}的可食部分{statement}150克"
        store = setup()
        store.foods[1].update(units=[], detail_loaded=True, type=food_type)

        def search_after_commit(messages):
            gap, = fact(messages, "c1")["feedback"]["unmet_conditions"]
            assert gap["code"] == "portion_evidence_missing" and not gap.get("requires_user")
            assert len(store.intake) == 1 and store.intake[0]["food_name"] == "夹具A"
            return search

        def complete(messages):
            unfinished, = fact(messages, "c1")["data"]["unfinished"]
            page, = fact(messages, "c3")["data"]["portion_web"]["pages"]
            return call("record_meal", {"date": "2026-10-31", "meal_type": "dinner", "finish_after_tools": True,
                "items": [{"food_query": "夹具B", "pending_id": unfinished["pending_id"], "quantity": 1, "unit": "个",
                "portion_evidence": {"ref": page["ref"], "quote": quote, "source_food": source_food, "source_unit": "个",
                    "applicability": "同一食物和可食部口径", "basis": "edible_total", "weight": 150, "weight_unit": "g", "count": 2}}]}, "c4")

        requests = []

        def provider(request):
            requests.append(request.url.path.rsplit("/", 1)[-1])
            if requests[-1] == "search":
                return httpx.Response(200, json={"results": [{"url": url, "title": "份量"}]})
            return httpx.Response(200, json={"title": "份量", "content": quote})

        text = "记昨天午饭：夹具A100克、夹具B两个"
        if rejected:
            text += "。来源：" + url
        with patch.dict("os.environ", {"TRACE_ENABLED": "false", "DEEPSEEK_SEARCH_API_KEY": "fixture"}), redirect_stdout(io.StringIO()), \
                httpx.Client(transport=httpx.MockTransport(provider)) as client, patch("agent.storage.deepseek_web._client", return_value=client):
            steps = [first, fetch, complete] if rejected else [first, search_after_commit, fetch, complete]
            result, model = send(store, text, steps, mid="portion-evidence")
            assert len(model.requests) == 4 and requests == (["fetch"] if rejected else ["search", "fetch"])
            if not rejected:
                assert not store.load_session("user", "test")["unfinished"] and len(store.intake) == 2
                a, b = store.intake
                assert (b["date"], b["meal_type"], b["grams"], b["nutrition"]["kcal"]) == ("2026-10-30", "lunch", 150, 225)
                assert b["snapshot"]["meal_id"] == a["snapshot"]["meal_id"]
                evidence = b["snapshot"]["portion_evidence"]
                assert (evidence["quote"], evidence["url"], evidence["grams_per_unit"]) == (quote, url, 75)
                assert b["snapshot"]["estimated"] and url in result["reply"]
            else:
                assert len(store.intake) == 1 and len(store.load_session("user", "test")["unfinished"]) == 1
                assert url not in result["reply"] and rejected in result["reply"]
            replay, empty = send(store, text, [], mid="portion-evidence")
            assert replay["reply"] == result["reply"] and not empty.requests and len(requests) == (1 if rejected else 2)
            if not rejected:
                send(store, "以后这个夹具B每个按75克估算", [call("update_user_settings", {"kind": "portion",
                    "values": {"food_query": "夹具B", "unit": "个", "eat_weight": 75}})], mid="portion-personal")
                reused, model = send(store, "再记两个夹具B", [call("record_meal", {"finish_after_tools": True,
                    "items": [{"food_query": "夹具B", "quantity": 2, "unit": "个"}]})], mid="portion-other-chat", chat="other")
                saved = store.intake[-1]
                assert len(model.requests) == 1 and saved["grams"] == 150
                assert saved["snapshot"]["basis"] == "confirmed_personal_portion"
                assert saved["snapshot"]["estimated"] and url not in reused["reply"]
                assert requests == ["search", "fetch"]


def TE06():
    """A product URL can precede nutrition lookup, and cached proof spends no web requests."""
    store = setup()
    store.foods[1].update(units=[], detail_loaded=True)
    url = "https://example.org/product"
    quote = "每盒夹具B的可食部分为75克"
    requests = []

    def provider(request):
        requests.append(request.url.path.rsplit("/", 1)[-1])
        return httpx.Response(200, json={"title": "夹具B规格", "content": quote})

    def fact(messages, ident):
        return json.loads(next(m.content for m in messages if isinstance(m, ToolMessage) and m.tool_call_id == ident))

    def record(messages, *, pending=False):
        page, = fact(messages, "web")["data"]["portion_web"]["pages"]
        evidence = {"ref": page["ref"], "quote": quote, "source_food": "夹具B", "source_unit": "盒",
                    "applicability": "同一食物的每盒可食克重", "basis": "edible_unit", "weight": 75, "weight_unit": "g"}
        item = {"food_query": "夹具B", "quantity": 2, "unit": "盒", "portion_evidence": evidence}
        if pending:
            item["pending_id"] = fact(messages, "miss")["data"]["unfinished"][0]["pending_id"]
        return call("record_meal", {"finish_after_tools": True, "items": [item]}, "complete")

    def cached(messages):
        item, = fact(messages, "miss")["data"]["unfinished"]
        cache, = item["portion_cache_refs"]
        return call("query_food_nutrition", {"portion_web": {"operation": "read_cache", "ref": cache["ref"]}}, "web")

    with patch.dict("os.environ", {"TRACE_ENABLED": "false", "DEEPSEEK_SEARCH_API_KEY": "fixture"}), redirect_stdout(io.StringIO()), \
            httpx.Client(transport=httpx.MockTransport(provider)) as client, patch("agent.storage.deepseek_web._client", return_value=client):
        result, model = send(store, "记夹具B两盒，这是产品资料 " + url, [call("query_food_nutrition", {
            "portion_web": {"operation": "fetch", "urls": [url]}}, "web"), record], mid="portion-direct")
        assert len(model.requests) == 2 and result["reply_source"] == "tool_facts" and requests == ["fetch"]
        assert [item["grams"] for item in store.intake] == [150] and url in result["reply"]
        result, _ = send(store, "再记夹具B两盒", [call("record_meal", {"items": [
            {"food_query": "夹具B", "quantity": 2, "unit": "盒"}]}, "miss"), cached,
            lambda messages: record(messages, pending=True)], mid="portion-cached", chat="other")
        assert result["tool_results"][1]["data"]["portion_web"]["requests_remaining"] == 3
        assert requests == ["fetch"] and [item["grams"] for item in store.intake] == [150, 150]
        denied, _ = send(store, "资料 http://127.0.0.1/product", [call("query_food_nutrition", {"portion_web": {
            "operation": "fetch", "urls": ["http://127.0.0.1/product", "https://example.org/not-in-request"]}}),
            AIMessage(content="这两个地址均未读取。")], mid="portion-direct-denied")
        web = denied["tool_results"][0]["data"]["portion_web"]
        assert web["status"] == "not_applicable" and web["requests_remaining"] == 3 and requests == ["fetch"]


def TE07():
    """Search reformulation and fetch share one actual HTTP budget across failed calls."""
    store = setup()
    url = "https://example.org/reformulated"
    requests = []

    def provider(request):
        body = json.loads(request.content)
        requests.append((request.url.path.rsplit("/", 1)[-1], body))
        if len(requests) == 1:
            return httpx.Response(200, json={"results": []})
        if len(requests) == 2:
            return httpx.Response(200, json={"results": [{"url": url}]})
        raise httpx.ReadTimeout("fixture timeout", request=request)

    def web(params, cid):
        return call("query_food_nutrition", {"portion_web": params}, cid).tool_calls[0]

    steps = [AIMessage(content="", tool_calls=[web({"operation": "search", "query": "  食物 可食  "}, "first")]),
             AIMessage(content="", tool_calls=[web({"operation": "search", "query": "食物   可食"}, "duplicate"),
                 web({"operation": "search", "query": "食物 规格"}, "reformulated"),
                 web({"operation": "fetch", "urls": [url]}, "fetch"),
                 web({"operation": "search", "query": "第三种搜索"}, "over-budget")]),
             AIMessage(content="这次未取得可核对的份量。")]
    with patch.dict("os.environ", {"TRACE_ENABLED": "false", "DEEPSEEK_SEARCH_API_KEY": "fixture"}), redirect_stdout(io.StringIO()), \
            httpx.Client(transport=httpx.MockTransport(provider)) as client, patch("agent.storage.deepseek_web._client", return_value=client):
        result, _ = send(store, "看看这种食物可食份量", steps, mid="portion-web-cap")
        facts = [fact["data"]["portion_web"] for fact in result["tool_results"]]
        assert [(fact["status"], fact["requests_remaining"]) for fact in facts] == [
            ("no_results", 2), ("repeated_request", 2), ("located", 1), ("service_error", 0), ("budget_exhausted", 0)]
        assert [kind for kind, _ in requests] == ["search", "search", "fetch"]
        replay, model = send(store, "看看这种食物可食份量", [], mid="portion-web-cap")
        assert replay["reply"] == result["reply"] and not model.requests and len(requests) == 3

    interrupted = []

    def lost(request):
        interrupted.append(request.url.path)
        raise RuntimeError("fixture interruption after sending")

    message = {"message_id": "portion-web-recovery", "user_id": "user", "chat_id": "test", "text": "另查一份资料"}
    with patch.dict("os.environ", {"TRACE_ENABLED": "false", "DEEPSEEK_SEARCH_API_KEY": "fixture"}), redirect_stdout(io.StringIO()), \
            httpx.Client(transport=httpx.MockTransport(lost)) as client, patch("agent.storage.deepseek_web._client", return_value=client):
        attempt = run(message, store=store, config=CONFIG, model=NativeFake(responses=steps[:1]))
        assert attempt["status"] == "error" and not attempt["terminal"]
        recovered = run(message, store=store, config=CONFIG,
                        model=NativeFake(responses=[AIMessage(content="网页请求中断，尚未核实份量。")]))
        web = recovered["tool_results"][0]["data"]["portion_web"]
        assert recovered["status"] == "ok" and web["reason"] == "lookup_interrupted" and web["requests_remaining"] == 2
        assert len(interrupted) == 1


def TE08():
    """A wrapped serving row retains its edible scope, and a saved size stays scoped."""
    from types import SimpleNamespace
    from agent.replies import add_recorded_portion_sources
    from agent.tools.portions import personal_portion, personal_unit, resolve_web_portion

    apple = {"food_id": "apple", "name": "苹果", "aliases": ["Apple"], "type": "生"}
    row = "Apple\n1 large\n\n(242 g/\n\n8 oz)"
    caption = "Raw fruit. Edible Weight Portion"
    page = {"ref": "apple-page", "status": "read", "url": "https://example.org/fruit", "title": "Fruit",
            "fetched_at": "2026-11-12T12:00:00+00:00",
            "text": f"{caption}\n\nServing\nSize | Calories\n---|---\n{row}\n | 130"}
    proof = {"ref": page["ref"], "quote": row + " | 130", "source_food": "Apple", "source_unit": "large",
             "weight": 242, "weight_unit": "g", "basis": "edible_unit"}
    rt = SimpleNamespace(journal={"calls": [{"result": {"data": {"portion_web": {"pages": [page]}}}}]})
    grams, evidence = resolve_web_portion(rt, apple, {"unit": "个（大）"}, 1, proof)
    assert grams == 242 and evidence["table_header"] == caption and evidence["quote"] == row + "\n | 130"
    written = {"tool": "record_meal", "status": "ok", "data": {"items": [
        {"matched_name": apple["name"], "snapshot": {"portion_evidence": evidence}}]}}
    adjustments = []
    reply = add_recorded_portion_sources("已记早餐。", [written, written], adjustments)
    assert reply.count(page["url"]) == 1 and adjustments[0]["urls"] == [page["url"]]
    assert add_recorded_portion_sources(reply, [written], []) == reply
    assert add_recorded_portion_sources("待确认。", [{**written, "status": "pending"}], []) == "待确认。"
    for text, wrong in [(page["text"], {**proof, "weight": 130}),
                        (page["text"].replace(caption, "Package weight only"), proof)]:
        bad_page = {**page, "text": text}
        rt.journal["calls"][0]["result"]["data"]["portion_web"]["pages"] = [bad_page]
        try:
            resolve_web_portion(rt, apple, {"unit": "个（大）"}, 1, wrong)
        except ValueError:
            pass
        else:
            raise AssertionError("unverified edible serving was accepted")
    unit = personal_unit(apple, "个", "大苹果")
    settings = {"portions": {f"apple:{unit}": {"eat_weight": 220}}}
    assert personal_portion(settings, apple, "个（大）")["eat_weight"] == 220
    assert personal_portion(settings, apple, "个", "大苹果")["eat_weight"] == 220
    assert personal_portion(settings, apple, "个（小）") is None
    assert personal_portion(settings, apple, "个") is None


CHECKS = {"TE01": TE01, "TE02": TE02, "TE03": TE03, "TE04": TE04, "TE05": TE05, "TE06": TE06, "TE07": TE07, "TE08": TE08}
