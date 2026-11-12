"""Action notes are durable hints; only final delivery presents business state."""
from copy import deepcopy
import json
import time
from unittest.mock import patch
from langchain_core.messages import AIMessage, ToolMessage
from agent import run
from agent.storage.timeutil import PROCESSING_DEADLINE
from harness.eval.personal_cases import NativeFake, call
from harness.eval.production_pipeline_cases import fixture, event, message

NOTE = "我会核对全谷物搭配的资料，再给你出处。"


def final(messages):
    fact = json.loads(next(m.content for m in reversed(messages) if isinstance(m, ToolMessage)))
    return AIMessage(content="全谷物可与蔬菜搭配。[" + fact["data"]["entries"][0]["source_id"] + "]")


def scenario(kind):
    objects, pipeline, config = fixture()
    receipt = pipeline.accept(message("action-note-" + kind, "全谷物怎样搭配？请查依据。"))
    notes, edits, sent, retrieval = [], [], [], []
    class Death(BaseException):
        pass
    def note(chat, text, uuid_key):
        assert 0 < PROCESSING_DEADLINE.get() - time.monotonic() <= 1
        notes.append((text, uuid_key))
        if kind == "lost_note":
            raise Death()
        return {"code": 0, "data": {"message_id": "same-note-message"}}
    def retrieve(query, entries):
        retrieval.append(query)
        if kind not in {"omitted", "invalid"}:
            assert notes
        return {"status": "ok", "entries": [{"answer": "全谷物可与蔬菜搭配。", "source": "资料甲"}]}
    def edit(mid, text):
        edits.append((mid, text))
        if kind == "lost_final" and len(edits) == 1:
            raise TimeoutError("final edit ACK lost")
        return {"code": 0}
    def send(chat, text, uuid_key):
        sent.append((text, uuid_key))
        return {"code": 0, "data": {"message_id": "final-message"}}
    args = {"query": "全谷物搭配"}
    if kind != "omitted":
        args["action_note"] = {"invalid": True} if kind == "invalid" else NOTE
    model = NativeFake(responses=[call("query_knowledge", args), final])
    def execute(msg, **kwargs):
        return run(msg, model=model, **kwargs)
    with patch("agent.config.load_config", return_value=config), \
         patch("agent.tools.reads.build_knowledge_retriever", return_value=retrieve):
        if kind in {"lost_note", "lost_final"}:
            try:
                pipeline.worker(event(receipt), execute=execute, send=send, update_send=edit, note_send=note)
            except (Death, TimeoutError):
                pass
            else:
                raise AssertionError("interruption missing")
            interrupted = pipeline.result(receipt["receipt_id"])
            assert not interrupted.get("delivery", {}).get("presented")
            from agent.storage.execution_pipeline import Pipeline
            pipeline = Pipeline(objects, pipeline.prefix)
            model = NativeFake(responses=[final])
        result = pipeline.worker(event(receipt), execute=execute, send=send, update_send=edit, note_send=note)
        assert result["status"] == "completed" and result["delivery"]["presented"]
        assert "资料甲" in result["result"]["reply"]
        assert len(retrieval) == 1
        assert pipeline.worker(event(receipt), execute=lambda *a, **k: (_ for _ in ()).throw(AssertionError("replay re-executed")))["duplicate"]
    if kind in {"omitted", "invalid"}:
        assert not notes and len(sent) == 1 and not edits
    elif kind == "lost_note":
        assert len(notes) == len(sent) == 1 and not edits
        assert notes[0][1] != sent[0][1]
    else:
        assert len(notes) == 1 and not sent and len(edits) == (2 if kind == "lost_final" else 1)
        assert all(mid == "same-note-message" for mid, _ in edits)
    return result


def AN01():
    """首轮知识说明在检索前确认，最终编辑同message；普通账本无额外发送。"""
    scenario("normal")
    _, pipeline, config = fixture()
    receipt = pipeline.accept(message("ordinary-no-note"))
    notes, sent = [], []
    model = NativeFake(responses=[call("query_today", {"finish_after_tools": True})])
    with patch("agent.config.load_config", return_value=config):
        result = pipeline.worker(event(receipt), execute=lambda msg, **kw: run(msg, model=model, **kw),
            note_send=lambda *a, **k: notes.append(1), send=lambda *a, **k: sent.append(1) or {"data": {"message_id": "final"}})
    assert not notes and len(sent) == 1 and result["result"]["reply_source"] == "tool_facts"


def AN02():
    """说明发送回包丢失后重建不重发，独立最终UUID只交付一次终态。"""
    scenario("lost_note")


def AN03():
    """最终编辑回包丢失仅重投同消息同正文，业务与说明均不重复。"""
    scenario("lost_final")


def AN04():
    """首轮缺说明或非文本字段均继续正常任务，无额外模型补调用。"""
    for kind in ("omitted", "invalid"):
        result = scenario(kind)
        assert result["result"]["model_calls"] == 2


def AN05():
    """规划首工具仅查目标仍先说明；确认说明后重启，最终展示才提升草案。"""
    from agent.graph import Runtime
    from agent.storage.execution_pipeline import Pipeline
    objects, pipeline, config = fixture()
    receipt = pipeline.accept(message("plan-note-restart", "给我每天的菜单，保留微波炉条件。"))
    notes, edits = [], []
    class Death(BaseException):
        pass
    first = NativeFake(responses=[call("query_today", {"detail": "summary", "action_note": "我会按你的目标核算方便微波炉准备的菜单。"})])
    def note(chat, text, uuid_key):
        notes.append(text)
        return {"data": {"message_id": "plan-note"}}
    with patch("agent.config.load_config", return_value=config):
        with patch.object(Runtime, "execute", side_effect=Death):
            try:
                pipeline.worker(event(receipt), execute=lambda msg, **kw: run(msg, model=first, **kw), note_send=note)
            except Death:
                pass
            else:
                raise AssertionError("confirmed-note restart missing")
        assert len(notes) == 1 and pipeline.result(receipt["receipt_id"])["action_notice"]["status"] == "confirmed"
        pipeline = Pipeline(objects, pipeline.prefix)
        resumed = NativeFake(responses=[call("calculate_meal", {"items": [{"food_query": "夹具A", "grams": 100, "portion_basis": "suggested"}], "scope": "daily", "constraints": ["微波炉"]}, "c2"), AIMessage(content="已经核算。")])
        def edit(mid, text):
            state = pipeline.readonly_store().load_session("user", "test")
            assert state.get("meal_draft_candidate") and not state.get("meal_draft")
            assert "夹具A" in text and "100克" in text and "60千卡" in text
            edits.append((mid, text))
            return {"code": 0}
        result = pipeline.worker(event(receipt), execute=lambda msg, **kw: run(msg, model=resumed, **kw),
                                 note_send=lambda *a, **k: (_ for _ in ()).throw(AssertionError("resent note")), update_send=edit)
        assert result["status"] == "completed" and len(edits) == 1 and edits[0][0] == "plan-note"
        state = pipeline.readonly_store().load_session("user", "test")
        assert state["meal_draft"]["scope"] == "daily" and state["meal_draft"]["constraints"] == ["微波炉"]
        assert not pipeline.readonly_store().intake_items("user")


def extended_note_scenario(kind):
    from agent.graph import Runtime
    from agent.storage.execution_pipeline import Pipeline
    objects, pipeline, config = fixture()
    receipt = pipeline.accept(message("note-r5-" + kind, "按目标核算微波炉菜单。"))
    text = "我会按已核对的目标计算微波炉菜单。"
    notes, edits, sends = [], [], []
    class Death(BaseException):
        pass
    def calculate(cid, note=None, grams=100):
        args = {"scope": "daily", "items": [{"food_query": "夹具A", "grams": grams, "portion_basis": "suggested"}]}
        if note is not None:
            args["action_note"] = note
        return call("calculate_meal", args, cid)
    if kind == "second_item":
        first = AIMessage(content="", tool_calls=[
            {"name": "query_today", "args": {"action_note": "菜单已算好，设置已保存。"}, "id": "first", "type": "tool_call"},
            {"name": "query_food_nutrition", "args": {"food_query": "夹具A", "grams": 100, "action_note": text}, "id": "second", "type": "tool_call"}])
        responses = [first, calculate("calc"), AIMessage(content="无需重复菜单。")]
    else:
        responses = [call("query_today", {}, "first")]
        if kind == "direct_final":
            responses += [AIMessage(content="目前还没有足够信息安排菜单。")]
        else:
            responses += [calculate("calc", None if kind == "both_empty" else text)]
            if kind == "both_empty":
                responses += [calculate("later-calc", text, grams=200)]
            responses += [AIMessage(content="无需重复菜单。")]
    model = NativeFake(responses=responses)
    def note(chat, body, uuid_key):
        notes.append((body, uuid_key))
        saved = pipeline.readonly_store().load_execution(receipt["message"])["payload"]
        pending_batch = saved["action_note_opportunities"]["first" if kind == "second_item" else "supplement"]
        current = [e for e in saved["calls"] if e["batch_id"] == pending_batch["batch_id"]]
        assert all("result" not in e and "prepared" not in e for e in current)
        if kind != "second_item":
            assert saved["calls"][0]["result"]["status"] == "ok"
        if kind == "lost_supplement":
            raise Death()
        return {"data": {"message_id": "r5-same-note"}}
    def edit(mid, body):
        edits.append((mid, body)); return {"code": 0}
    def send(chat, body, uuid_key):
        sends.append((body, uuid_key)); return {"data": {"message_id": "r5-independent-final"}}
    execute = lambda msg, **kw: run(msg, model=model, **kw)
    original_execute = Runtime.execute
    def interrupt_calculation(runtime, entry, handler=None):
        if entry["tool"] == "calculate_meal":
            raise Death()
        return original_execute(runtime, entry, handler)
    with patch("agent.config.load_config", return_value=config):
        if kind in {"restart_supplement", "lost_supplement"}:
            try:
                if kind == "restart_supplement":
                    with patch.object(Runtime, "execute", interrupt_calculation):
                        pipeline.worker(event(receipt), execute=execute, send=send, update_send=edit, note_send=note)
                else:
                    pipeline.worker(event(receipt), execute=execute, send=send, update_send=edit, note_send=note)
            except Death:
                pass
            else:
                raise AssertionError("supplement interruption missing")
            pipeline = Pipeline(objects, pipeline.prefix)
            model = NativeFake(responses=[AIMessage(content="无需重复菜单。")])
        result = pipeline.worker(event(receipt), execute=execute, send=send, update_send=edit, note_send=note)
    assert result["status"] == "completed"
    expected_models = 4 if kind == "both_empty" else 2 if kind == "direct_final" else 3
    assert result["result"]["model_calls"] == expected_models
    if kind == "direct_final":
        assert not notes and not edits and len(sends) == 1
    elif kind == "lost_supplement":
        assert len(notes) == len(sends) == 1 and not edits and notes[0][1] != sends[0][1]
    else:
        assert len(notes) == len(edits) == 1 and not sends and edits[0][0] == "r5-same-note"
        notice = result["action_notice"]
        assert notice["opportunity"] == ("first" if kind == "second_item" else "supplement")
        assert notice["call_id"] == ("second" if kind == "second_item" else "later-calc" if kind == "both_empty" else "calc")
    return result


def AN06():
    """首批第二项才有有效说明，整批执行前发送，拒绝已完成/已保存陈述。"""
    extended_note_scenario("second_item")


def AN07():
    """首批全空在首个计算批次补发，不增加模型决策且终态同消息。"""
    extended_note_scenario("supplement")


def AN08():
    """补发确认后工具前重启，仅恢复原批次，零第二次进度。"""
    extended_note_scenario("restart_supplement")


def AN09():
    """补发ACK丢失重建后跳过重发，独立最终UUID只投递一份终态。"""
    extended_note_scenario("lost_supplement")


def AN10():
    """前两批省略时仍可在后续动作前发送；纯最终正文不补发。"""
    for kind in ("both_empty", "direct_final"):
        extended_note_scenario(kind)


def AN11():
    """写入批次的行动说明也可在执行前发送并沿同一消息交付。"""
    _, pipeline, config = fixture()
    receipt = pipeline.accept(message("ordinary-write-missing-finish", "记录夹具A100克"))
    notes = []
    model = NativeFake(responses=[call("record_meal", {"items": [{"food_query": "夹具A", "grams": 100}],
        "action_note": "我会按你给的份量记录早餐。"}), AIMessage(content="已记录60千卡。")])
    with patch("agent.config.load_config", return_value=config):
        state = pipeline.worker(event(receipt), execute=lambda msg, **kw: run(msg, model=model, **kw),
            note_send=lambda *a, **kw: notes.append(1) or {"data": {"message_id": "record-note"}},
            update_send=lambda mid, body: {"code": 0} if mid == "record-note" else None)
    assert state["status"] == "completed" and notes == [1] and state["result"]["model_calls"] == 2


CHECKS = {f"AN{n:02}": fn for n, fn in enumerate((AN01, AN02, AN03, AN04, AN05, AN06, AN07, AN08, AN09, AN10, AN11), 1)}
