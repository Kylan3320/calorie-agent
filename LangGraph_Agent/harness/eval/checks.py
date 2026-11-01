"""Permanent contract regressions invoked by harness/eval/runner.py."""

from __future__ import annotations

import asyncio
import json
import io
import os
import tempfile
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from agent import FakeStore, run
from agent.config import Config
from agent.nodes.reply_guard_node import reply_guard
from agent.schemas import AgentPlan
from agent.storage import BitableStore
from agent.storage import bitable_adapter, boohee_client
from agent.tools.base import calculate_food_intake

CONFIG = Config("shadow_fakewrite", "deterministic", "", (), 8)


def guard_facts():
    """事实守卫拒绝输入数字、元数据及无写入成功声明"""
    assert not reply_guard({"final_reply": "已记录。", "tool_results": []})["guard_result"]["passed"]
    data = {"query": "9876 kcal", "entries": [{"answer": "按需安排饮食。"}], "evidence": "按需安排饮食。", "score": 9876}
    result = {"tool": "query_knowledge", "status": "ok", "data": data}
    assert not reply_guard({"final_reply": "9876 kcal", "tool_results": [result]})["guard_result"]["passed"]
    data["entries"][0]["answer"] = "9876 kcal"
    assert not reply_guard({"final_reply": "9876 kcal", "tool_results": [result]})["guard_result"]["passed"]
    assert not reply_guard({"final_reply": "100 kcal", "tool_results": []})["guard_result"]["passed"]
    record = {"tool": "record_food", "status": "ok", "data": {"matched_name": "米饭", "grams": 200,
              "nutrition": {"carbs": 51.8, "protein": 5.2, "fat": 0.6, "kcal": 233.4},
              "intake_write_result": {"status": "created", "created_count": 1}}}
    assert not reply_guard({"final_reply": "已记录米饭200g，热量51.8 kcal，蛋白质233.4g。", "tool_results": [record]})["guard_result"]["passed"]
    from agent.nodes.reply_composer_node import reply_composer
    rendered = reply_composer({"tool_results": [record]})["final_reply"]
    for metric in ("233.4 kcal", "碳水 51.8g", "蛋白质 5.2g", "脂肪 0.6g"):
        assert metric in rendered
    assert reply_guard({"final_reply": rendered, "tool_results": [record]})["guard_result"]["passed"]
    metadata = {"tool": "record_food", "status": "needs_confirmation", "data": {"score": 9876, "food_id": "9876"}}
    assert not reply_guard({"final_reply": "9876 kcal", "tool_results": [metadata]})["guard_result"]["passed"]
    write = {"tool": "record_food", "status": "ok", "data": {"intake_write_result": {"status": "created", "created_count": 0}}}
    assert not reply_guard({"final_reply": "已记录。", "tool_results": [write]})["guard_result"]["passed"]


def policy_and_readonly():
    """风险标签不可降级；只读预设保护摄入、食物库及 pending"""
    plan = AgentPlan.from_dict({"actions": [{"tool": "undo_last", "safety": "read_only"}]})
    assert plan.actions[0].safety == "destructive"
    store = FakeStore()
    msg = {"message_id": "first", "user_id": "u", "chat_id": "c", "text": "鸡蛋100g"}
    run(msg, store=store, config=CONFIG)
    msg = {**msg, "message_id": "undo", "text": "撤销上一条"}
    result = run(msg, store=store, config=CONFIG)
    assert result["tool_results"][0]["status"] == "needs_confirmation_plan"
    before = json.dumps([store.intake, store.foods, store.pending], sort_keys=True)
    for i, text in enumerate(["确认", "取消", "米饭200g"]):
        run({**msg, "message_id": f"readonly-{i}", "text": text}, store=store,
            config=replace(CONFIG, preset="shadow_readonly"))
    assert json.dumps([store.intake, store.foods, store.pending], sort_keys=True) == before


def nutrition_and_pending():
    """营养计算有独立期望值；歧义可回复，过期 pending 不可继续"""
    values = calculate_food_intake({"carbs_per_100g": 10, "protein_per_100g": 20, "fat_per_100g": 5}, 200)
    assert values == {"carbs": 20, "protein": 40, "fat": 10, "kcal": 330}
    result = run({"message_id": "amb", "user_id": "u", "chat_id": "c", "text": "米营养"}, store=FakeStore(), config=CONFIG)
    assert result["tool_results"][0]["status"] == "needs_confirmation"
    assert result["guard_result"]["passed"]
    store = FakeStore()
    with patch("agent.storage.fake_store.now_millis", return_value=1000):
        store.create_pending({"message_id": "m", "user_id": "u", "chat_id": "c"}, "missing_grams", "米饭", None)
    with patch("agent.storage.fake_store.now_millis", return_value=1000 + 25 * 3600000):
        assert store.load_active_pending("u", "c") is None


def knowledge_sync():
    """真假存储按知识 ID 幂等更新，答案改变不重复创建"""
    records = []
    real = BitableStore()
    real.app_token, real.knowledge_table = "test", "knowledge"
    def create(table, rows, client_token):
        assert client_token
        records.extend((f"r{len(records)+i}", row["fields"]) for i, row in enumerate(rows))
        return {"code": 0}
    def update(table, rid, fields):
        next(old for key, old in records if key == rid).update(fields)
    entry = {"entry_id": "test-entry", "question": "问题", "answer": "初版"}
    with patch.object(bitable_adapter, "_list_records", side_effect=lambda *args: records.copy()), patch.object(real, "_batch_create", side_effect=create), patch.object(real, "_update_record", side_effect=update):
        for store in (FakeStore(), real):
            assert store.sync_knowledge([entry]) == {"created_count": 1, "updated_count": 0}
            assert store.sync_knowledge([entry]) == {"created_count": 0, "updated_count": 0}
            assert store.sync_knowledge([{**entry, "answer": "修订"}]) == {"created_count": 0, "updated_count": 1}


def deployment_payloads():
    """部署路径正确，Layer 更新保留旧环境及层顺序"""
    from deploy import api_deploy, deploy_rag_layer
    current = {"Environment": {"Variables": [{"Key": "KEEP", "Value": "test"}]},
               "Layers": [{"LayerName": "other", "LayerVersion": 1}, {"LayerName": "calorie-rag", "LayerVersion": 2}]}
    calls = []
    def request(action, payload):
        calls.append((action, payload))
        return current if action == "GetFunction" else {}
    with patch.object(deploy_rag_layer, "tc3_request", side_effect=request):
        deploy_rag_layer.main(["--attach-only", "3"])
    payload = calls[-1][1]
    assert payload["Layers"] == [{"LayerName": "other", "LayerVersion": 1}, {"LayerName": "calorie-rag", "LayerVersion": 3}]
    assert {x["Key"]: x["Value"] for x in payload["Environment"]["Variables"]}["KEEP"] == "test"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "deploy").mkdir()
        (root / "deploy/calorie-agent-v4.zip").write_bytes(b"test")
        calls.clear()
        with patch.object(api_deploy, "ROOT", root), patch.object(api_deploy, "tc3_request", side_effect=request):
            api_deploy.main([])
        assert calls[0][0] == "UpdateFunctionCode"


def knowledge_snapshot():
    """索引无静默 seed 回退，非空目录保持原样"""
    from deploy import ingest_knowledge
    with patch.object(BitableStore, "load_knowledge", return_value=[]):
        try:
            ingest_knowledge._documents("bitable")
        except RuntimeError:
            pass
        else:
            raise AssertionError("empty Bitable silently fell back")
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory)
        marker = output / "old-index"
        marker.write_text("keep", encoding="utf-8")
        with patch.object(ingest_knowledge, "_documents", return_value=["new"]):
            try:
                asyncio.run(ingest_knowledge._run("bitable", output))
            except ValueError:
                pass
            else:
                raise AssertionError("non-empty output accepted")
        assert marker.read_text(encoding="utf-8") == "keep"


def cli_and_trace():
    """CLI 逐轮回复并保留状态；trace 耗时与返回值一致"""
    import cli
    from agent.nodes.basic_nodes import trace_writer
    replies, messages = [], []
    texts = iter(["米饭", "200g", ""])
    def input_text(prompt):
        text = next(texts)
        if text == "200g":
            assert len(replies) == 1
        return text
    actual_run = cli.run
    def capture(message, **kwargs):
        messages.append(message)
        return actual_run(message, **kwargs)
    with patch.object(cli, "load_config", return_value=CONFIG), patch("builtins.input", side_effect=input_text), patch.object(cli, "run", side_effect=capture), patch.object(cli, "_print_result", side_effect=replies.append):
        cli.main([])
    assert "已记录" in replies[-1]["reply"]
    assert len({msg["message_id"] for msg in messages}) == 2
    with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"TRACE_ENABLED": "true", "TRACE_PATH": str(Path(directory) / "trace.jsonl")}):
        result = trace_writer({"started_at": time.monotonic() - 1})
        payload = json.loads(Path(directory, "trace.jsonl").read_text(encoding="utf-8"))
        assert payload["latency_ms"] == result["latency_ms"] >= 900


def boohee_failure():
    """薄荷故障保持 pending，并在工具事实中暴露失败"""
    def unavailable(url, headers):
        raise TimeoutError("test timeout")
    with patch.object(boohee_client, "_TRANSPORT", unavailable):
        boohee_client._cache.clear()
        result = run({"message_id": "fail", "user_id": "u", "chat_id": "c", "text": "未知餐品99g"}, store=FakeStore(), config=CONFIG)
    fact = result["tool_results"][0]
    assert fact["status"] == "pending_created"
    assert "boohee_search_failed" in fact["error"]
    assert "已记录" not in result["reply"]



def _message(text, key, chat="c"):
    return {"message_id": key, "user_id": "u", "chat_id": chat, "text": text}


def stable_execution():
    """部分失败后沿用首份计划与日期，完成重放不重复写入"""
    from agent.schemas import AgentAction
    class TransientStore(FakeStore):
        calls = 0
        def write_intake(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 2:
                raise TimeoutError("second write failed")
            return super().write_intake(*args, **kwargs)
    store = TransientStore()
    config = replace(CONFIG, planner="deepseek")
    plans = [AgentPlan("record_intake", [AgentAction(f"a{i}", "record_food", {"food_query": n, "grams": 100})
                                       for i, n in enumerate(names, 1)])
             for names in (("米饭", "鸡蛋"), ("鸡蛋", "米饭"))]
    msg = _message("帮我把刚才那些食物记进午餐", "stable")
    with patch("agent.nodes.planner_node.plan_with_deepseek", side_effect=plans) as llm:
        first = run(msg, store=store, config=config)
        assert first["status"] == "error" and len(store.intake) == 1
        store.today = "2026-09-06"
        second = run(msg, store=store, config=config)
        llm.assert_called_once()
    assert second["status"] == "ok"
    assert [row["food_name"] for row in store.intake] == ["米饭", "鸡蛋"]
    assert {row["date"] for row in store.intake} == {"2026-09-05"}
    assert run(msg, store=store, config=config) == second
    assert len(store.intake) == 2
    store = FakeStore()
    run(_message("米饭200g", ""), store=store, config=CONFIG)
    run(_message("鸡蛋100g", ""), store=store, config=CONFIG)
    assert len(store.intake) == 2


def pending_execution():
    """追问复用工具、白名单和固定候选；撤销重试只作用于原目标"""
    from agent.schemas import AgentAction
    store = FakeStore()
    run(_message("米饭", "ask"), store=store, config=CONFIG)
    limited = replace(CONFIG, preset="live", allowed_tools=("query_today",))
    denied = run(_message("200g", "denied"), store=store, config=limited)
    assert not denied["policy_decision"]["allowed"] and not store.intake
    assert store.load_active_pending("u", "c")["intent"] == "missing_grams"
    run(_message("200g", "answer"), store=store, config=CONFIG)
    run(_message("鸡蛋100g", "egg"), store=store, config=CONFIG)
    run(_message("撤销上一条", "undo"), store=store, config=CONFIG)
    run(_message("蛋白质是什么", "question"), store=store, config=CONFIG)
    assert all(row["status"] == "valid" for row in store.intake)
    delete = store.undo_last
    attempts = []
    def timeout_after_delete(user_id, record_id=""):
        result = delete(user_id, record_id)
        attempts.append(record_id)
        if len(attempts) == 1:
            raise TimeoutError("delete committed before timeout")
        return result
    confirm = _message("确认", "confirm")
    with patch.object(store, "undo_last", side_effect=timeout_after_delete):
        assert run(confirm, store=store, config=CONFIG)["status"] == "error"
        assert run(confirm, store=store, config=CONFIG)["status"] == "ok"
    assert len(set(attempts)) == 1
    assert [row["status"] for row in store.intake] == ["valid", "deleted"]
    # A cancelled authorization must not be revived by an old failed execution.
    cancelled = FakeStore()
    run(_message("米饭100g", "seed"), store=cancelled, config=CONFIG)
    run(_message("撤销上一条", "request"), store=cancelled, config=CONFIG)
    stale = _message("确认", "failed-confirm")
    with patch.object(cancelled, "undo_last", side_effect=TimeoutError("before delete")):
        assert run(stale, store=cancelled, config=CONFIG)["status"] == "error"
    run(_message("取消", "cancel"), store=cancelled, config=CONFIG)
    assert not run(stale, store=cancelled, config=CONFIG)["policy_decision"]["allowed"]
    assert cancelled.intake[0]["status"] == "valid"
    # Unknown bare name uses the LLM once, then follows the same record tool.
    store = FakeStore()
    candidate = {"code": "cooked", "name": "藜麦饭", "protein": 4.4, "fat": 2, "carbohydrate": 20.5}
    plan = AgentPlan("record_intake", [AgentAction("a1", "record_food", {"food_query": "藜麦饭"})])
    with patch("agent.nodes.planner_node.plan_with_deepseek", return_value=plan):
        run(_message("藜麦饭", "unknown"), store=store, config=replace(CONFIG, planner="deepseek"))
    with patch.object(boohee_client, "_TRANSPORT", lambda u, h: {"code": 0, "data": {"foods": [candidate]}}):
        boohee_client._cache.clear()
        offered = run(_message("50g", "weight"), store=store, config=CONFIG)
    assert offered["tool_results"][0]["status"] == "needs_confirmation"
    with patch.object(boohee_client, "search_food", side_effect=AssertionError("confirmation must use snapshot")):
        boohee_client._cache.clear()
        imported = run(_message("1", "pick"), store=store, config=CONFIG)
    assert imported["status"] == "ok"
    assert store.foods[-1]["boohee_id"] == "cooked"


def delivery_recovery():
    """ACK 前持久化收件；发送失败可重放同一结果而不重新写入"""
    import app
    from agent.schemas import SUPPORTED_TOOLS
    store = FakeStore()
    config = replace(CONFIG, preset="live", allowed_tools=tuple(SUPPORTED_TOOLS))
    payload = {"event": {"sender": {"sender_id": {"open_id": "u"}}, "message": {
        "message_type": "text", "message_id": "delivery", "chat_id": "c", "content": json.dumps({"text": "米饭200g"})}}}
    raw = json.dumps(payload).encode()
    handler = object.__new__(app.Handler)
    handler.path, handler.headers, handler.rfile = "/webhook/feishu/events", {"Content-Length": str(len(raw))}, io.BytesIO(raw)
    def ack(data, status=200):
        assert status == 200 and data["status"] == "accepted"
        assert any(t["intent"] == "message_received" for t in store.pending)
        assert not store.intake
    handler._send_json = ack
    with patch.object(app, "_get_store", return_value=store), patch.object(app, "load_config", return_value=config), \
            patch.object(app, "_verify_rollover_style_token", return_value=True), \
            patch.dict(app._MESSAGE_STATE, {}, clear=True), \
            patch.object(app.feishu_client, "send_text", side_effect=[TimeoutError("send"), TimeoutError("send"), {}]) as send:
        with patch.object(app, "_process_message_synchronously", return_value=False):
            handler.do_POST()  # Simulate exit after ACK but before planning.
        assert not store.intake
        store.today = "2026-09-06"
        assert app.retry_pending_messages() == 1  # Both sends fail after the write.
        assert len(store.intake) == 1 and store.intake[0]["date"] == "2026-09-05"
        assert store.load_unfinished_executions()[0]["intent"] == "message_received"
        assert app.retry_pending_messages() == 0
        assert len(store.intake) == 1 and not store.load_unfinished_executions()
        assert len({call.kwargs["uuid_key"] for call in send.call_args_list}) == 1
        assert len({call.args[1] for call in send.call_args_list}) == 1


def parser_boundaries():
    """记录快路径完整消费语义、数量单位和每项餐次"""
    from agent.planners.deterministic import build_plan
    from agent.nodes.planner_node import planner
    for text in ("我还能吃米饭200g吗", "不要记录米饭200g", "米饭200ml", "米饭200g明天再说"):
        assert not any(a.tool == "record_food" for a in build_plan(text).actions), text
    assert build_plan("吃了米饭0.5kg").actions[0].parameters["grams"] == 500
    assert build_plan("吃了米饭1斤").actions[0].parameters["grams"] == 500
    plan = build_plan("早餐鸡蛋100g，午餐米饭200g")
    assert [a.parameters["meal_type"] for a in plan.actions] == ["breakfast", "lunch"]
    for text in ("确认", "收到", "好像米饭200g"):
        state = {"config": CONFIG, "normalized_text": text, "store": FakeStore(), "context": {}}
        assert not planner(state)["plan"]["actions"]


def execution_store_contract():
    """Bitable 传输替身验证首份快照、固定目标和创建响应真实性"""
    from copy import deepcopy
    from agent.tools.base import build_intake_item
    real = BitableStore()
    real.app_token, real.pending_table, real.intake_table = "test", "pending", "intake"
    tables = {"pending": [], "intake": []}
    def create(table, rows, client_token):
        assert client_token
        made = []
        for row in rows:
            rid = f"{table}-{len(tables[table])+1}"
            tables[table].append((rid, deepcopy(row["fields"])))
            made.append({"record_id": rid})
        return {"code": 0, "data": {"records": made}}
    def update(table, rid, fields):
        next(row for key, row in tables[table] if key == rid).update(deepcopy(fields))
        return {"code": 0}
    msg = _message("米饭100g", "store")
    with patch.object(bitable_adapter, "_list_records", side_effect=lambda app, table, *args: deepcopy(tables[table])), \
            patch.object(real, "_batch_create", side_effect=create), patch.object(real, "_update_record", side_effect=update):
        for store in (FakeStore(), real):
            first = store.begin_execution(msg, {"plan": {"goal": "first"}})
            second = store.begin_execution(msg, {"plan": {"goal": "other"}})
            assert second["payload"] == first["payload"]
            assert store.load_active_pending("u", "c") is None
            item = {**build_intake_item(FakeStore().foods[0], 100), "date": "2026-01-01"}
            store.write_intake(msg, [item], "breakfast")
            target = store.last_intake("u")
            assert store.undo_last("u", target["record_id"])["status"] == "deleted"
            assert store.undo_last("u", target["record_id"])["status"] == "deleted"
            assert store.last_intake("u") is None
        with patch.object(real, "_batch_create", return_value={"code": 0, "data": {"records": []}}):
            try:
                real.write_intake(_message("鸡蛋100g", "empty-response"), [item])
            except RuntimeError:
                pass
            else:
                raise AssertionError("empty create response claimed success")


CHECKS = {"C01": guard_facts, "C02": policy_and_readonly, "C03": nutrition_and_pending,
          "C04": knowledge_sync, "C05": deployment_payloads, "C06": knowledge_snapshot,
          "C07": cli_and_trace, "C08": boohee_failure, "C09": stable_execution,
          "C10": pending_execution, "C11": delivery_recovery, "C12": parser_boundaries,
          "C13": execution_store_contract}
