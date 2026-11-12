"""Regressions for stale conversation facts and explicit eval selection."""
from copy import deepcopy
from dataclasses import replace
import contextlib
import io
import json
import subprocess
from unittest.mock import patch

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_to_dict, messages_from_dict
from agent import run
from harness.eval.personal_cases import CONFIG, NativeFake, call, setup


def old_history():
    return [
        HumanMessage(content="昨天4条晚餐合计525.6千卡", additional_kwargs={"lc_source": "summarization"}),
        HumanMessage(content="我偏好清淡的做法。"), AIMessage(content="可以优先蒸煮。"),
        HumanMessage(content="昨天晚餐多少热量？"),
        AIMessage(content="", tool_calls=[{"id": "old-query", "name": "query_history", "args": {"period": "yesterday"}, "type": "tool_call"}],
                  additional_kwargs={"reasoning_content": "旧账本为525.6千卡"}),
        ToolMessage(content=json.dumps({"tool": "query_history", "call_id": "old-query", "status": "ok", "data": {
            "count": 4, "total": {"kcal": 525.6}, "items": [{"record_id": "original-row", "food_name": "原餐食", "date": "2026-11-01", "nutrition": {"kcal": 525.6}}]}}), tool_call_id="old-query"),
        AIMessage(content="昨天有4条晚餐记录，合计525.6千卡。"),
        HumanMessage(content="米饭能与蔬菜搭配吗？", id="calorie-user:old-knowledge-msg"), call("query_knowledge", {"query": "主食搭配"}, "old-knowledge"),
        ToolMessage(content=json.dumps({"tool": "query_knowledge", "status": "ok", "data": {"evidence": "谷类与蔬菜搭配", "entries": []}}), tool_call_id="old-knowledge"),
        AIMessage(content="可以与蔬菜搭配。"),
    ]


def respond(messages):
    # Deliberately reproduce the observed copying behavior when the old value
    # reaches the provider. Expected ledger values are independent fixtures.
    wire = json.dumps(messages_to_dict(messages), ensure_ascii=False)
    if "525.6" in wire:
        return AIMessage(content="今天0千卡，昨天4条晚餐525.6千卡。")
    return AIMessage(content="今天合计0千卡。")


def CX01():
    """迁移/清理后的旧账本、回执、reasoning与旧摘要不进入新轮；引用与普通对话保留。"""
    from harness.eval.production_store_cases import Objects
    from agent.storage.cos_store import CosStore
    from agent.replies import project_messages

    history = old_history()
    assert "525.6" in respond(project_messages(history)).content
    for real in (False, True):
        objects = Objects() if real else None
        store = CosStore(objects, "regression/history") if real else setup()
        store.save_session("user", "test", {"messages": messages_to_dict(history), "refs": ["original-row"],
                          "last_message_id": "old-knowledge-msg", "last_meal": ["original-row"], "unfinished": {"pending": {"food_query": "未完食物", "date": "2026-11-01"}}})
        from agent.storage.execution_pipeline import receipt_id
        store.save_document("receipt_state", "system", receipt_id("feishu", "old-knowledge-msg"), {"status": "completed", "delivery": {"presented": True}, "result": {"status": "ok"}})
        archived = {"messages": messages_to_dict(history), "calls": [{"result": {"data": {"total": {"kcal": 525.6}}}}]}
        task = store.begin_execution({"message_id": "old-audit", "user_id": "user", "chat_id": "test", "text": "旧查询"}, archived)
        store.save_execution(task, archived, completed=True)
        if real:
            store = CosStore(objects, "regression/history")
        model = NativeFake(responses=[call("query_today", {}), respond])
        result = run({"message_id": "fresh", "user_id": "user", "chat_id": "test", "text": "请只查今天的热量。"},
                     store=store, config=replace(CONFIG, preset="live") if real else CONFIG,
                     intake_date="2026-11-02", model=model)
        assert result["status"] == "ok", result["errors"]
        assert result["reply"] == "今天合计0千卡。"
        assert result["tool_results"][0]["data"]["total"] == {"kcal": 0}
        wire = json.dumps(messages_to_dict(model.requests[0]), ensure_ascii=False)
        assert "525.6" not in wire and "original-row" in wire
        assert "清淡" in wire and "谷类与蔬菜搭配" in wire
        session = store.load_session("user", "test")
        assert session["unfinished"]["pending"]["date"] == "2026-11-01"
        old = store.load_execution({"message_id": "old-audit", "user_id": "user", "chat_id": "test", "text": "旧查询"})
        assert old["payload"]["calls"][0]["result"]["data"]["total"]["kcal"] == 525.6
    assert "525.6" in history[6].content
    # Legacy replies may have lost their paired tool exchange, or may themselves
    # be the earlier unsupported reply. They must not seed another stale claim.
    for legacy in ([HumanMessage(content="昨天晚餐多少热量？"), AIMessage(content="昨天4条晚餐525.6千卡。")],
                   [HumanMessage(content="昨天4条晚餐525.6千卡。", additional_kwargs={"lc_source": "summarization"})]):
        store = setup()
        store.save_session("user", "test", {"messages": messages_to_dict(legacy)})
        result = run({"message_id": "legacy", "user_id": "user", "chat_id": "test", "text": "今天多少热量？"},
                     store=store, config=CONFIG, model=NativeFake(responses=[call("query_today", {}), respond]))
        assert result["reply"] == "今天合计0千卡。"
    # New ordinary dialogue remains usable for the next turn's references.
    store = setup()
    message = {"message_id": "chat", "user_id": "user", "chat_id": "test", "text": "给我一个清淡做法。"}
    delivered = run(message, store=store, config=CONFIG, model=NativeFake(responses=[AIMessage(content="可以优先蒸煮。")]))
    from agent.graph import mark_reply_presented
    mark_reply_presented(store, "user", "test", delivered)
    model = NativeFake(responses=[AIMessage(content="可以沿用刚才的做法。")])
    run({**message, "message_id": "chat-followup", "text": "刚才那个呢？"}, store=store, config=CONFIG, model=model)
    history = [json.loads(m.content.split("：", 1)[1]) for m in model.requests[0]
               if m.additional_kwargs.get("history_turn_id")]
    assert any({"role": "assistant", "content": "可以优先蒸煮。"} in turn["dialogue"] for turn in history)


def CX02():
    """旧未完成执行恢复只清理前序历史；本轮已存结果、摘要压缩和下一轮继续有效。"""
    store = setup()
    history = old_history()
    history[1] = HumanMessage(content="我偏好清淡的做法。" * 200)
    session = {"messages": messages_to_dict(history), "refs": ["original-row"], "last_meal": ["original-row"]}
    store.save_session("user", "test", session)
    message = {"message_id": "resume", "user_id": "user", "chat_id": "test", "text": "请查今天热量。"}
    native = call("query_today", {}, "current-call")
    fact = {"tool": "query_today", "call_id": "current-call", "action_id": "resume:agent:1", "status": "ok",
            "data": {"date": "2026-11-02", "count": 0, "total": {"kcal": 0}, "items": [], "detail": "calories"}}
    payload = {"message": message, "day": "2026-11-02", "session": session, "settings": {}, "model_calls": 1,
               "calls": [{"call_id": "current-call", "tool": "query_today", "args": {}, "index": 1, "action_id": "resume:agent:1", "result": deepcopy(fact)}],
               "model_requests": [], "messages": messages_to_dict(history + [HumanMessage(content=message["text"]), native,
                   ToolMessage(content=json.dumps(fact), tool_call_id="current-call")])}
    store.begin_execution(message, payload)
    summary = NativeFake(responses=[AIMessage(content="用户偏好清淡，曾查询餐食。")])
    model = NativeFake(responses=[respond])
    with patch.object(store, "today_summary", side_effect=AssertionError("saved tool result must replay")):
        result = run(message, store=store, config=replace(CONFIG, input_target_tokens=5000),
                     model=model)
    assert result["status"] == "ok", result["errors"]
    assert result["reply"] == "今天合计0千卡。"
    assert result["tool_results"] == [fact] and result["model_calls"] == 2
    assert not summary.requests
    assert all("525.6" not in json.dumps(messages_to_dict(ms), ensure_ascii=False) for ms in summary.requests)
    saved = store.load_session("user", "test")
    from agent.storage.conversation import history as clean_history
    clean, baseline = clean_history(store, "user", "test")
    assert "525.6" not in json.dumps(messages_to_dict(clean), ensure_ascii=False)
    next_model = NativeFake(responses=[call("query_today", {}), respond])
    continued = run({**message, "message_id": "next", "text": "再看看今天热量。"}, store=store, config=CONFIG,
                    intake_date="2026-11-02", model=next_model)
    assert continued["reply"] == "今天合计0千卡。"
    assert "清淡" in json.dumps(messages_to_dict(next_model.requests[0]), ensure_ascii=False)
    # Restore an already-compressed legacy execution without its original Human.
    store = setup()
    collapsed = deepcopy(payload)
    native = native.model_copy(update={"additional_kwargs": {"reasoning_content": "当前工具返回0千卡"}})
    collapsed["messages"] = messages_to_dict([old_history()[0], native, ToolMessage(content=json.dumps(fact), tool_call_id="current-call")])
    from agent.graph import _execution_messages
    exchange = _execution_messages(collapsed, message, "calorie-user:resume")
    assert next(m for m in exchange if isinstance(m, AIMessage)).additional_kwargs == native.additional_kwargs
    store.begin_execution(message, collapsed)
    with patch.object(store, "today_summary", side_effect=AssertionError("saved tool result must replay")):
        restored = run(message, store=store, config=CONFIG, model=NativeFake(responses=[respond]))
    assert restored["status"] == "ok" and restored["reply"] == "今天合计0千卡。", restored
    assert restored["tool_results"] == [fact]
    # A long active turn is never summarized, even after a tool has returned.
    store = setup()
    long_message = {**message, "message_id": "long-active", "text": "请只查今天已记录的热量。" * 200}
    class Death(BaseException): pass
    def die(_): raise Death()
    model = NativeFake(responses=[call("query_today", {}), die])
    summary = NativeFake(responses=[AIMessage(content="不应压缩当前消息")])
    config = replace(CONFIG, input_target_tokens=5000)
    try:
        run(long_message, store=store, config=config, intake_date="2026-11-02", model=model)
    except Death:
        pass
    else:
        raise AssertionError("model interruption was not reached")
    durable = store.load_execution(long_message)["payload"]
    assert any(m["type"] == "human" and m["data"]["content"] == long_message["text"] for m in durable["messages"])
    assert not summary.requests and durable["model_calls"] == 2
    with patch.object(store, "today_summary", side_effect=AssertionError("saved tool result must replay")):
        restored = run(long_message, store=store, config=config, model=NativeFake(responses=[respond]))
    assert restored["status"] == "ok" and restored["reply"] == "今天合计0千卡。", restored
    assert restored["model_calls"] == 3 and not summary.requests


def RG01():
    """评测精确选择、错误整批拒绝、真实证据独立保存且不覆盖、控制台范围一致。"""
    from harness.eval import runner
    from console import server
    executed = []
    checks = {"E05": lambda: executed.append("E05"), "P01": lambda: executed.append("P01")}
    def invoke(args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return runner.main(args)
    with patch.object(runner, "load_checks", return_value=checks):
        assert invoke([]) == 0 and not executed
        assert invoke(["--list"]) == 0 and not executed
        assert invoke(["--case", " E05,P01,E05 "]) == 0
        assert executed == ["E05", "P01"]
        executed.clear()
        for args in (["--case", "E05,TYPO"], ["--case", ""], ["--case", "E05,"], ["--smoke"],
                     ["--real-model", "--all"], ["--real-model", "--case", "P01"],
                     ["--output", "unused.json"]):
            try: invoke(args)
            except SystemExit as exc: assert exc.code == 2
            else: raise AssertionError(f"invalid selection accepted: {args}")
        assert not executed
        assert invoke(["--all"]) == 0 and executed == ["E05", "P01"]
    import tempfile
    from pathlib import Path
    from harness.eval import conversation_cases
    previous_mode = conversation_cases.REAL
    def real_case():
        assert conversation_cases.REAL
        executed.append("real")
        conversation_cases.TRANSCRIPT.append({"case": "E05"})
    with tempfile.TemporaryDirectory() as directory, patch.object(runner, "ROOT", Path(directory)), \
            patch.object(runner, "load_checks", return_value={"E05": real_case}):
        legacy = Path(directory) / "reports/conversation-real.json"
        legacy.parent.mkdir()
        legacy.write_text("original evidence", encoding="utf-8")
        for _ in range(2):
            assert invoke(["--real-model", "--case", "E05"]) == 0
        outputs = list(legacy.parent.glob("conversation-real-*.json"))
        assert len(outputs) == 2 and legacy.read_text(encoding="utf-8") == "original evidence"
        assert all(json.loads(path.read_text(encoding="utf-8")) == [{"case": "E05"}] for path in outputs)
        explicit = Path(directory) / "chosen.json"
        assert invoke(["--real-model", "--case", "E05", "--output", str(explicit)]) == 0
        before = list(executed)
        for path in (explicit, legacy):
            try: invoke(["--real-model", "--case", "E05", "--output", str(path)])
            except SystemExit as exc: assert exc.code == 2
            else: raise AssertionError("existing evidence was overwritten")
        assert executed == before and legacy.read_text(encoding="utf-8") == "original evidence"
        def failed_case():
            real_case()
            raise AssertionError("record this failure")
        failed = Path(directory) / "failed.json"
        with patch.object(runner, "load_checks", return_value={"E05": failed_case}):
            assert invoke(["--real-model", "--case", "E05", "--output", str(failed)]) == 1
        assert json.loads(failed.read_text(encoding="utf-8")) == [{"case": "E05"}]
        assert conversation_cases.REAL == previous_mode
    calls = []
    def process(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="selected", stderr="")
    with patch.object(server.subprocess, "run", side_effect=process):
        server.eval_run(scope="list", case="")
        server.eval_run(scope="case", case="E05,P01")
        server.eval_run(scope="full", case="")
        assert server.eval_run(scope="unknown", case="").status_code == 400
    assert calls[0][-1] == "--list" and calls[1][-2:] == ["--case", "E05,P01"] and calls[2][-1] == "--all"


def CX03():
    """原任务恢复核对当前授权/待办；提交待办进度保留新会话，提案绑定目标版本。"""
    from agent.graph import mark_reply_presented
    from harness.eval.production_store_cases import open_store
    from harness.eval.personal_cases import A, B

    config = replace(CONFIG, preset="live")
    def invoke(store, mid, steps, text="execute", chat="test"):
        return run({"message_id": mid, "user_id": "user", "chat_id": chat, "text": text},
                   store=store, config=config, model=NativeFake(responses=steps))
    done = AIMessage(content="已处理本次请求。")

    store = open_store()
    proposal = invoke(store, "offer", [call("update_user_settings", {"mode": "propose", "kind": "targets", "values": {"kcal": 1900}}), done])
    mark_reply_presented(store, "user", "test", proposal)
    save = store.save_document
    def fail_settings(kind, *args, **kwargs):
        if kind == "user_settings": raise ConnectionError("before settings commit")
        return save(kind, *args, **kwargs)
    with patch.object(store, "save_document", side_effect=fail_settings):
        first = invoke(store, "confirm", [call("update_user_settings", {"mode": "confirm", "kind": "targets", "proposal_id": proposal["proposal_id"], "authorization_quote": "ok"})], text="ok")
    assert first["status"] == "error"
    invoke(store, "cancel", [done], text="取消")
    store = open_store(store.objects)
    replay = invoke(store, "confirm", [done], text="ok")
    assert replay["tool_results"][0]["status"] == "pending"
    assert store.get_standard("user")["kcal"] != 1900

    for later in ("completed", "cancelled", "read", "legacy", "commit-progress"):
        store = open_store()
        invoke(store, "need-weight", [call("record_meal", {"items": [{"food_query": A["name"]}]}), done])
        pid = next(iter(store.load_session("user", "test")["unfinished"]))
        args = {"items": [{"food_query": A["name"], "pending_id": pid, "grams": 100}]}
        if later == "commit-progress":
            offer = invoke(store, "offer-completion", [call("record_meal", {**args, "mode": "propose"}), done])
            mark_reply_presented(store, "user", "test", offer)
            save_session = store.save_session
            failures = []
            def fail_progress(*args, **kwargs):
                if not failures:
                    failures.append(True)
                    raise ConnectionError("after tool result, before atomic commit")
                return save_session(*args, **kwargs)
            with patch.object(store, "save_session", side_effect=fail_progress):
                first = invoke(store, "old-completion", [call("record_meal", {"mode": "confirm", "proposal_id": offer["proposal_id"], "authorization_quote": "ok"})], text="ok")
            assert not first["tool_results"]
        else:
            with patch.object(store, "write_intake", side_effect=ConnectionError("before intake commit")):
                first = invoke(store, "old-completion", [call("record_meal", args)])
        assert first["status"] == "error" and not store.intake_items("user")
        if later == "legacy":
            task = store.load_execution({"message_id": "old-completion"})
            task["payload"]["calls"][0]["prepared"].pop("pending_before")
            store.save_execution(task, task["payload"])
        elif later == "completed":
            invoke(store, "new-completion", [call("record_meal", {"items": [{"food_query": A["name"], "pending_id": pid, "grams": 150}]}), done])
        elif later == "cancelled":
            invoke(store, "new-cancel", [call("record_meal", {"cancel_pending": [pid]}), done], text="取消")
        elif later == "read":
            invoke(store, "new-read", [call("query_today", {}), done])
        current = store.load_session("user", "test")
        restored = open_store(store.objects)
        result = invoke(restored, "old-completion", [done])
        grams = [row["grams"] for row in restored.intake_items("user")]
        assert grams == ([150] if later == "completed" else [100] if later in {"read", "commit-progress"} else []), (later, grams)
        if later in {"read", "commit-progress"}:
            assert result["status"] == "ok"
            session = restored.load_session("user", "test")
            assert not session["unfinished"]
            if later == "read":
                assert session["messages"] == current["messages"] and session["last_message_id"] == "new-read"
                assert session["last_meal"] == [row["record_id"] for row in restored.intake_items("user")]
        else:
            assert result["status"] == "error", (later, result)
        if later == "completed":
            # A newer task consumes the same logical pending item only once.
            assert restored.load_session("user", "test") == current

    for changed in (False, True, "legacy"):
        store = open_store()
        invoke(store, "meal", [call("record_meal", {"items": [{"food_query": B["name"], "quantity": 2, "unit": "个"}]}), done])
        offer = invoke(store, "halve", [call("revise_intake", {"mode": "propose", "ratio": .5}), done])
        assert offer["tool_results"][0]["data"]["parameters"]["grams"] == 50
        assert "50" in offer["reply"]
        mark_reply_presented(store, "user", "test", offer)
        if changed == "legacy":
            session = store.load_session("user", "test")
            session["proposal"].pop("target_version")
            store.save_session("user", "test", session)
        elif changed:
            def revise(messages):
                fact = json.loads(next(m for m in reversed(messages) if isinstance(m, ToolMessage)).content)
                return call("revise_intake", {"record_id": fact["data"]["items"][0]["record_id"], "grams": 300}, "c2")
            invoke(store, "other-chat", [call("query_history", {"period": "today", "detail": "items"}), revise, done], chat="other")
        result = invoke(store, "accept-halving", [call("revise_intake", {"mode": "confirm", "proposal_id": offer["proposal_id"], "authorization_quote": "ok"}), done], text="ok")
        assert result["tool_results"][0]["status"] == ("pending" if changed else "ok")
        assert store.intake_items("user")[0]["grams"] == (300 if changed is True else 100 if changed == "legacy" else 50)
        if changed is False:
            snapshot = store.intake_items("user")[0]["snapshot"]
            assert snapshot["estimated"] is True and snapshot["basis"] == "boohee_eat_weight"


def CX04():
    """参数校验错误回到原生循环，恢复路径同样纠错且不重放错误或成功写入。"""
    from harness.eval.production_store_cases import open_store
    from harness.eval.personal_cases import A
    config = replace(CONFIG, preset="live")
    for interrupted in (False, True):
        store = open_store()
        message = {"message_id": "invalid-then-corrected", "user_id": "user", "chat_id": "test", "text": "记录食物"}
        invalid = call("record_meal", {"items": [{"food_query": A["name"], "grams": 0}]})
        corrected = call("record_meal", {"items": [{"food_query": A["name"], "grams": 100}]}, "corrected")
        class Death(BaseException): pass
        def die(_): raise Death()
        model = NativeFake(responses=[invalid, die] if interrupted else [invalid, corrected, AIMessage(content="已记录，60千卡。")])
        if interrupted:
            try:
                run(message, store=store, config=config, model=model)
            except Death:
                pass
            else:
                raise AssertionError("interruption not reached")
            assert not store.intake_items("user")
            store = open_store(store.objects)
            model = NativeFake(responses=[corrected, AIMessage(content="已记录，60千卡。")])
        result = run(message, store=store, config=config, model=model)
        assert result["status"] == "ok", result["errors"]
        assert [fact["status"] for fact in result["tool_results"]] == ["error", "ok"]
        assert "invalid_tool_arguments" in result["tool_results"][0]["error"]
        assert len(store.intake_items("user")) == 1 and store.intake_items("user")[0]["nutrition"]["kcal"] == 60
        replay_model = NativeFake()
        replay = run(message, store=store, config=config, model=replay_model)
        assert not replay_model.requests and replay["reply"] == result["reply"]
        assert len(store.intake_items("user")) == 1


CHECKS = {"CX01": CX01, "CX02": CX02, "CX03": CX03, "CX04": CX04, "RG01": RG01}
