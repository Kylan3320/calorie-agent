"""Message deadlines and delivery recovery through the native runtime and COS Store."""
from contextlib import contextmanager, redirect_stdout
from copy import deepcopy
from dataclasses import replace
import io
import json
import time
from unittest.mock import patch

import httpx
from langchain_core.messages import AIMessage

from agent import run
import agent.model as model_module
from agent.storage.cos_journal import CommitUnavailable
from agent.storage.cos_store import CosStore
from agent.storage.execution_pipeline import Pipeline
from harness.eval.model_transport_cases import Clock, PAYLOAD
from harness.eval.personal_cases import A as MEAL_FOOD, CONFIG, MEAL, NativeFake, call
from harness.eval.production_pipeline_cases import event, message
from harness.eval.production_store_cases import Objects, open_store


LIVE = replace(CONFIG, preset="live")


def forbidden(*args, **kwargs):
    raise AssertionError("expired or delivered work must not execute again")


@contextmanager
def offline(clock):
    with patch.object(time, "monotonic", clock.monotonic), patch.object(time, "time", clock.time), \
            patch.object(time, "sleep", clock.sleep), patch("agent.config.load_config", return_value=LIVE), \
            patch.dict("os.environ", {"TRACE_ENABLED": "false", "DEEPSEEK_API_KEY": "offline-fixture"}), \
            patch("agent.storage.boohee_client.search_food", side_effect=forbidden), redirect_stdout(io.StringIO()):
        yield


def finished(args=MEAL, ident="c1"):
    return call("record_meal", {**deepcopy(args), "finish_after_tools": True}, ident)


def business_message(clock, ident):
    return {**message(ident, "记录夹具A和夹具B"), "received_at": clock.time()}


def successful_send(sent):
    def send(chat, text, uuid_key):
        sent.append((chat, text, uuid_key))
        return {"data": {"message_id": "delivery-" + str(len(sent))}}
    return send


def CB01():
    """>10且<40秒的首次理解继续正常提交并直接回执，不把慢请求当超时。"""
    clock = Clock()
    with offline(clock):
        store = open_store()
        original = business_message(clock, "cb-slow-understanding")

        def slow(messages):
            clock.now += 15
            return finished()

        model = NativeFake(responses=[slow])
        result = run(original, store=store, config=LIVE, model=model)
        assert result["status"] == "ok" and result["reply_source"] == "tool_facts" and result["model_reply"] == ""
        assert result["model_calls"] == len(model.requests) == 1
        assert result["latency_ms"] == 15000 and result["deadline_at"] == original["received_at"] + 90
        assert len(result["model_steps"]) == 1 and result["model_steps"][0]["deadline_at"] == original["received_at"] + 40
        assert "270千卡" in result["reply"] and "估算" in result["reply"]
        assert len(store.intake_items("user")) == 2 and store.today_summary("user")["totals"]["kcal"] == 270


def CB02():
    """原received+90过期零模型零摄入；重投不续期，终态推进后新消息正常执行。"""
    clock = Clock()
    with offline(clock):
        store = open_store()
        pipeline = Pipeline(store.objects, store.prefix)
        original = message("cb-expired-queue", "记录夹具A200克")
        receipt = pipeline.accept(original, received_at=clock.time())
        clock.now += 91
        blocked = NativeFake(responses=[finished()])
        sent = []

        def execute(msg, **kwargs):
            return run(msg, model=blocked, **kwargs)

        state = pipeline.worker(event(receipt), execute=execute, send=successful_send(sent))
        assert state["status"] == "completed" and state["work_status"] == "terminated" and state["attempts"] == 1
        assert state["deadline_at"] == receipt["received_at"] + 90 and state["queue_wait_ms"] == 91000
        assert state["result"]["termination_reason"] == "deadline_reached" and state["result"]["model_calls"] == 0
        assert not blocked.requests and not pipeline.readonly_store().intake_items("user")
        assert len(sent) == 1
        duplicate = pipeline.accept(original, received_at=clock.time())
        assert duplicate["received_at"] == receipt["received_at"]
        assert pipeline.worker(event(duplicate), execute=forbidden, send=forbidden)["duplicate"]
        assert pipeline.result(receipt["receipt_id"])["deadline_at"] == state["deadline_at"]
        fresh = pipeline.accept(message("cb-after-expired", "查今天的热量"), received_at=clock.time())
        fresh_model = NativeFake(responses=[call("query_today", {"finish_after_tools": True})])
        state = pipeline.worker(event(fresh), execute=lambda msg, **kwargs: run(msg, model=fresh_model, **kwargs), send=successful_send(sent))
        assert state["status"] == "completed" and state["result"]["reply_source"] == "tool_facts"
        assert len(fresh_model.requests) == 1 and len(sent) == 2


def CB03():
    """实际模型503仅额外一传输，同一步/同请求；成功或耗尽后的平台重投零新HTTP。"""
    for statuses in ((503, 200), (503, 503)):
        clock = Clock()
        with offline(clock):
            store = open_store()
            pipeline = Pipeline(store.objects, store.prefix)
            receipt = pipeline.accept(message("cb-http-" + str(statuses[-1]), "记录整餐"), received_at=clock.time())
            requests = []
            payload = deepcopy(PAYLOAD)
            payload["choices"] = [{"finish_reason": "tool_calls", "message": {"content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "record_meal", "arguments": json.dumps({**MEAL, "finish_after_tools": True}, ensure_ascii=False)}}]}}]

            def respond(request):
                requests.append(request.content)
                clock.now += 1
                return httpx.Response(statuses[len(requests)-1], json=payload, headers={"x-request-id": "cb-attempt-" + str(len(requests))})

            sent = []
            with httpx.Client(transport=httpx.MockTransport(respond)) as client, patch.object(model_module, "_CLIENT", client), \
                    patch.object(model_module.random, "uniform", return_value=.75):
                state = pipeline.worker(event(receipt), execute=lambda msg, **kwargs: run(msg, model=model_module.DeepSeekModel(), **kwargs), send=successful_send(sent))
                result = state["result"]
                assert state["status"] == "completed" and state["attempts"] == 1
                assert result["model_calls"] == len(result["model_steps"]) == len(result["model_requests"]) == 1
                step = result["model_steps"][0]
                assert step["deadline_at"] == receipt["received_at"] + 40 and result["deadline_at"] == receipt["received_at"] + 90
                assert len(requests) == len(step["attempts"]) == 2 and requests[0] == requests[1]
                assert [attempt["status_code"] for attempt in step["attempts"]] == list(statuses)
                assert [attempt["provider_request_id"] for attempt in step["attempts"]] == ["cb-attempt-1", "cb-attempt-2"]
                assert clock.sleeps == [.75]
                if statuses[-1] == 200:
                    assert result["reply_source"] == "tool_facts" and result["status"] == "ok"
                    assert len(pipeline.readonly_store().intake_items("user")) == 2
                else:
                    assert result["terminal"] and result["termination_reason"] == "model_retry_exhausted"
                    assert state["work_status"] == "terminated" and not pipeline.readonly_store().intake_items("user")
                clock.now += 100
                restored = Pipeline(store.objects, store.prefix)
                duplicate = restored.accept(receipt["message"], received_at=clock.time())
                assert duplicate["received_at"] == receipt["received_at"]
                assert restored.worker(event(duplicate), execute=forbidden, send=forbidden)["duplicate"]
                assert len(requests) == 2 and len(sent) == 1


def _interrupted_action(clock, *, after, multiple):
    objects = Objects()
    store = open_store(objects)
    ident = "cb-after" if after else "cb-before"
    original = business_message(clock, ident)
    objects.fault_action_prefix = "action:" + ident + ":agent:"
    setattr(objects, "lose_action" if after else "interrupt_action", True)
    batch = finished({"items": [{"food_query": "夹具A", "grams": 200}]})
    if multiple:
        batch = AIMessage(content="", tool_calls=[*batch.tool_calls,
            *finished({"items": [{"food_query": "夹具B", "grams": 100}]}, "c2").tool_calls])
    first_model = NativeFake(responses=[batch])
    try:
        run(original, store=store, config=LIVE, model=first_model)
    except (OSError, CommitUnavailable):
        pass
    else:
        raise AssertionError("uncertain manifest must require a fresh confirmation barrier")
    assert len(first_model.requests) == 1 and store.journal.unavailable
    restored = open_store(objects)
    reserved = restored.load_execution(original)["payload"]
    assert reserved["deadline_at"] == original["received_at"] + 90
    assert reserved["calls"][0]["action_id"] == ident + ":agent:1" and reserved["calls"][0]["prepared"]
    assert len(restored.intake_items("user")) == int(after)
    clock.now += 91
    recovery_model = NativeFake(responses=[forbidden])
    with patch.object(restored, "write_intake", side_effect=forbidden):
        result = run(original, store=restored, config=LIVE, model=recovery_model)
    assert not recovery_model.requests and result["model_calls"] == 1
    assert result["deadline_at"] == original["received_at"] + 90
    assert len(restored.intake_items("user")) == int(after)
    assert restored.load_execution(original)["status"] == "completed"
    saved = restored.load_execution(original)["payload"]
    assert saved["calls"][0]["action_id"] == ident + ":agent:1"
    assert bool(restored.committed_action(ident + ":agent:1")) is after
    if after and not multiple:
        assert result["status"] == "ok" and result["reply_source"] == "tool_facts" and "120千卡" in result["reply"]
    else:
        assert result["terminal"] and result["termination_reason"] == "deadline_reached"
        unexecuted = saved["calls"][1:] if after else saved["calls"]
        assert unexecuted and all(entry.get("terminated") == "deadline_reached" and "result" not in entry for entry in unexecuted)
        if after:
            assert "120千卡" in result["reply"] and "150千卡" not in result["reply"]
    with patch.object(restored, "write_intake", side_effect=forbidden):
        replay = run(original, store=restored, config=LIVE, model=recovery_model)
    assert replay["reply"] == result["reply"] and not recovery_model.requests
    assert len(restored.intake_items("user")) == int(after)


def CB04():
    """COS提交前中断，过期恢复只核对原action，prepared和第二动作均不得复活。"""
    clock = Clock()
    with offline(clock):
        _interrupted_action(clock, after=False, multiple=True)


def CB05():
    """提交成功回包丢失后过期：完整事实可直接交付，部分提交保留且后续动作终止。"""
    for multiple in (False, True):
        clock = Clock()
        with offline(clock):
            _interrupted_action(clock, after=True, multiple=multiple)


def CB06():
    """已存事实回执发送ACK丢失，过期重投原正文/UUID；零工具模型且只一次账本。"""
    clock = Clock()
    with offline(clock):
        store = open_store()
        pipeline = Pipeline(store.objects, store.prefix)
        receipt = pipeline.accept(message("cb-delivery-ack", "记录午餐"), received_at=clock.time())
        model = NativeFake(responses=[finished()])
        sends, executions = [], []

        def execute(msg, **kwargs):
            executions.append(msg["message_id"])
            return run(msg, model=model, **kwargs)

        def ack_lost(chat, text, uuid_key):
            sends.append((chat, text, uuid_key))
            raise TimeoutError("send accepted but acknowledgement lost")

        try:
            pipeline.worker(event(receipt), execute=execute, send=ack_lost)
        except TimeoutError:
            pass
        else:
            raise AssertionError("delivery acknowledgement loss was not injected")
        state = pipeline.result(receipt["receipt_id"])
        assert state["status"] == "retry" and state["attempts"] == 1
        assert state["result"]["reply_source"] == "tool_facts" and not state["delivery"]["presented"]
        assert state["delivery"]["uuid"] == sends[0][2] and state["result"]["reply"] == sends[0][1]
        deadline = state["deadline_at"]
        clock.now += 100
        restored = Pipeline(store.objects, store.prefix)
        with patch.object(CosStore, "write_intake", side_effect=forbidden):
            recovered = restored.worker(event(receipt), execute=forbidden, send=successful_send(sends))
            assert restored.worker(event(receipt), execute=forbidden, send=forbidden)["duplicate"]
        assert recovered["status"] == "completed" and recovered["attempts"] == 2
        assert recovered["deadline_at"] == deadline == receipt["received_at"] + 90
        assert len(model.requests) == 1 and executions == ["cb-delivery-ack"]
        assert len(sends) == 2 and sends[0] == sends[1]
        rows = restored.readonly_store().intake_items("user")
        assert len(rows) == 2 and sorted(row["nutrition"]["kcal"] for row in rows) == [120, 150]


def CB07():
    """旧收件无时间仅首次接管保存legacy起点，重试不续期且T_service为空。"""
    clock = Clock()
    with offline(clock):
        store = open_store()
        pipeline = Pipeline(store.objects, store.prefix)
        receipt = pipeline.accept(message("cb-legacy-no-time"))
        inbox = pipeline.inbox()
        legacy = inbox.get("receipt:" + receipt["receipt_id"])
        legacy.pop("received_at")
        inbox.commit({"receipt:" + receipt["receipt_id"]: legacy})

        def interrupted(*args, **kwargs):
            raise ConnectionError("storage unavailable before runtime")

        try:
            pipeline.worker(event(receipt), execute=interrupted)
        except ConnectionError:
            pass
        else:
            raise AssertionError("legacy first takeover interruption missing")
        first = pipeline.result(receipt["receipt_id"])
        assert first["legacy_budget"] and first["budget_started_at"] == clock.time()
        assert first["deadline_at"] == clock.time() + 90
        clock.now += 100
        restored = Pipeline(store.objects, store.prefix)
        model = NativeFake(responses=[])
        state = restored.worker(event(receipt), execute=lambda msg, **kw: run(msg, model=model, **kw), send=successful_send([]))
        assert state["status"] == "completed" and state["result"]["terminal"]
        assert state["legacy_budget"] and state["deadline_at"] == first["deadline_at"]
        assert state["result"]["legacy_budget"] and state["delivery"]["t_service_ms"] is None
        assert state["queue_wait_ms"] is None and not model.requests


def CB08():
    """原子账本与待投递回执、飞书确认与游标、两次丢失提交回包后的恢复。"""
    _atomic_receipt(False)


def CB09():
    """同一次 COS 发布携带恢复数据，冷重启在原提交上恢复。"""
    _atomic_receipt(True)


def _atomic_receipt(packed):
    from agent.storage.bitable_adapter import stable_uuid
    from agent.storage.execution_pipeline import receipt_id

    clock = Clock()
    with offline(clock), patch.dict("os.environ", {"CALORIE_PACK_MESSAGE_COMMITS": str(int(packed))}):
        store = open_store()
        objects = store.objects
        pipeline = Pipeline(objects, store.prefix)
        original = pipeline.accept(message("cb-atomic", "记录午餐"), received_at=clock.time())
        rid = original["receipt_id"]
        receipt_key = "document:" + stable_uuid("receipt_state:system:" + rid)
        cursor_key = "document:" + stable_uuid("ingress_cursor:system:" + receipt_id("user", "test"))
        model = NativeFake(responses=[finished()])

        class ModelFactory:
            def __new__(cls, **kwargs):
                return model

        class NoModel:
            def __new__(cls, **kwargs):
                forbidden()

        objects.fault_action_prefix = "action:cb-atomic:agent:"
        objects.lose_action = True
        with patch("agent.graph.DeepSeekModel", ModelFactory):
            try:
                pipeline.worker(event(original), send=forbidden)
            except (OSError, CommitUnavailable):
                pass
            else:
                raise AssertionError("lost action response must stop delivery")
        recovered = Pipeline(objects, store.prefix)
        ready = recovered.result(rid)
        assert ready["status"] == "processing" and not ready["delivery"]["presented"]
        assert ready["result"]["tool_results"][0]["data"]["total"]["kcal"] == 270
        assert "tool_results" not in recovered.readonly_store().load_receipt(rid)["result"]
        by_key = {}
        for key, raw in sorted(objects.data.items()):
            if "/message/commits/" in key:
                for changed in json.loads(raw)["changes"]:
                    by_key[changed] = key
        assert by_key[receipt_key] == by_key["action:cb-atomic:agent:1"]
        assert by_key["pending:" + ready["execution_id"]] == by_key[receipt_key]
        assert all(by_key[key] == by_key[receipt_key] for key in by_key if key.startswith("item:"))
        if packed:
            from agent.storage.cos_journal import PACKED_SCHEMA
            from harness.eval.production_live import RawCommits
            manifest = json.loads(objects.data[by_key[receipt_key]])
            assert manifest["schema"] == PACKED_SCHEMA
            revision, digest = manifest["changes"][receipt_key].split(":")
            assert int(revision) == manifest["revision"] and digest in manifest["objects"]
            assert manifest["root"].startswith(revision + ":")
            audit = RawCommits(objects, store.journal.prefix).items("user", exact=manifest["revision"])
            assert sorted(row["nutrition"]["kcal"] for row in audit["items"]) == [120, 150]
        sends = []
        objects.fault_action_prefix = "action:receipt-complete:" + rid
        objects.lose_action = True
        with patch("agent.graph.DeepSeekModel", NoModel), patch.object(CosStore, "write_intake", side_effect=forbidden):
            try:
                recovered.worker(event(original), send=successful_send(sends))
            except (OSError, CommitUnavailable):
                pass
            else:
                raise AssertionError("lost completion response was not injected")
            restarted = Pipeline(objects, store.prefix)
            assert restarted.worker(event(original), send=forbidden)["duplicate"]
        complete = restarted.result(rid)
        assert complete["status"] == "completed" and complete["delivery"]["presented"]
        assert complete["result"] == ready["result"]
        assert sends == [("test", ready["result"]["reply"], ready["delivery"]["uuid"])]
        changes = next(commit["changes"] for key, raw in objects.data.items() if "/message/commits/" in key
                       and "action:receipt-complete:" + rid in (commit := json.loads(raw))["changes"])
        assert {receipt_key, cursor_key, "document:" + stable_uuid("dialogue_turn:user:calorie-user:cb-atomic")} <= changes.keys()
        assert len(restarted.readonly_store().intake_items("user")) == 2


def CB10():
    """明确的称重名称按原文绑定，品牌与未说明生熟仍应保留歧义。"""
    clock = Clock()
    foods = [
        {**deepcopy(MEAL_FOOD), "food_id": "rice-cooked", "name": "米饭（熟）", "aliases": ["熟米饭", "米饭"], "type": "熟"},
        {**deepcopy(MEAL_FOOD), "food_id": "rice-generic", "name": "米饭", "aliases": []},
    ]
    with offline(clock):
        store = CosStore(Objects(), "acceptance/food-grounding")
        store.import_baseline({"foods": foods})
        for i, (phrase, supplied) in enumerate([
            ("熟米饭", "熟米饭（白米饭，蒸煮后）"),
            ("已煮熟的白米饭", "白米饭（已煮熟）"),
            ("已煮熟的白米饭（只记这一项）", "已煮熟的白米饭"),
        ]):
            msg = message(f"grounded-{i}", f"午餐记一条：200克{phrase}。告诉我这条记录的热量。")
            outcome = run(msg, store=store, config=LIVE, model=NativeFake(responses=[call("record_meal",
                {"items": [{"food_query": supplied, "grams": 200}], "finish_after_tools": True})]))
            assert outcome["model_calls"] == 1 and outcome["tool_results"][0]["data"]["items"][0]["snapshot"]["food"]["food_id"] == "rice-cooked"
        with patch("agent.storage.boohee_client.search_food", return_value=[]):
            for i, text in enumerate(("记200克某牌熟米饭。", "记200克米饭。", "记某牌熟米饭200克。", "记米饭200克。",
                                      "记200克熟米饭2号。", "记200克熟米饭（特别版（新））。")):
                msg = message(f"ambiguous-{i}", text)
                outcome = run(msg, store=store, config=LIVE, model=NativeFake(responses=[call("record_meal",
                    {"items": [{"food_query": "熟米饭", "grams": 200}], "finish_after_tools": True}),
                    AIMessage(content="请明确食物身份。")]))
                assert outcome["tool_results"][0]["status"] == "pending"
        assert len(store.intake_items("user")) == 3


CHECKS = {name: globals()[name] for name in ("CB01", "CB02", "CB03", "CB04", "CB05", "CB06", "CB07", "CB08", "CB09", "CB10")}
