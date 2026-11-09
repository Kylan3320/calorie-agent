"""Menu finishing and recent-meal references through the native runtime."""
from copy import deepcopy
import json
from unittest.mock import patch

from langchain_core.messages import AIMessage, SystemMessage

from agent import run
from .personal_cases import NativeFake, call, send, setup
from .task_domain_cases import deliver, fact, rebuild
from .receipt_runtime_cases import _direct


def draft_fixture():
    store = setup()
    first, _ = send(store, "安排菜单", [call("calculate_meal", {"scope": "daily", "items": [
        {"item_id": "fixed", "food_query": "夹具A", "grams": 200, "fixed": True},
        {"item_id": "change", "food_query": "夹具B", "grams": 100}]}),
        AIMessage(content="按这份菜单准备。")], mid="menu-first")
    deliver(store, first)
    store = rebuild(store)
    draft = deepcopy(store.load_session("user", "test")["meal_draft"])
    return store, draft


def MC01():
    """Displayed draft revision finishes in one calculation; reasons/missing identity stay in the model loop."""
    for declared in (True, False):
        store, draft = draft_fixture()
        ledger = deepcopy(store.intake)
        params = {"draft_id": draft["draft_id"], "replacements": [{"item_id": "change", "grams": 150}],
                  "finish_after_tools": declared}
        steps = [call("calculate_meal", params)]
        if not declared:
            steps += [AIMessage(content="<plan_note>为了按你的要求增加指定项，其余菜单保留。</plan_note>")]
        result, model = send(store, "指定项改150克" + ("，为什么这样换？" if not declared else ""), steps,
                             mid="menu-direct-" + str(declared))
        if declared:
            _direct(result, model)
        else:
            assert result["model_calls"] == 2 and "为了按你的要求" in result["reply"]
        data = fact(result, "calculate_meal")["data"]
        assert data["total"] == {"kcal": 345, "carbs": 20, "protein": 22, "fat": 17}
        assert store.intake == ledger
        candidate = store.load_session("user", "test")["meal_draft_candidate"]
        assert candidate["items"][0] == draft["items"][0]
        assert store.load_session("user", "test")["meal_draft"] == draft
        deliver(store, result)
        assert store.load_session("user", "test")["meal_draft"]["draft_id"] == data["draft_id"]
        replay, empty = send(rebuild(store), "指定项改150克", [], mid="menu-direct-" + str(declared))
        assert replay["reply"] == result["reply"] and not empty.requests
    for params in ({"items": [{"food_query": "夹具A", "grams": 100}]},
                   {"draft_id": "never-presented", "replacements": [{"item_id": "change", "grams": 100}]}):
        store, _ = draft_fixture()
        result, model = send(store, "安排或修改", [call("calculate_meal", {**params, "finish_after_tools": True}),
            AIMessage(content="请从有效草案继续。")], mid="menu-ineligible-" + str(bool(params.get("draft_id"))))
        assert result["model_calls"] == 2 and not result["completion"]["adopted"]
    store, draft = draft_fixture()
    result, _ = send(store, "替换并说明理由", [call("calculate_meal", {"draft_id": draft["draft_id"],
        "replacements": [{"item_id": "change", "grams": 150}]}), AIMessage(content=
        "<plan_note>这样可以沿用原来的准备方式。整份只有999千卡、蛋白质999克。只需三克就够了。</plan_note>")], mid="menu-numeric-note")
    assert "准备方式" in result["reply"] and "999" not in result["reply"] and "三克" not in result["reply"]
    assert any(a["reason"] == "quantitative_plan_note_removed" for a in result["response_adjustments"])
    from agent.replies import plan_companion_reply
    body = plan_companion_reply([{"tool": "query_knowledge", "status": "ok", "data": {"entries": [
        {"source_id": "K101", "answer": "保持均衡。", "source": "原文资料"}]}}],
        "<plan_note>保持准备方式。999千卡。[K101]</plan_note>保持均衡。[K101]",
        [{"source_id": "K101", "answer": "保持均衡。", "source": "原文资料"}])
    assert "999" not in body and "<plan_note>" not in body and body.count("保持准备方式") == 1


def MC02():
    """Pure revision action note precedes calculation, final edits it; empty note still delivers and ACK loss only re-delivers."""
    from .production_pipeline_cases import fixture, event, message
    from agent.storage.execution_pipeline import Pipeline
    from agent.graph import mark_reply_presented
    for note_present in (True, False):
        objects, pipeline, config = fixture()
        store = pipeline.store()
        first_model = NativeFake(responses=[call("calculate_meal", {"items": [
            {"item_id": "keep", "food_query": "夹具A", "grams": 200, "fixed": True},
            {"item_id": "change", "food_query": "夹具A", "grams": 100}]}), AIMessage(content="菜单。")])
        first = run(message("pipeline-menu-first", "安排菜单"), store=store, config=config, model=first_model)
        mark_reply_presented(store, "user", "test", first)
        draft = store.load_session("user", "test")["meal_draft"]
        receipt = pipeline.accept(message("pipeline-menu-edit", "指定项改150克"))
        args = {"draft_id": draft["draft_id"], "replacements": [{"item_id": "change", "grams": 150}], "finish_after_tools": True}
        if note_present:
            args["action_note"] = "我会只调整指定项，再核算完整菜单。"
        model = NativeFake(responses=[call("calculate_meal", args)])
        notes, finals = [], []
        def note(*a, **kw):
            assert not store.load_session("user", "test").get("meal_draft_candidate")
            notes.append(a)
            return {"code": 0, "data": {"message_id": "progress-mid"}}
        def final(*a, **kw):
            finals.append((a, kw))
            if len(finals) == 1:
                raise TimeoutError("final ACK lost")
            return {"code": 0, "data": {"message_id": "final-mid"}}
        with patch("agent.config.load_config", return_value=config):
            try:
                pipeline.worker(event(receipt), execute=lambda m, **kw: run(m, model=model, **kw),
                                note_send=note, send=final, update_send=final)
            except TimeoutError:
                pass
            else:
                raise AssertionError("missing delivery interruption")
            restored = Pipeline(objects, pipeline.prefix)
            result = restored.worker(event(receipt), execute=lambda *a, **k: (_ for _ in ()).throw(AssertionError("re-execution")),
                                     note_send=note, send=final, update_send=final)
        assert result["status"] == "completed" and len(finals) == 2
        _direct(result["result"], model)
        assert len(notes) == int(note_present)
        if note_present:
            assert finals[0][0][0] == finals[1][0][0] == "progress-mid"
        else:
            assert finals[0] == finals[1]


def MC03():
    """Two breakfasts: explicit most recent changes one original ID; date-only reference asks which before writing."""
    store = setup()
    for mid in ("older-breakfast", "recent-breakfast"):
        result, _ = send(store, "记录早餐", [call("record_meal", {"items": [
            {"food_query": "夹具A", "grams": 100}, {"food_query": "夹具B", "grams": 100}],
            "meal_type": "breakfast", "finish_after_tools": True})], mid=mid)
        deliver(store, result)
    original = deepcopy(store.intake)
    store = rebuild(store)
    ambiguous = rebuild(store)
    expected_id = original[2]["record_id"]
    def recent(messages):
        state = json.loads(next(m.content for m in messages if isinstance(m, SystemMessage) and m.content.startswith("可信会话事实：")).split("：", 1)[1])
        matches = [r for r in state["refs"] if r["record_id"] in state["last_meal"] and r["food_name"] == "夹具A"]
        assert len(matches) == 1 and matches[0]["record_id"] == expected_id
        return call("revise_intake", {"record_id": matches[0]["record_id"], "grams": 50, "finish_after_tools": True})
    result, model = send(store, "刚才早餐的夹具A改为50克", [recent], mid="recent-direct")
    _direct(result, model)
    assert fact(result, "revise_intake")["data"]["meal_totals"][0]["total"]["kcal"] == 180
    assert store.intake[:2] == original[:2] and store.intake[3] == original[3]
    assert store.intake[2]["record_id"] == expected_id and store.intake[2]["grams"] == 50
    replay, empty = send(rebuild(store), "刚才早餐的夹具A改为50克", [], mid="recent-direct")
    assert not empty.requests and replay["reply"] == result["reply"]
    result, _ = send(ambiguous, "今天早餐的夹具A改为50克", [call("query_history", {
        "start_date": ambiguous.today, "end_date": "2026-11-01", "meal_type": "breakfast", "food_query": "夹具A", "detail": "items"}),
        AIMessage(content="今天有两份早餐的夹具A，要改第一份还是第二份？")], mid="ambiguous-breakfast")
    assert ambiguous.intake == original and "两份" in result["reply"]
    ambiguous = rebuild(ambiguous)
    result, model = send(ambiguous, "第一份", [call("revise_intake", {
        "record_id": original[0]["record_id"], "grams": 50, "finish_after_tools": True})], mid="chosen-breakfast")
    _direct(result, model)
    assert ambiguous.intake[0]["grams"] == 50 and ambiguous.intake[1:] == original[1:]


def MC04():
    """COS traces expose confirmed start/end/range and credential-only reconstruction without credential values."""
    import os
    from agent.storage import execution_pipeline as module
    from agent.storage.telemetry import EVENTS
    from .production_pipeline_cases import Objects
    events = []
    token = EVENTS.set(events)
    try:
        with patch.object(module, "_PIPELINE", None), patch.object(module, "_PIPELINE_KEY", None), \
             patch("agent.storage.cos_transport.CosObjects", side_effect=lambda **kw: Objects()), \
             patch.dict(os.environ, {"COS_PREFIX": "offline/trace/", "COS_BUCKET": "test-bucket", "TENCENTCLOUD_SECRET_ID": "private-id", "TENCENTCLOUD_SECRET_KEY": "private-key"}):
            first = module.configured_pipeline()
            first.store()
            assert module.configured_pipeline() is first
            first.store().confirm_current()
            with patch.dict(os.environ, {"TENCENTCLOUD_SESSION_TOKEN": "private-token"}):
                second = module.configured_pipeline()
                second.store()
                assert second is not first  # Observation alone preserves R7 semantics.
        views = [e for e in events if e["kind"] == "cos_pipeline_view"]
        assert [e["reason"] for e in views] == ["first_use", "confirmed_view", "transport_credentials_changed"]
        assert views[-1]["previous_revision"] >= 0 and views[-1]["authorization_scope"] == "unknown"
        refreshes = [e for e in events if e["kind"] == "cos_refresh"]
        assert refreshes[0]["start_revision"] == -1 and refreshes[0]["end_revision"] >= 0
        assert refreshes[1]["view_id"] == refreshes[0]["view_id"] and refreshes[1]["start_revision"] == refreshes[0]["end_revision"]
        assert refreshes[-1]["view_id"] != refreshes[0]["view_id"]
        raw = json.dumps(events)
        assert all(secret not in raw for secret in ("private-id", "private-key", "private-token"))
    finally:
        EVENTS.reset(token)


def MC05():
    """Known chicken-to-egg sources: all corresponding meal items change in one decision/calculation; a reason uses the native final step."""
    from .menu_quality_cases import food
    store = setup()
    store.foods += [food("鸡胸肉", 100, 0, 20, 2), food("鸡蛋", 140, 2, 12, 10)]
    send(store, "记录早餐", [call("record_meal", {"items": [{"food_query": "夹具A", "grams": 100}],
         "meal_type": "breakfast", "finish_after_tools": True})], mid="replace-baseline")
    first, _ = send(store, "安排含鸡胸菜单", [call("query_food_nutrition", {"food_query": "鸡蛋", "grams": 100}, "known-egg"),
        call("calculate_meal", {"scope": "daily", "constraints": ["无卤蛋", "微波炉"], "items": [
            {"item_id": "fixed", "food_query": "夹具A", "grams": 200, "servings": 2, "fixed": True},
            {"item_id": "lunch", "food_query": "鸡胸肉", "grams": 80, "group": "午餐"},
            {"item_id": "dinner", "food_query": "鸡胸肉", "grams": 80, "group": "晚餐"}]}, "initial-calc"),
        AIMessage(content="按这份菜单准备。")], mid="replace-initial")
    egg_ref = fact(first, "query_food_nutrition")["data"]["items"][0]["source_ref"]
    deliver(store, first)
    original = deepcopy(store.load_session("user", "test")["meal_draft"])
    ledger = deepcopy(store.intake)
    for why in (False, True):
        current = rebuild(store)
        params = {"draft_id": original["draft_id"], "replacements": [
            {"item_id": key, "source_ref": egg_ref, "grams": 100} for key in ("lunch", "dinner")], "finish_after_tools": not why}
        steps = [call("calculate_meal", params)]
        if why:
            steps.append(AIMessage(content="<plan_note>鸡蛋也方便微波准备，可以保留其余搭配。</plan_note>"))
        result, model = send(current, "把鸡胸换成鸡蛋" + ("，为什么？" if why else ""), steps, mid="replace-exact-" + str(why))
        if not why:
            _direct(result, model)
        else:
            assert len(model.requests) == 2 and "方便微波准备" in result["reply"]
        data = fact(result, "calculate_meal")["data"]
        assert data["total"] == {"kcal": 520, "carbs": 44, "protein": 32, "fat": 24}
        assert len(result["native_calls"]) == 1 and current.intake == ledger
        candidate = current.load_session("user", "test")["meal_draft_candidate"]
        assert candidate["items"][0] == original["items"][0] and candidate["constraints"] == original["constraints"]
        assert [i["food_query"] for i in candidate["items"][1:]] == ["鸡蛋", "鸡蛋"]


def MC06():
    """Native decisions see the durable remaining budget, including the final slot reserved for prose."""
    store = setup()
    observed = []
    def decide(messages):
        state = json.loads(next(m.content for m in messages if isinstance(m, SystemMessage) and m.content.startswith("可信会话事实：")).split("：", 1)[1])
        budget = state["execution_budget"]
        observed.append(budget)
        if budget["model_decisions_after_this"] == 0:
            return AIMessage(content="已按现有资料说明，待补条件另行确认。")
        return call("query_food_nutrition", {"food_query": "夹具A", "grams": 100}, "budget-" + str(budget["model_decision"]))
    result, _ = send(store, "核对来源并说明", [decide] * 4, mid="budget-visible")
    assert [(b["model_decision"], b["model_decisions_after_this"], b["tool_calls_remaining"]) for b in observed] == [(1, 3, 8), (2, 2, 7), (3, 1, 6), (4, 0, 5)]
    assert result["status"] == "ok" and not result["termination_reason"]


def MC07():
    """Old-meal checks get per-meal totals from the same query snapshot, without a second ledger read."""
    from types import SimpleNamespace
    from agent.tools.registry import query_today
    from agent.replies import model_view
    store = setup()
    for mid in ("check-old-breakfast", "check-new-breakfast"):
        result, _ = send(store, "记录早餐", [call("record_meal", {"items": [
            {"food_query": "夹具A", "grams": 100}, {"food_query": "夹具B", "grams": 100}],
            "meal_type": "breakfast", "finish_after_tools": True})], mid=mid)
        deliver(store, result)
    result, _ = send(store, "刚才夹具A改50克", [call("revise_intake", {
        "food_query": "夹具A", "grams": 50, "finish_after_tools": True})], mid="check-revise")
    deliver(store, result)
    before = deepcopy(store.intake)
    snapshot = deepcopy(store.today_summary("user"))
    rt = SimpleNamespace(store=store, user_id="user", day=store.today, add_refs=lambda rows: None)
    with patch.object(store, "today_summary", return_value=snapshot), patch.object(
            store, "intake_items", side_effect=AssertionError("second ledger read")):
        fact = {"tool": "query_today", **query_today(rt, {"detail": "items"})}
    assert fact["data"]["total"]["kcal"] == 390
    groups = fact["data"]["meal_totals"]
    assert sorted(m["total"]["kcal"] for m in groups) == [180, 210]
    assert all(m["count"] == 2 and m["meal_type"] == "breakfast" for m in groups)
    assert model_view(fact)["data"]["meal_totals"] == groups
    assert store.intake == before
    with patch.object(store, "today_summary", return_value=snapshot):
        assert "meal_totals" not in query_today(rt, {"detail": "summary"})["data"]
    with patch.object(store, "today_summary", return_value=snapshot):
        assert "meal_totals" not in query_today(rt, {"detail": "calories"})["data"]


CHECKS = {"MC01": MC01, "MC02": MC02, "MC03": MC03, "MC04": MC04, "MC05": MC05, "MC06": MC06, "MC07": MC07}
