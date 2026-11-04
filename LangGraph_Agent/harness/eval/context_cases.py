"""Context governance failures use independent facts and injected transports."""
from copy import deepcopy
from dataclasses import replace
import json
import threading
from unittest.mock import patch

import httpx
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage, messages_to_dict
from agent import run
from agent.context import tokens, request_body, measure_request, InputBudgetExceeded, TruncatedGeneration, check_request
from agent.graph import mark_reply_presented
from agent.storage import conversation as cx
from agent.storage.cos_journal import Journal
from harness.eval.personal_cases import CONFIG, NativeFake, setup, call
from harness.eval.production_store_cases import open_store, Objects


def seeded(store, config, count=7):
    for index in range(count):
        ident = "seed:" + str(index)
        cx.stage_turn(store, "user", "context", ident, [HumanMessage(content=f"话题{index}：我偏好清淡，周三希望方便准备。", id=ident), AIMessage(content="可以蒸煮搭配。", additional_kwargs={"fact_scope": "dialogue"})])
        cx.confirm_turn(store, "user", "context", ident, config)
    return store.load_document("summary_request", "user", "context")


def CX05():
    """最终HTTP全字段计量、真实max_tokens、截断拒绝及硬超限终结后继续。"""
    from agent.model import DeepSeekModel
    import agent.model as transport
    config = replace(CONFIG, context_window=20000)
    basic = request_body(config.model, [SystemMessage(content="规则"), HumanMessage(content="问题")], [{"type": "function", "function": {"name": "fact", "parameters": {"type": "object"}}}])
    before = measure_request(basic, config)
    for location in ("system", "current", "result", "reasoning", "tools"):
        body = deepcopy(basic)
        if location == "system": body["messages"][0]["content"] += "中文" * 10000
        elif location == "current": body["messages"][-1]["content"] += "中文" * 10000
        elif location == "result": body["messages"].append({"role": "tool", "content": "中文" * 10000, "tool_call_id": "fixed"})
        elif location == "reasoning": body["messages"].append({"role": "assistant", "content": "", "reasoning_content": "中" * 10000})
        else: body["tools"][0]["function"]["description"] = "中文" * 10000
        assert measure_request(body, config)["input_tokens"] > before["input_tokens"] + 1000
        try: check_request(body, replace(config, context_window=11000))
        except InputBudgetExceeded: pass
        else: raise AssertionError(location)
    requests = []
    def reply(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"finish_reason": "length", "message": {"content": "截断正文", "tool_calls": [{"id": "x", "function": {"name": "record_meal", "arguments": "{"}}]}}], "usage": {}})
    with httpx.Client(transport=httpx.MockTransport(reply)) as client, patch.object(transport, "_CLIENT", client), patch.dict("os.environ", {"DEEPSEEK_API_KEY": "fixture"}):
        try: DeepSeekModel().invoke([HumanMessage(content="问题")], tools=basic["tools"])
        except TruncatedGeneration: pass
        else: raise AssertionError("truncation was accepted")
    assert requests[0]["max_tokens"] == 8192 and requests[0]["tools"] == basic["tools"]
    store = open_store()
    config = replace(CONFIG, preset="live", context_window=23000)
    message = {"message_id": "hard-limit", "user_id": "user", "chat_id": "hard", "text": "巨大输入" * 30000}
    blocked = NativeFake()
    result = run(message, store=store, config=config, model=blocked)
    assert result["terminal"] and not blocked.requests and store.load_execution(message)["status"] == "completed"
    replay = run(message, store=store, config=config, model=NativeFake())
    assert replay["reply"] == result["reply"]
    smaller = run({**message, "message_id": "small", "text": "查今天热量"}, store=store, config=config, model=NativeFake(responses=[call("query_today", {}), AIMessage(content="今天0千卡。")]))
    assert smaller["status"] == "ok", smaller["errors"]
    # A proposal produced before the rejected next request must not survive as
    # an executable action in the terminal reply.
    proposal_store = open_store()
    offer = run({"message_id": "oversize-offer", "user_id": "user", "chat_id": "proposal", "text": "先建议偏好，等我确认。"}, store=proposal_store, config=config,
        model=NativeFake(responses=[call("update_user_settings", {"mode": "propose", "kind": "preferences", "values": {"taste": "清淡" * 3500}})]))
    assert offer["terminal"] and not offer["proposal_id"] and "回复确认" not in offer["reply"]
    mark_reply_presented(proposal_store, "user", "proposal", offer)
    confirm = run({"message_id": "confirm-after-limit", "user_id": "user", "chat_id": "proposal", "text": "确认"}, store=proposal_store, config=config,
        model=NativeFake(responses=[call("update_user_settings", {"mode": "confirm", "kind": "preferences", "values": {}}), AIMessage(content="原提案已终止，请重新说明。")]))
    assert not proposal_store.load_document("user_settings", "user").get("preferences")
    assert confirm["tool_results"][0]["status"] == "pending"
    # A confirmed prior effect survives a hard limit on its result.
    store = setup()
    from agent import graph
    original = graph.model_view
    def large_result(fact):
        value = original(fact)
        if fact.get("tool")=="record_meal":value["padding"]="额外结果"*30000
        return value
    committed_message={"message_id":"committed-before-limit","user_id":"user","chat_id":"facts","text":"记录夹具A200克"}
    with patch.object(graph,"model_view",side_effect=large_result):
        committed=run(committed_message,store=store,config=replace(CONFIG,context_window=23000),model=NativeFake(responses=[call("record_meal",{"items":[{"food_query":"夹具A","grams":200}]})]))
    assert committed["terminal"] and len(store.intake)==1 and store.intake[0]["nutrition"]["kcal"]==120
    assert "120" in committed["reply"]
    replay=run(committed_message,store=store,config=CONFIG,model=NativeFake())
    assert replay["reply"]==committed["reply"] and len(store.intake)==1
    # Exercise the actual Pipeline termination path without a paid provider.
    from agent.storage.execution_pipeline import Pipeline
    objects = Objects(); pipeline = Pipeline(objects, "context/hard")
    receipt = pipeline.accept({**message, "message_id": "p-hard", "text": "超额"}, source="feishu")
    terminal = {"status": "error", "terminal": True, "errors": ["input_budget_exceeded"], "reply": "请缩小范围"}
    event = {"receipt_id": receipt["receipt_id"], "ingress_revision": receipt["ingress_revision"], **{key: receipt["message"][key] for key in ("message_id", "user_id", "chat_id")}}
    with patch("agent.config.load_config", return_value=replace(CONFIG, preset="live")), patch("agent.storage.execution_pipeline.load_config", create=True, return_value=replace(CONFIG, preset="live")):
        state = pipeline.worker(event, execute=lambda *args, **kwargs: terminal, send=lambda *args, **kwargs: {"code": 0})
    assert state["status"] == "completed" and state["work_status"] == "terminated" and state["attempts"] == 1


def CX06():
    """连续前缀、迟到/重复候选、追加后缀、旧执行及应用中断不回滚会话。"""
    config = replace(CONFIG, summary_pending_tokens=1, summary_input_tokens=450)
    store = open_store(); request = seeded(store, config)
    progress = Journal(store.objects, store.prefix + "sync/")
    old = store.load_document("conversation_context", "user", "context")
    result = cx.run_background(store, progress, config=config, model=NativeFake(responses=[AIMessage(content="用户偏好清淡，周三方便准备。")]))
    candidate = result["candidate"]
    ids = candidate["turn_ids"]
    assert ids and ids == [turn["id"] for turn in old["turns"][:len(ids)]]
    cx.stage_turn(store, "user", "context", "later-correction", [HumanMessage(content="纠正：周三改为周四。", id="later-correction")])
    cx.confirm_turn(store, "user", "context", "later-correction", config)
    session = {"last_message_id": "later", "unfinished": {"pending": {"date": "2026-11-01", "food_query": "鱼"}}, "proposal": {"presented": True}}
    store.save_session("user", "context", session)
    saved = store.save_document
    def interrupted(kind, *args, **kwargs):
        if kind == "summary_request": raise ConnectionError("application interrupted before commit")
        return saved(kind, *args, **kwargs)
    with patch.object(store, "save_document", side_effect=interrupted):
        try: cx.apply_candidate(store, {"candidate": candidate}, config)
        except ConnectionError: pass
        else: raise AssertionError("interruption missing")
    fresh = open_store(store.objects)
    assert fresh.load_document("conversation_context", "user", "context")["summary"]["id"] == "root"
    applied = cx.apply_candidate(fresh, {"candidate": candidate}, config)
    assert applied["status"] == "applied"
    value = fresh.load_document("conversation_context", "user", "context")
    assert value["turns"][-1]["id"] == "later-correction" and value["summary"]["id"] == candidate["task_id"]
    assert fresh.load_session("user", "context") == session
    assert cx.apply_candidate(fresh, {"candidate": candidate}, config) == applied
    assert fresh.load_document("conversation_context", "user", "context") == value
    model = NativeFake(responses=[])
    resend = cx.run_background(fresh, progress, config=config, model=model)
    # Next task may exist; original successful task itself never recomputes.
    assert progress.get("summary:" + candidate["task_id"])["attempts"] == 1
    assert "input" not in progress.get("summary:" + candidate["task_id"])
    history, summary = cx.history(fresh, "user", "context")
    assert any("周四" in message.content for message in history)
    # Failure saving the final execution must not publish an undelivered reply.
    for legacy in (False, True):
        store = open_store()
        message = {"message_id": "legacy-active" if legacy else "final-save", "user_id": "user", "chat_id": "discussion", "text": "a discussion"}
        ident = "calorie-user:" + message["message_id"]
        if legacy:
            partial = {"messages": messages_to_dict([HumanMessage(content=message["text"], id=ident)]), "last_message_id": message["message_id"], "last_execution_complete": False}
            store.save_session("user", "discussion", partial)
            store.begin_execution(message, {"message": message, "session": partial, "settings": {}, "messages": partial["messages"], "calls": [], "model_calls": 0, "model_requests": []})
        else:
            save_execution = store.save_execution
            def fail_final(task, payload, **kwargs):
                if kwargs.get("completed"): raise OSError("final result save interrupted")
                return save_execution(task, payload, **kwargs)
            with patch.object(store, "save_execution", side_effect=fail_final):
                try: run(message, store=store, config=replace(CONFIG, preset="live"), model=NativeFake(responses=[AIMessage(content="FIRST_NOT_DELIVERED")]))
                except OSError: pass
                else: raise AssertionError("final save interruption missing")
            staged=cx.context(store, "user", "discussion")["turns"]
            assert len(staged)==1 and not staged[0]["complete"]
            assert "FIRST_NOT_DELIVERED" not in json.dumps(cx.history(store,"user","discussion")[0],default=str)
        fresh = open_store(store.objects)
        resumed_model = NativeFake(responses=[AIMessage(content="ACTUAL_DELIVERED_REPLY")])
        delivered = run(message, store=fresh, config=replace(CONFIG, preset="live"), model=resumed_model)
        mark_reply_presented(fresh, "user", "discussion", delivered)
        history, _ = cx.history(fresh, "user", "discussion")
        # The timely response was checkpointed before the final-result save.
        # Recovery reuses that response rather than purchasing another decision.
        expected = "ACTUAL_DELIVERED_REPLY" if legacy else "FIRST_NOT_DELIVERED"
        assert [message.content for message in history] == ["a discussion", expected]
        assert len(resumed_model.requests) == int(legacy)
        assert cx.context(fresh, "user", "discussion")["turns"][0]["complete"]
    store = open_store()
    old = {"message_id": "old-failed", "user_id": "user", "chat_id": "order", "text": "OLD: lunch on Wednesday"}
    save_execution = store.save_execution
    failed_once = []
    def interrupted_checkpoint(task, payload, **kwargs):
        if payload.get("model_calls") == 1 and not failed_once:
            failed_once.append(True)
            raise OSError("temporary storage failure before model HTTP")
        return save_execution(task, payload, **kwargs)
    with patch.object(store, "save_execution", side_effect=interrupted_checkpoint):
        failure = run(old, store=store, config=replace(CONFIG, preset="live"), model=NativeFake(responses=[]))
    assert failure["status"] == "error"
    mark_reply_presented(store, "user", "order", failure)
    assert not cx.context(store, "user", "order")["turns"][0]["complete"]
    correction = run({**old, "message_id": "new-correction", "text": "CORRECTION: Thursday instead"}, store=store, config=replace(CONFIG, preset="live"), model=NativeFake(responses=[AIMessage(content="THURSDAY_ACK")]))
    mark_reply_presented(store, "user", "order", correction)
    recovered = run(old, store=store, config=replace(CONFIG, preset="live"), model=NativeFake(responses=[AIMessage(content="OLD_RECOVERED")]))
    mark_reply_presented(store, "user", "order", recovered)
    clean, _ = cx.history(store, "user", "order")
    assert [message.content for message in clean] == [old["text"], "OLD_RECOVERED", "CORRECTION: Thursday instead", "THURSDAY_ACK"]
    assert store.load_session("user", "order")["last_message_id"] == "new-correction"
    # An interrupted manual recovery may have staged a new, unseen body. Until
    # delivery/recovery confirmation, projections may expose its user input only.
    store = open_store()
    cx.stage_turn(store, "user", "unseen", "calorie-user:manual", [HumanMessage(content="OLD_TOPIC", id="calorie-user:manual"), AIMessage(content="UNSEEN_MANUAL_REPLY", additional_kwargs={"fact_scope": "dialogue"})])
    visible, _ = cx.history(store, "user", "unseen")
    assert [message.content for message in visible] == ["OLD_TOPIC"]
    probe = NativeFake(responses=[AIMessage(content="NEXT_ACK")])
    run({"message_id": "next-after-manual", "user_id": "user", "chat_id": "unseen", "text": "NEXT_TOPIC"}, store=store, config=replace(CONFIG, preset="live"), model=probe)
    assert "UNSEEN_MANUAL_REPLY" not in json.dumps(messages_to_dict(probe.requests[0]), ensure_ascii=False)
    # An old execution-complete flag is not evidence of actual presentation.
    from agent.storage.execution_pipeline import receipt_id
    store = open_store()
    store.save_session("user", "legacy-display", {"messages": messages_to_dict([HumanMessage(content="old", id="calorie-user:waiting"), AIMessage(content="not displayed", additional_kwargs={"fact_scope": "dialogue"})]), "last_message_id": "waiting", "last_execution_complete": True})
    store.save_document("receipt_state", "system", receipt_id("dsh", "waiting"), {"status": "awaiting_presentation", "delivery": {"presented": False}, "result": {"status": "ok"}})
    assert not cx.context(store, "user", "legacy-display", write=True)["turns"][0]["complete"]
    mark_reply_presented(store, "user", "legacy-display", {"status": "ok", "dialogue_turn_id": "calorie-user:waiting"})
    assert cx.context(store, "user", "legacy-display")["turns"][0]["complete"]


def CX07():
    """失败最多两次并释放快照；无新轮次不重建；只重送未应用成功候选。"""
    config = replace(CONFIG, summary_pending_tokens=1)
    for bad in (AIMessage(content=""), AIMessage(content="过长" * 3000), AIMessage(content="不完整", response_metadata={"finish_reason": "length"}), RuntimeError("provider unavailable")):
        store = open_store(); seeded(store, config)
        progress = Journal(store.objects, store.prefix + "sync/")
        def response(messages):
            if isinstance(bad, Exception): raise bad
            return bad
        model = NativeFake(responses=[response, response])
        first = cx.run_background(store, progress, config=config, model=model)
        second = cx.run_background(store, progress, config=config, model=model)
        assert first["status"] == "retry" and second["status"] == "failed" and len(model.requests) == 2
        assert "input" not in progress.get("summary:" + second["task_id"])
        cx.apply_candidate(store, {"candidate": second["candidate"]}, config)
        assert cx.run_background(store, progress, config=config, model=model)["status"] == "idle"
        value = store.load_document("conversation_context", "user", "context")
        assert value["summary"]["id"] == "root" and len(value["turns"]) == 7
        cx.confirm_turn(store, "user", "context", "seed:6", config)
        assert not store.load_document("summary_active", "system")
        cx.stage_turn(store, "user", "context", "new", [HumanMessage(content="新的已完成对话", id="new")])
        cx.confirm_turn(store, "user", "context", "new", config)
        assert store.load_document("summary_active", "system")
    store = open_store(); seeded(store, config); progress = Journal(store.objects, store.prefix + "sync/")
    model = NativeFake(responses=[AIMessage(content="用户偏好清淡。")])
    first = cx.run_background(store, progress, config=config, model=model)
    second = cx.run_background(store, progress, config=config, model=model)
    assert first["candidate"] == second["candidate"] and len(model.requests) == 1
    assert cx.run_background(store, progress, config=config, model=model, remaining_seconds=24)["status"] == "skipped_budget"


def CX08():
    """阻塞后台不占消息执行位；裁剪不丢来源，控制事件不新增收件/用户轮次。"""
    config = replace(CONFIG, preset="live", summary_pending_tokens=1, input_target_tokens=4500)
    store = open_store(); seeded(store, config, 12)
    progress = Journal(store.objects, store.prefix + "sync/")
    entered, release = threading.Event(), threading.Event()
    outcome = []
    def blocked(messages):
        entered.set()
        assert release.wait(10)
        return AIMessage(content="用户偏好清淡。")
    def work(): outcome.append(cx.run_background(open_store(store.objects), progress, config=config, model=NativeFake(responses=[blocked])))
    thread = threading.Thread(target=work); thread.start()
    assert entered.wait(5)
    try:
        result = run({"message_id": "foreground", "user_id": "user", "chat_id": "context", "text": "查今天热量"}, store=open_store(store.objects), config=config, model=NativeFake(responses=[call("query_today", {}), AIMessage(content="今天0千卡。")]))
        assert result["status"] == "ok" and not release.is_set()
    finally:
        release.set(); thread.join(10)
    assert outcome and not thread.is_alive()
    from agent.storage.execution_pipeline import Pipeline
    pipeline = Pipeline(store.objects, store.prefix)
    before = pipeline.inbox(readonly=True).get("next_sequence", 0)
    current = pipeline.store().load_session("user", "context")
    applied = pipeline.worker({"type": "calorie_summary", "candidate": outcome[0]["candidate"]})
    assert applied["status"] == "applied"
    assert pipeline.inbox(readonly=True).get("next_sequence", 0) == before
    assert pipeline.store().load_session("user", "context") == current
    # A later front save/replay cannot replace independently stored context.
    pipeline.store().save_session("user", "context", {"last_message_id": "old"})
    assert pipeline.store().load_document("conversation_context", "user", "context")["summary"]["id"] == outcome[0]["task_id"]


def CX09():
    """维护错误阻止摘要，前置IO计入20秒计算且至少预留5秒。"""
    from agent.storage.execution_pipeline import Pipeline
    from agent.storage.timeutil import PROCESSING_DEADLINE
    pipeline = Pipeline(Objects(), "context/maintenance")
    receipt = pipeline.accept({"message_id": "retry", "user_id": "user", "chat_id": "retry", "text": "test"})
    pipeline.store().save_document("receipt_state", "system", receipt["receipt_id"], {"status": "retry", "attempts": 1, "error": "unresolved"})
    recovery = pipeline.recover(wake=lambda receipt: None)
    assert recovery["blocked"] == [receipt["receipt_id"]]
    import app
    class Deadline:
        time_limit_in_ms = 25000
    with patch.object(app, "configured_pipeline", return_value=pipeline), patch.object(pipeline, "recover", return_value=recovery), patch("agent.storage.cos_archive.archive_pending", return_value={"failed": []}), patch.object(cx, "run_background") as background:
        assert app.maintenance_handler({}, Deadline())["summary"]["status"] == "skipped_maintenance_error"
        assert not background.called
    config = replace(CONFIG, summary_pending_tokens=1)
    store = open_store(); seeded(store, config)
    progress = Journal(store.objects, store.prefix + "sync/")
    original = progress.commit; clock = [0.0]; observed = []
    def slow_initial_commit(value):
        result = original(value)
        if clock[0] == 0: clock[0] = 2.0
        return result
    def model(messages):
        observed.append((clock[0], PROCESSING_DEADLINE.get()))
        return AIMessage(content="用户偏好清淡。")
    token = PROCESSING_DEADLINE.set(25.0)
    try:
        with patch.object(cx.time, "monotonic", side_effect=lambda: clock[0]), patch.object(progress, "commit", side_effect=slow_initial_commit):
            assert cx.run_background(store, progress, config=config, model=NativeFake(responses=[model]), remaining_seconds=25)["status"] == "ready"
    finally: PROCESSING_DEADLINE.reset(token)
    assert observed == [(2.0, 20.0)], observed


def CX10():
    """Historical requests are grouped with real delivery state; current mixed exchanges and knowledge evidence stay intact."""
    from agent.context import select_messages, wire_messages, turns
    from agent.evidence import collect_visible_evidence
    from agent.storage.execution_pipeline import receipt_id
    store = open_store()
    config = replace(CONFIG, preset="live")
    message = {"message_id": "history-boundary", "user_id": "user", "chat_id": "boundary", "text": "记录夹具A200克和夹具B2个"}
    result = run(message, store=store, config=config, model=NativeFake(responses=[call("record_meal", {
        "items": [{"food_query": "夹具A", "grams": 200}, {"food_query": "夹具B", "quantity": 2, "unit": "个"}],
        "finish_after_tools": True})]))
    assert result["reply_source"] == "tool_facts"
    before_delivery, _ = cx.history(store, "user", "boundary", for_model=True)
    assert json.loads(before_delivery[0].content.split("：", 1)[1])["reply_status"] == "未知"
    mark_reply_presented(store, "user", "boundary", result)
    source_before = deepcopy(store.load_document("conversation_context", "user", "boundary"))
    revision = store.journal.revision
    projected, _ = cx.history(store, "user", "boundary", for_model=True)
    payload = json.loads(projected[0].content.split("：", 1)[1])
    assert isinstance(projected[0], SystemMessage) and payload["reply_status"] == "已回复"
    assert len(payload["references"]) == 2 and all("record_id" in ref for ref in payload["references"])
    assert payload["dialogue"][0]["content"] == message["text"]
    assert not any(isinstance(m, HumanMessage) for m in projected)
    assert "270" not in json.dumps(wire_messages(projected), ensure_ascii=False)
    assert store.journal.revision == revision and store.load_document("conversation_context", "user", "boundary") == source_before
    # Recovered legacy state must use its delivery receipt, never a guessed complete flag.
    legacy = open_store()
    legacy.save_session("user", "legacy-boundary", {"messages": messages_to_dict([
        HumanMessage(content="OLD_UNKNOWN", id="calorie-user:old-unknown"),
        AIMessage(content="旧营养999千卡", additional_kwargs={"fact_scope": "business"}),
        HumanMessage(content="OLD_DELIVERED", id="calorie-user:old-delivered")]), "last_message_id": "old-delivered"})
    legacy.save_document("receipt_state", "system", receipt_id("feishu", "old-delivered"), {
        "delivery": {"presented": True}, "result": {"status": "ok", "business_status": "none", "tool_results": []}})
    recovered, _ = cx.history(legacy, "user", "legacy-boundary", for_model=True)
    assert [json.loads(m.content.split("：", 1)[1])["reply_status"] for m in recovered] == ["未知", "已回复"]
    assert json.loads(recovered[1].content.split("：", 1)[1])["execution"] == {"business_status": "none", "tool_results": []}
    assert json.loads(recovered[0].content.split("：", 1)[1])["execution"]["business_status"] == "未知"
    assert "999" not in json.dumps(wire_messages(recovered), ensure_ascii=False)
    # Knowledge JSON stays separately visible and travels with its complete historical group.
    ident = "knowledge-boundary"
    evidence = {"tool": "query_knowledge", "status": "ok", "data": {"entries": [{
        "source_id": "K987654321", "evidence_id": "fixed-evidence", "answer": "保留原文42克。", "source": "原始出处"}]}}
    cx.stage_turn(store, "user", "boundary", ident, [HumanMessage(content="旧知识问题", id=ident),
        AIMessage(content=json.dumps(evidence, ensure_ascii=False), additional_kwargs={"fact_scope": "knowledge", "tool_evidence_origin": "query_knowledge"})])
    cx.confirm_turn(store, "user", "boundary", ident, config)
    history, _ = cx.history(store, "user", "boundary", for_model=True)
    assert collect_visible_evidence(history)[0]["answer"] == "保留原文42克。"
    current = [HumanMessage(content="记餐并修改上次菜单", id="current-original"),
        AIMessage(content="", tool_calls=[{"id": "fixed-current-call", "name": "record_meal", "args": {}, "type": "tool_call"}], additional_kwargs={"reasoning_content": "CURRENT_REASONING"}),
        ToolMessage(content='{"status":"ok"}', tool_call_id="fixed-current-call", name="record_meal")]
    selected, measurement = select_messages(history, {"text": ""}, current, "规则", {}, [], replace(config, recent_turns=1, input_target_tokens=1), config.model)
    assert len(turns(selected[:-len(current)])) == 1 and selected[-len(current):] == current
    assert collect_visible_evidence(selected)[0]["source"] == "原始出处"
    wire = wire_messages(selected)
    assert [m["content"] for m in wire if m["role"] == "user"] == ["记餐并修改上次菜单"]
    assert wire[-2]["tool_calls"][0]["id"] == wire[-1]["tool_call_id"] == "fixed-current-call"
    assert wire[-2]["reasoning_content"] == "CURRENT_REASONING" and measurement["parts"]["history"] > 0


CHECKS = {"CX05": CX05, "CX06": CX06, "CX07": CX07, "CX08": CX08, "CX09": CX09, "CX10": CX10}
