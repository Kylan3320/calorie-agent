"""Independent menu-choice counterexamples through the real native agent loop."""
from copy import deepcopy
import json

from langchain_core.messages import AIMessage, ToolMessage

from agent import FakeStore
from .personal_cases import call, send, setup
from .task_domain_cases import deliver, fact, rebuild, TD03


TARGET = {"kcal": 2230, "carbs": 250, "protein": 150, "fat": 70}


def food(name, kcal, carbs, protein, fat):
    return {"food_id": name, "name": name, "aliases": [], "kcal_per_100g": kcal,
            "carbs_per_100g": carbs, "protein_per_100g": protein, "fat_per_100g": fat}


def fixture():
    s = setup()
    # The 500 g serving sizes and source per-100g values independently produce
    # the specification's fixed subtotal and A/B supplements.
    s.foods += [food("固定餐来源", 120, 12, 9, 4),
                food("补充甲", 200, 16, 25, 4), food("补充乙", 202.4, 25, 13, 5.6)]
    s.save_document("user_settings", "user", "", {"targets": TARGET,
        "daily_targets": {s.today: {"kcal": 1900, "carbs": 210, "protein": 130, "fat": 60}}})
    return s


def menu():
    return [{"item_id": "fixed", "food_query": "固定餐来源", "grams": 500, "servings": 2,
             "fixed": True, "group": "午晚餐", "portion_basis": "assumed_takeout", "assumption": "每餐组成按来源估算，不含卤蛋"},
            {"item_id": "supplement", "food_query": "补充甲", "grams": 500,
             "group": "早餐和加餐", "portion_basis": "suggested", "max_grams": 600}]


def compare(s, *, mid="quality", scope="daily", items=None, adjustment=None, **extra):
    params = {"scope": scope, "items": items or menu(), "tolerance_kcal": 200,
              "constraints": ["固定两餐无卤蛋", "微波炉"], **extra}
    if adjustment is None:
        adjustment = {"replacements": [{"item_id": "supplement", "food_query": "补充乙", "grams": 500}]}
    params["adjustment"] = adjustment
    return send(s, "按固定餐缺口安排菜单", [call("calculate_meal", params), AIMessage(content="按工具选择的菜单准备即可。")], mid=mid)


def MQ01():
    """§6.1 fixed-source 2230/250/150/70 counterexample selects B and binds exactly that draft."""
    s = fixture()
    # Query actual source refs inside the same native request, then reuse them
    # for both candidates. No expected values come from calculate_meal output.
    queries = AIMessage(content="", tool_calls=[{"name": "query_food_nutrition",
        "args": {"food_query": name, "grams": 100}, "id": f"source-{n}", "type": "tool_call"}
        for n, name in enumerate(("固定餐来源", "补充甲", "补充乙"))])
    def calculate(messages):
        refs = {}
        for m in messages:
            if isinstance(m, ToolMessage) and m.name == "query_food_nutrition":
                item = json.loads(m.content)["data"]["items"][0]
                refs[item["matched_name"]] = item["source_ref"]
        entries = menu()
        for item in entries:
            item["source_ref"] = refs[item["food_query"]]
        return call("calculate_meal", {"scope": "daily", "items": entries, "tolerance_kcal": 200,
            "constraints": ["固定两餐无卤蛋", "微波炉"], "adjustment": {"replacements": [
                {"item_id": "supplement", "food_query": "补充乙", "source_ref": refs["补充乙"], "grams": 500}]}})
    result, model = send(s, "每天固定两餐，用来源比较补充组合", [queries, calculate, AIMessage(content="采用更接近宏量目标的一版。")], mid="quality-sources")
    d = fact(result, "calculate_meal")["data"]
    assert d["fixed_subtotal"] == {"kcal": 1200, "carbs": 120, "protein": 90, "fat": 40}
    assert d["supplement_gap"] == {"kcal": 1030, "carbs": 130, "protein": 60, "fat": 30}
    a, b = d["selection"]["candidates"]
    assert a["total"] == {"kcal": 2200, "carbs": 200, "protein": 215, "fat": 60}
    assert b["total"] == {"kcal": 2212, "carbs": 245, "protein": 155, "fat": 68}
    assert a["within_tolerance"] and b["within_tolerance"]
    assert d["selection"]["selected"] == "adjustment" and d["selection"]["reason"] == "macro_pareto"
    assert d["budget_difference"] == {"kcal": 18, "carbs": 5, "protein": -5, "fat": 2}
    draft = s.load_session("user", "test")["meal_draft_candidate"]
    assert draft["calculation"] == d and draft["items"][1]["food_query"] == "补充乙"
    assert draft["items"][0]["servings"] == 2 and draft["items"][0]["fixed"]
    assert all(i["source_ref"] for c in (a, b) for i in c["items"])
    schema = next(x["function"]["parameters"] for x in model.schemas[0] if x["function"]["name"] == "calculate_meal")
    assert "adjustment" in schema["properties"]
    assert not s.intake


def MQ02():
    """Daily ignores today's target/intake; full_day and remaining use dated targets with one intake subtraction."""
    s = fixture()
    send(s, "早餐吃夹具A200克", [call("record_meal", {"items": [{"food_query": "夹具A", "grams": 200}], "meal_type": "breakfast"}), AIMessage(content="已记录。")], mid="quality-breakfast")
    ledger = deepcopy(s.intake)
    expected = {
        "daily": (TARGET, {"kcal": 0, "carbs": 0, "protein": 0, "fat": 0}, {"kcal": 1030, "carbs": 130, "protein": 60, "fat": 30}),
        "full_day": ({"kcal": 1900, "carbs": 210, "protein": 130, "fat": 60}, {"kcal": 0, "carbs": 0, "protein": 0, "fat": 0}, {"kcal": 700, "carbs": 90, "protein": 40, "fat": 20}),
        "remaining": ({"kcal": 1900, "carbs": 210, "protein": 130, "fat": 60}, {"kcal": 120, "carbs": 20, "protein": 4, "fat": 2}, {"kcal": 580, "carbs": 70, "protein": 36, "fat": 18})}
    for scope, (target, consumed, gap) in expected.items():
        result, _ = compare(s, scope=scope, mid="quality-scope-" + scope)
        d = fact(result, "calculate_meal")["data"]
        assert (d["targets"], d["consumed"], d["supplement_gap"]) == (target, consumed, gap)
        assert d["consumed_count"] == int(scope == "remaining") and s.intake == ledger
        if scope != "daily":
            assert d["selection"]["selected"] == "portion_adjustment" and d["within_tolerance"]
    TD03()  # R4 production source-row and next-day target counterexamples.


def MQ03():
    """Missing fixed portions keep a partial subtotal and cannot earn calorie tolerance or a complete choice."""
    s = fixture()
    entries = menu()
    entries[0].pop("grams")
    result, _ = compare(s, items=entries)
    d = fact(result, "calculate_meal")["data"]
    assert fact(result, "calculate_meal")["status"] == "pending"
    assert not d["fixed_complete"] and d["fixed_subtotal"] == {"kcal": 0, "carbs": 0, "protein": 0, "fat": 0}
    assert d["supplement_gap"] == TARGET and not d["complete"] and not d["within_tolerance"]
    assert d["selection"]["selected"] == "initial"
    assert all(not c["complete"] and not c["within_tolerance"] for c in d["selection"]["candidates"])
    assert len(d["unfinished"]) == 1 and not s.intake
    # Completing a missing adjustable portion is a valid feasible alternative.
    entries = menu()
    entries[1].pop("grams")
    result, _ = compare(s, items=entries, mid="quality-complete", adjustment={"replacements": [{"item_id": "supplement", "grams": 500}]})
    d = fact(result, "calculate_meal")["data"]
    assert d["complete"] and d["selection"]["reason"] == "complete_plan" and d["selection"]["selected"] == "adjustment"


def MQ04():
    """Native comparisons obey tolerance, macro tradeoff, portion preference, stable ties, and fixed/max limits."""
    s = fixture()
    # Equal macro worst deviation, different single portions: same actual food
    # amount split into servings only wins when that preference is explicit.
    split = {"replacements": [{"item_id": "supplement", "grams": 250, "servings": 2}]}
    for pref, selected, reason in (({}, "initial", "tie_keep_initial"),
        ({"portion_preference": "smaller_portions"}, "adjustment", "portion_preference")):
        result, _ = compare(s, adjustment=split, mid="quality-split-" + selected, **pref)
        d = fact(result, "calculate_meal")["data"]
        assert (d["selection"]["selected"], d["selection"]["reason"]) == (selected, reason)
    # A tempting heat-only fix outside tolerance loses to a complete inside one.
    s.foods.append(food("超热替代", 300, 26, 12, 6))
    result, _ = compare(s, mid="quality-tolerance", adjustment={"replacements": [{"item_id": "supplement", "food_query": "超热替代", "grams": 500}]})
    assert fact(result, "calculate_meal")["data"]["selection"]["reason"] == "calorie_tolerance"
    # Two in-tolerance tradeoffs: minimize the worst relative macro error.
    s.foods.append(food("取舍替代", 200, 20, 10, 10))
    result, _ = compare(s, mid="quality-tradeoff", adjustment={"replacements": [{"item_id": "supplement", "food_query": "取舍替代", "grams": 500}]})
    selection = fact(result, "calculate_meal")["data"]["selection"]
    assert (selection["selected"], selection["reason"]) == ("adjustment", "worst_macro_relative_deviation")
    assert abs(selection["candidates"][1]["worst_macro_relative_deviation"] - 2 / 7) < 1e-12
    result, _ = compare(s, mid="quality-addition", adjustment={"replacements": [{"item_id": "supplement", "grams": 250}],
        "additions": [{"item_id": "extra", "food_query": "补充乙", "grams": 250, "portion_basis": "suggested"}]})
    d = fact(result, "calculate_meal")["data"]
    assert d["selection"]["selected"] == "adjustment" and d["selection"]["reason"] == "macro_pareto"
    assert d["total"] == {"kcal": 2206, "carbs": 222.5, "protein": 185, "fat": 64}
    for mid, adjustment in (("fixed", {"replacements": [{"item_id": "fixed", "grams": 100}]}),
        ("limit", {"replacements": [{"item_id": "supplement", "grams": 700}]})):
        result, _ = compare(s, mid="quality-guard-" + mid, adjustment=adjustment)
        d = fact(result, "calculate_meal")["data"]
        assert d["selection"]["selected"] == "initial" and d["total"]["kcal"] == 2200
        if mid == "fixed":
            assert d["selection"]["rejected_adjustment"]
        else:
            assert not d["selection"]["candidates"][1]["complete"]


def MQ05():
    """Displayed selected drafts survive reconstruction and targeted replacements preserve source/fixed scope without ledger writes."""
    s = fixture()
    initial, _ = compare(s)
    old_id = fact(initial, "calculate_meal")["data"]["draft_id"]
    # An unconfirmed result cannot be edited.
    result, _ = send(rebuild(s), "改这份还没展示的菜单", [call("calculate_meal", {"draft_id": old_id, "replacements": [{"item_id": "supplement", "grams": 400}]}), AIMessage(content="请先展示菜单。")], mid="quality-unseen")
    assert fact(result, "calculate_meal")["status"] == "pending"
    deliver(s, initial)
    s = rebuild(s)
    original = deepcopy(s.load_session("user", "test")["meal_draft"])
    # Every unchanged source is resolved solely from the stored refs, even when
    # the food catalogue is unavailable after process reconstruction.
    s.foods = []
    changed, _ = send(s, "只把补充份量改400克", [call("calculate_meal", {"draft_id": old_id,
        "replacements": [{"item_id": "supplement", "grams": 400}], "constraints": ["新增准备偏好"]}), AIMessage(content="仅调整指定项。")], mid="quality-edit")
    d = fact(changed, "calculate_meal")["data"]
    candidate = s.load_session("user", "test")["meal_draft_candidate"]
    assert candidate["items"][0] == original["items"][0]
    assert candidate["items"][1]["source_ref"] == original["items"][1]["source_ref"]
    assert d["total"] == {"kcal": 2009.6, "carbs": 220, "protein": 142, "fat": 62.4}
    assert d["scope"] == "daily" and d["constraints"] == ["固定两餐无卤蛋", "微波炉", "新增准备偏好"]
    assert s.load_session("user", "test")["meal_draft"]["draft_id"] == old_id
    rebuilt = rebuild(s)
    deliver(rebuilt, changed)
    active = deepcopy(rebuilt.load_session("user", "test")["meal_draft"])
    deliver(rebuilt, initial)
    assert rebuilt.load_session("user", "test")["meal_draft"] == active
    for suffix, params in (("stale", {"draft_id": old_id, "replacements": [{"item_id": "supplement", "grams": 1}]}),
        ("rebuild", {"draft_id": active["draft_id"], "items": menu()}),
        ("missing", {"draft_id": active["draft_id"], "replacements": [{"item_id": "no-chicken", "grams": 100}]}),
        ("expand", {"draft_id": active["draft_id"], "replacements": [{"item_id": "supplement", "grams": 300}], "adjustment": {"additions": [{"item_id": "extra", "food_query": "补充乙", "grams": 100}]}})):
        result, _ = send(rebuilt, "只修改指定餐项", [call("calculate_meal", params), AIMessage(content="保留原草案，请明确要修改的项。")], mid="quality-invalid-" + suffix)
        assert fact(result, "calculate_meal")["status"] == "pending"
        assert rebuilt.load_session("user", "test")["meal_draft"] == active
    # The model's no-object branch makes no calculation and leaves the draft.
    send(rebuilt, "把鸡胸换成鸡蛋", [AIMessage(content="这份菜单没有鸡胸肉，原菜单保持。")], mid="quality-no-object")
    assert rebuilt.load_session("user", "test")["meal_draft"] == active and not rebuilt.intake


def MQ06():
    """Chicken-to-egg replacement covers every named meal group, retaining servings, fixed sources and the intake ledger."""
    s = fixture()
    s.foods += [food("鸡胸肉", 100, 0, 20, 2), food("鸡蛋", 140, 2, 12, 10)]
    send(s, "早餐吃夹具A200克", [call("record_meal", {"items": [{"food_query": "夹具A", "grams": 200}], "meal_type": "breakfast"}), AIMessage(content="已记录。")], mid="quality-egg-breakfast")
    ledger = deepcopy(s.intake)
    entries = [menu()[0], {"item_id": "chicken-lunch", "food_query": "鸡胸肉", "grams": 100,
        "group": "午餐搭配", "portion_basis": "suggested", "assumption": "微波炉熟制"},
        {"item_id": "chicken-dinner", "food_query": "鸡胸肉", "grams": 100, "servings": 2,
        "group": "晚餐搭配", "portion_basis": "suggested", "assumption": "微波炉熟制"}]
    first, _ = send(s, "每天固定两餐再搭配鸡胸", [call("calculate_meal", {"scope": "daily", "items": entries,
        "constraints": ["无卤蛋", "微波炉"], "tolerance_kcal": 200}), AIMessage(content="按该菜单准备。")], mid="quality-egg-first")
    deliver(s, first)
    s = rebuild(s)
    original = deepcopy(s.load_session("user", "test")["meal_draft"])
    second, _ = send(s, "把鸡胸换成鸡蛋", [call("calculate_meal", {"draft_id": original["draft_id"], "replacements": [
        {"item_id": key, "food_query": "鸡蛋", "grams": 100} for key in ("chicken-lunch", "chicken-dinner")]}), AIMessage(content="对应餐次已换为鸡蛋。")], mid="quality-egg-second")
    d = fact(second, "calculate_meal")["data"]
    assert d["total"] == {"kcal": 1620, "carbs": 126, "protein": 126, "fat": 70}
    assert [(i["group"], i["matched_name"], i["servings"]) for i in d["items"]] == [
        ("午晚餐", "固定餐来源", 2), ("午餐搭配", "鸡蛋", 1), ("晚餐搭配", "鸡蛋", 2)]
    candidate = s.load_session("user", "test")["meal_draft_candidate"]
    assert candidate["items"][0] == original["items"][0]
    assert candidate["constraints"] == original["constraints"]
    assert all(i["assumption"] == "微波炉熟制" for i in candidate["items"][1:])
    assert s.intake == ledger
    # Signed gap preserves an over-budget fixed meal instead of clamping to 0.
    s.save_document("user_settings", "user", "", {"targets": {"kcal": 1000, "carbs": 100, "protein": 80, "fat": 30}})
    result, _ = compare(s, mid="quality-negative-gap")
    assert fact(result, "calculate_meal")["data"]["supplement_gap"] == {"kcal": -200, "carbs": -20, "protein": -10, "fat": -10}


def MQ07():
    """One selected menu replaces duplicated model lists; resolved source offers and comparison audit stay private."""
    from agent.replies import model_view
    s = fixture()
    params = {"scope": "daily", "items": menu(), "tolerance_kcal": 200,
              "constraints": ["无卤蛋", "微波炉"], "adjustment": {"replacements": [{"item_id": "supplement", "food_query": "补充乙", "grams": 500}]}}
    result, _ = send(s, "每天固定两餐，选好一版菜单", [call("query_food_nutrition", {"food_query": "补充甲", "grams": 100}, "source"),
        call("calculate_meal", params, "selected-menu"), AIMessage(content="初稿补充甲500克。\n菜单：固定餐来源500克两份、补充乙500克。\n再列菜单：固定餐来源500克两份、补充乙500克。")], mid="one-selected-menu")
    body = result["reply"]
    assert body.count("固定餐来源") == body.count("补充乙") == body.count("菜单合计") == 1
    assert "补充甲" not in body and "营养查询选项" not in body
    assert body.count("午晚餐：") == 1 and "500克×2份" in body and "2212千卡" in body
    assert set(result["presented_candidates"]) == {"meal_draft_candidate"}
    full = fact(result, "calculate_meal")["data"]
    projected = model_view(fact(result, "calculate_meal"))["data"]
    assert full["selection"]["candidates"][0]["items"]
    assert all("items" not in c for c in projected["selection"]["candidates"])
    deliver(s, result); s = rebuild(s)
    active = s.load_session("user", "test")["meal_draft"]
    assert active["items"][1]["food_query"] == "补充乙"
    changed, model = send(s, "只把补充项改400克", [call("calculate_meal", {"draft_id": active["draft_id"],
        "replacements": [{"item_id": "supplement", "grams": 400}]}), AIMessage(content="补充甲旧菜单。补充乙又写一遍。")], mid="one-edited-menu")
    assert changed["reply"].count("补充乙") == changed["reply"].count("菜单合计") == 1 and "400克×1份" in changed["reply"]
    state = json.loads(next(m.content.removeprefix("可信会话事实：") for m in model.requests[0] if isinstance(m.content, str) and m.content.startswith("可信会话事实：")))
    assert "selection" not in state["meal_draft"]["calculation"]
    assert s.load_session("user", "test")["meal_draft"]["draft_id"] == active["draft_id"]
    deliver(s, changed); deliver(s, result)
    assert s.load_session("user", "test")["meal_draft"]["draft_id"] == fact(changed, "calculate_meal")["data"]["draft_id"]


def MQ08():
    """A nutrition lookup cannot erase the no-chicken/no-object answer or replace the active draft."""
    s = fixture(); s.foods.append(food("鸡蛋", 140, 2, 12, 10))
    first, _ = compare(s); deliver(s, first); s = rebuild(s)
    old = deepcopy(s.load_session("user", "test")["meal_draft"])
    result, _ = send(s, "把鸡胸换成鸡蛋", [call("query_food_nutrition", {"food_query": "鸡蛋", "grams": 100}),
        AIMessage(content="这份菜单没有鸡胸肉，原菜单保持。")], mid="no-object-after-nutrition")
    assert result["reply"] == "这份菜单没有鸡胸肉，原菜单保持。"
    assert not result["presented_candidates"] and s.load_session("user", "test")["meal_draft"] == old
    assert not s.intake


def MQ09():
    """Direct partial calculation displays and binds real unresolved choices; next turn uses that exact source."""
    from unittest.mock import patch
    s = setup(); s.foods += [food("白鱼", 100, 1, 20, 1), food("黑鱼", 120, 2, 21, 2)]
    with patch("agent.storage.boohee_client.search_food", return_value=[]):
        first, _ = send(s, "安排鱼100克和夹具A100克", [call("calculate_meal", {"scope": "daily", "items": [
            {"item_id": "fish", "food_query": "鱼", "grams": 100}, {"item_id": "side", "food_query": "夹具A", "grams": 100}]}),
            AIMessage(content="鱼有多个来源，请选。")], mid="partial-direct-source")
    assert "白鱼" in first["reply"] and "黑鱼" in first["reply"] and "请选择" in first["reply"]
    assert "已知小计60千卡" in first["reply"] and "菜单合计" not in first["reply"]
    assert set(first["presented_candidates"]) == {"meal_draft_candidate", "food_offer_candidate"}
    deliver(s, first); s = rebuild(s)
    session = s.load_session("user", "test"); offer = session["food_offer"]; old = session["meal_draft"]
    selected = offer["options"][1]
    def use_source(messages):
        data = json.loads(next(m.content for m in reversed(messages) if isinstance(m, ToolMessage)))["data"]
        item = data["items"][0]
        assert item["matched_name"] == selected["name"]
        return call("calculate_meal", {"draft_id": old["draft_id"], "replacements": [{"item_id": "fish", "source_ref": item["source_ref"], "grams": 100}]}, "resolved-calculation")
    second, _ = send(s, "第2个", [call("query_food_nutrition", {"choice": 2}, "choose-source"), use_source,
        AIMessage(content="已按所选来源核算。")], mid="partial-direct-choice")
    data = fact(second, "calculate_meal")["data"]
    assert data["complete"] and data["items"][0]["matched_name"] == selected["name"]
    assert "营养查询选项" not in second["reply"] and not s.intake
    assert second["completion_status"] == "completed" and first["completion_status"] == "awaiting_user"


def MQ10():
    """The sole menu keeps committed meal receipts and separately sourced knowledge answers from the same request."""
    from unittest.mock import patch
    s = fixture()
    batch = AIMessage(content="", tool_calls=[
        {"name": "record_meal", "args": {"meal_type": "breakfast", "items": [{"food_query": "夹具A", "grams": 100}]}, "id": "record", "type": "tool_call"},
        {"name": "calculate_meal", "args": {"scope": "remaining", "items": [{"food_query": "补充乙", "grams": 500}]}, "id": "calculate", "type": "tool_call"}])
    result, _ = send(s, "早餐吃了夹具A100克，记下后安排剩余菜单", [batch, AIMessage(content="早餐夹具A已记录60千卡。补充乙500克菜单重复。")], mid="mixed-record-plan")
    assert "已记" in result["reply"] and "夹具A" in result["reply"] and "60千卡" in result["reply"]
    assert result["reply"].count("补充乙") == result["reply"].count("菜单合计") == 1 and len(s.intake) == 1
    batch = AIMessage(content="", tool_calls=[
        {"name": "query_today", "args": {"detail": "calories"}, "id": "today", "type": "tool_call"},
        {"name": "calculate_meal", "args": {"scope": "daily", "items": [{"food_query": "补充乙", "grams": 500}]}, "id": "calculate", "type": "tool_call"}])
    answer, _ = send(s, "安排每天菜单，另外告诉我今天记录多少热量", [batch, AIMessage(content="今天60千卡。菜单见下方。")], mid="mixed-today-plan")
    assert s.today in answer["reply"] and "60千卡" in answer["reply"] and answer["reply"].count("菜单合计") == 1
    def retrieve(query, entries):
        return {"status": "ok", "entries": [{"answer": "全谷物可与蔬菜搭配。", "source": "资料甲"}]}
    for cited in (True, False):
        s = fixture()
        batch = AIMessage(content="", tool_calls=[
            {"name": "query_knowledge", "args": {"query": "全谷物怎样搭配"}, "id": "knowledge", "type": "tool_call"},
            {"name": "calculate_meal", "args": {"scope": "daily", "items": [{"food_query": "补充乙", "grams": 500}]}, "id": "calculate", "type": "tool_call"}])
        def final(messages):
            f = next(json.loads(m.content) for m in messages if isinstance(m, ToolMessage) and m.name == "query_knowledge")
            citation = f["data"]["entries"][0]["source_id"]
            return AIMessage(content=(f"全谷物可与蔬菜搭配。[{citation}]\n" if cited else "") + "菜单重复：补充乙500克、补充乙500克。")
        with patch("agent.tools.reads.build_knowledge_retriever", return_value=retrieve):
            result, _ = send(s, "安排菜单，并查资料解释全谷物搭配", [batch, final], mid="mixed-knowledge-plan-" + str(cited))
        assert "全谷物可与蔬菜搭配" in result["reply"] and "资料甲" in result["reply"]
        assert result["reply"].count("补充乙") == result["reply"].count("菜单合计") == 1 and not s.intake


def MQ11():
    """Known final calculation survives budget/model termination as one displayed, resumable draft."""
    from agent import run
    from .personal_cases import CONFIG, NativeFake, A
    from unittest.mock import patch
    s = fixture()
    params = {"scope": "daily", "items": menu(), "tolerance_kcal": 200,
              "adjustment": {"replacements": [{"item_id": "supplement", "food_query": "补充乙", "grams": 500}]}}
    over = AIMessage(content="", tool_calls=[{"name": "calculate_meal", "args": params, "id": f"over-{n}", "type": "tool_call"} for n in range(8)])
    result = run({"message_id": "known-draft-limit", "user_id": "user", "chat_id": "test", "text": "安排一份菜单"},
        store=s, config=CONFIG, model=NativeFake(responses=[call("calculate_meal", params), over]))
    assert result["terminal"] and result["termination_reason"] == "tool_call_limit"
    assert result["reply"].count("补充乙") == result["reply"].count("菜单合计") == 1
    assert "2212千卡" in result["reply"] and "已达上限" in result["reply"] and "补充甲" not in result["reply"]
    assert result["presented_candidates"]["meal_draft_candidate"] == result["tool_results"][7]["data"]["draft_id"]
    deliver(s, result); assert s.load_session("user", "test")["meal_draft"]["calculation"]["total"]["kcal"] == 2212
    s = FakeStore(foods=[deepcopy(A), food("白鱼", 100, 1, 20, 1), food("黑鱼", 120, 2, 21, 2)])
    def fail(messages):
        raise RuntimeError("model unavailable after partial calculation")
    with patch("agent.storage.boohee_client.search_food", return_value=[]):
        result = run({"message_id": "known-partial-model-error", "user_id": "user", "chat_id": "test", "text": "鱼和A各100克"},
            store=s, config=CONFIG, model=NativeFake(responses=[call("calculate_meal", {"scope": "daily", "items": [
                {"food_query": "鱼", "grams": 100}, {"food_query": "夹具A", "grams": 100}]}), fail]))
    assert result["terminal"] and "已知小计60千卡" in result["reply"]
    assert "白鱼" in result["reply"] and "黑鱼" in result["reply"] and "菜单合计" not in result["reply"]
    assert set(result["presented_candidates"]) == {"meal_draft_candidate", "food_offer_candidate"}
    deliver(s, result); s = rebuild(s)
    assert not s.load_session("user", "test")["meal_draft"]["calculation"]["complete"] and not s.intake


def MQ12():
    """Brand-bearing candidate profiles expose real per-100g facts; conflicting names stay pending with exact correction facts."""
    from unittest.mock import patch
    s = fixture()
    s.foods += [food("童妈 卤鸭腿", 221, 9.1, 28.3, 7.8), food("另牌 卤鸭腿", 260, 8, 23, 15),
                food("Artealca 熟甜玉米", 103, 14, 3.5, 2.6), food("另牌 熟甜玉米", 120, 20, 3, 2)]
    queries = AIMessage(content="", tool_calls=[
        {"name": "query_food_nutrition", "args": {"food_query": "卤鸭腿", "grams": 100}, "id": "duck-profile", "type": "tool_call"},
        {"name": "query_food_nutrition", "args": {"food_query": "熟甜玉米", "grams": 100}, "id": "corn-profile", "type": "tool_call"}])
    refs = {}
    def use_profiles(messages):
        options = [o for m in messages if isinstance(m, ToolMessage) for o in json.loads(m.content).get("data", {}).get("options", [])]
        for name, kcal in (("童妈 卤鸭腿", 221), ("Artealca 熟甜玉米", 103)):
            option = next(o for o in options if o["name"] == name)
            assert option["features"]["per_100g"]["kcal"] == kcal
            assert all(k in option["features"]["per_100g"] for k in ("carbs", "protein", "fat"))
            refs[name] = option["source_ref"]
        return call("calculate_meal", {"scope": "daily", "items": [
            {"source_ref": refs["童妈 卤鸭腿"], "grams": 120, "servings": 2, "fixed": True},
            {"source_ref": refs["Artealca 熟甜玉米"], "grams": 200}]}, "profile-calculation")
    with patch("agent.storage.boohee_client.search_food", return_value=[]):
        first, _ = send(s, "按候选真实营养估算组合", [queries, use_profiles, AIMessage(content="已按所选来源计算。")], mid="real-profile-plan")
    data = fact(first, "calculate_meal")["data"]
    assert data["complete"] and data["fixed_subtotal"]["kcal"] == 530.4 and data["total"]["kcal"] == 736.4
    assert data["items"][0]["matched_name"] == "童妈 卤鸭腿"
    for name in ("卤鸭腿", "水煮鸡胸肉"):
        result, _ = send(s, "核对引用冲突", [call("calculate_meal", {"scope": "daily", "items": [
            {"source_ref": refs["童妈 卤鸭腿"], "food_query": name, "grams": 120}]}), AIMessage(content="请沿用实际来源原名。")], mid="strict-name-" + name)
        data = fact(result, "calculate_meal")["data"]
        assert not data["complete"] and data["total"]["kcal"] == 0
        option = data["unfinished"][0]["candidates"][0]
        assert option["name"] == "童妈 卤鸭腿" and option["features"]["per_100g"]["kcal"] == 221
    assert not s.intake
    send(s, "记录A200克", [call("record_meal", {"items": [{"food_query": "夹具A", "grams": 200}]}), AIMessage(content="已记录120千卡。")], mid="profile-snapshot-record")
    result, _ = send(s, "查这一项的营养", [call("query_food_nutrition", {"record_id": s.intake[-1]["record_id"]}), AIMessage(content="原项200克。")], mid="profile-snapshot-query")
    snapshot_ref = fact(result, "query_food_nutrition")["data"]["options"][0]["source_ref"]
    result, _ = send(s, "名称有冲突", [call("calculate_meal", {"items": [{"source_ref": snapshot_ref, "food_query": "夹具B"}]}), AIMessage(content="请沿用原来源。")], mid="profile-snapshot-conflict")
    option = fact(result, "calculate_meal")["data"]["unfinished"][0]["candidates"][0]
    assert option["source_ref"] == snapshot_ref and option["features"]["grams"] == 200 and option["features"]["nutrition"]["kcal"] == 120
    s = rebuild(s)
    result, _ = send(s, "沿用原引用，不重问数量", [call("calculate_meal", {"items": [{"source_ref": option["source_ref"]}]}), AIMessage(content="按原份量核算。")], mid="profile-snapshot-corrected")
    assert fact(result, "calculate_meal")["data"]["total"]["kcal"] == 120 and len(s.intake) == 1


def MQ13():
    """One batch retains six source profiles plus ambiguity/missing inputs, supplementary lookup and A/B calculation within the native budget."""
    from unittest.mock import patch
    s = fixture()
    s.foods += [food("玉米", 100, 20, 3, 1), food("白鱼", 100, 1, 20, 1),
                food("黑鱼", 120, 2, 21, 2), food("花生", 550, 15, 25, 45), food("核桃", 650, 10, 15, 60)]
    names = ["固定餐来源", "补充甲", "补充乙", "夹具A", "夹具B", "玉米", "鱼", "不存在的食物"]
    queries = [{"food_query": name, "grams": 100} for name in names]
    refs = {}
    def supplements(messages):
        f = next(json.loads(m.content) for m in reversed(messages) if isinstance(m, ToolMessage))
        children = f["data"]["queries"]
        assert "items" not in f["data"] and "options" not in f["data"]
        assert [c["query"] for c in children] == queries
        assert [c["status"] for c in children] == ["ok"] * 6 + ["pending", "pending"]
        for child in children[:6]:
            option = child["data"]["options"][0]
            assert all(k in option["features"]["per_100g"] for k in ("kcal", "carbs", "protein", "fat"))
            assert child["data"]["items"][0]["source_ref"] == option["source_ref"]
            refs[option["name"]] = option["source_ref"]
        assert len(children[6]["data"]["options"]) == 2
        assert children[7]["data"]["items"] == [] and children[7]["data"]["unfinished"]
        return call("query_food_nutrition", {"queries": [{"food_query": name, "grams": 100}
            for name in ("花生米炒", "花生", "核桃")]}, "batch-supplements")
    def calculate(messages):
        children = json.loads(next(m.content for m in reversed(messages) if isinstance(m, ToolMessage)))["data"]["queries"]
        assert [c["status"] for c in children] == ["pending", "ok", "ok"]
        assert [c["query"]["food_query"] for c in children] == ["花生米炒", "花生", "核桃"]
        assert children[1]["data"]["options"][0]["features"]["per_100g"]["fat"] == 45
        assert children[2]["data"]["items"][0]["nutrition"]["fat"] == 60
        entries = menu()
        for item in entries:
            item["source_ref"] = refs[item.pop("food_query")]
        return call("calculate_meal", {"scope": "daily", "items": entries, "tolerance_kcal": 200,
            "adjustment": {"replacements": [{"item_id": "supplement", "source_ref": refs["补充乙"], "grams": 500}]}}, "batch-calculate")
    with patch("agent.storage.boohee_client.search_food", return_value=[]):
        result, model = send(s, "每天固定两餐，用真实来源比较搭配，单项适量", [
            call("query_food_nutrition", {"queries": queries}), supplements, calculate,
            AIMessage(content="采用工具选定的一版。")], mid="batch-native-menu")
    assert len(result["native_calls"]) == 3 and len(model.requests) == 4
    assert len(result["tool_results"][0]["data"]["queries"]) == 8
    assert len(result["tool_results"][1]["data"]["queries"]) == 3
    d = fact(result, "calculate_meal")["data"]
    assert d["scope"] == "daily" and d["consumed"]["kcal"] == 0
    assert d["selection"]["selected"] == "adjustment" and d["total"] == {"kcal": 2212, "carbs": 245, "protein": 155, "fat": 68}
    assert s.load_session("user", "test")["meal_draft_candidate"]["calculation"]["total"] == d["total"]
    assert not s.intake
    # The batch path keeps strict source identity: a conflicting name returns
    # correction facts; only a later exact source-ref use resolves it.
    correction_queries = [{"source_ref": refs["补充乙"], "food_query": "补充甲", "grams": 100},
                          {"source_ref": refs["补充乙"], "grams": 100}]
    result, _ = send(s, "核对来源名称冲突", [call("query_food_nutrition", {"queries": correction_queries}),
        AIMessage(content="按实际来源原名处理。")], mid="batch-source-mismatch")
    bad, good = fact(result, "query_food_nutrition")["data"]["queries"]
    assert bad["status"] == "pending" and bad["data"]["items"] == []
    assert bad["data"]["options"][0]["name"] == "补充乙"
    assert good["status"] == "ok" and good["data"]["items"][0]["matched_name"] == "补充乙"
    assert good["data"]["items"][0]["source_ref"] == refs["补充乙"] and not s.intake


def MQ14():
    """Batch offers bind the first unresolved query across reconstruction; typed bounds and mode conflicts reject without extra lookups."""
    from unittest.mock import patch
    from pydantic import ValidationError
    from agent.tools.registry import Nutrition
    from agent.graph import DeadlineExceeded
    from .personal_cases import A, B
    s = setup()
    s.foods += [food("白鱼", 100, 1, 20, 1), food("黑鱼", 120, 2, 21, 2),
                food("米饭（蒸）", 120, 25, 2, 0), food("米饭（糙米）", 110, 22, 3, 1)]
    queries = [{"food_query": name, "grams": 100} for name in ("夹具A", "鱼", "夹具B", "米饭")]
    with patch("agent.storage.boohee_client.search_food", return_value=[]):
        first, _ = send(s, "查询这些食物来源", [call("query_food_nutrition", {"queries": queries}),
            AIMessage(content="请先选择鱼的来源。")], mid="batch-offer")
    children = fact(first, "query_food_nutrition")["data"]["queries"]
    expected_options = children[1]["data"]["options"]
    candidate = s.load_session("user", "test")["food_offer_candidate"]
    assert candidate["query"] == "鱼" and candidate["options"] == expected_options
    assert candidate["options"] != children[2]["data"]["options"] and candidate["options"] != children[3]["data"]["options"]
    assert "1. 白鱼" in first["reply"] and "2. 黑鱼" in first["reply"]
    deliver(s, first)
    s = rebuild(s)
    second, _ = send(s, "第2个100克", [call("query_food_nutrition", {"choice": 2, "grams": 100}),
        AIMessage(content="已选择该来源。")], mid="batch-choice")
    item = fact(second, "query_food_nutrition")["data"]["items"][0]
    assert item["matched_name"] == "黑鱼" and item["source_ref"] == expected_options[1]["source_ref"]
    assert item["nutrition"] == {"kcal": 120, "carbs": 2, "protein": 21, "fat": 2}
    # If the first unresolved query has no candidates, later ambiguity cannot
    # publish a numbered offer pretending to answer that first query.
    no_source = FakeStore(foods=deepcopy(s.foods), today=s.today)
    with patch("agent.storage.boohee_client.search_food", return_value=[]):
        result, _ = send(no_source, "查询缺来源和鱼", [call("query_food_nutrition", {"queries": [
            {"food_query": "不存在的食物"}, {"food_query": "鱼"}, {"food_query": "夹具A"}]}),
            AIMessage(content="请先补充缺来源食物的信息。")], mid="batch-first-missing")
    assert len(fact(result, "query_food_nutrition")["data"]["queries"]) == 3
    assert not no_source.load_session("user", "test").get("food_offer_candidate")
    eight = [{"food_query": "夹具A", "grams": n * 10} for n in range(1, 9)]
    result, model = send(s, "一次查八个份量", [call("query_food_nutrition", {"queries": eight}),
        AIMessage(content="已查询各份量。")], mid="batch-eight")
    assert len(result["native_calls"]) == 1 and len(model.requests) == 2
    assert [c["data"]["items"][0]["nutrition"]["kcal"] for c in fact(result, "query_food_nutrition")["data"]["queries"]] == [6, 12, 18, 24, 30, 36, 42, 48]
    for suffix, args in (("nine", {"queries": eight + [eight[0]]}), ("zero", {"queries": []}),
        ("mixed", {"queries": eight[:1], "food_query": "夹具A"}),
        ("portion", {"queries": eight[:1], "grams": 100}),
        ("inner-action", {"queries": [{"food_query": "夹具A", "action_note": "不属于单项参数"}]})):
        try:
            Nutrition.model_validate(args)
        except ValidationError:
            pass
        else:
            raise AssertionError("invalid batch schema accepted: " + suffix)
        with patch.object(s, "load_foods", wraps=s.load_foods) as lookups:
            result, model = send(s, "查营养", [call("query_food_nutrition", args), AIMessage(content="请修正查询参数。")], mid="batch-invalid-" + suffix)
        assert not lookups.called and len(result["native_calls"]) == 1 and len(model.requests) == 2
        assert fact(result, "query_food_nutrition")["status"] == "error" and fact(result, "query_food_nutrition")["data"] == {}
    # Unexpected storage and deadline failures must propagate through the
    # existing runtime boundary rather than become a successful partial batch.
    from agent.planning import query_food_nutrition
    from types import SimpleNamespace
    for failure in (OSError("store unavailable"), DeadlineExceeded("deadline reached")):
        rt = SimpleNamespace(check_deadline=lambda: None)
        with patch("agent.planning._query_food_nutrition", side_effect=[{"status": "ok", "data": {"items": []}}, failure]):
            try:
                query_food_nutrition(rt, {"queries": [{"food_query": A["name"]}, {"food_query": B["name"]}]})
            except type(failure) as exc:
                assert str(exc) == str(failure)
            else:
                raise AssertionError("batch swallowed storage/deadline failure")
    assert not s.intake and not no_source.intake


def MQ15():
    """Batch snapshot choices keep their known amounts; projection and exceptional receipts retain per-query facts once."""
    from agent.replies import model_view, progress_reply
    s = setup()
    for mid, grams, meal in (("batch-snapshot-lunch", 100, "lunch"), ("batch-snapshot-dinner", 200, "dinner")):
        send(s, "记录夹具A", [call("record_meal", {"meal_type": meal, "items": [{"food_query": "夹具A", "grams": grams}]}), AIMessage(content="已记录。")], mid=mid)
    before = deepcopy(s.intake)
    result, _ = send(s, "查询A的原摄入和B100克营养", [call("query_food_nutrition", {"queries": [
        {"food_query": "夹具A"}, {"food_query": "夹具B", "grams": 100}]}), AIMessage(content="A有两份原摄入来源，请按餐次选择。")], mid="batch-snapshot-options")
    fact_data = fact(result, "query_food_nutrition")
    assert fact_data["status"] == "ok" and len(fact_data["data"]["queries"]) == 2
    candidate = s.load_session("user", "test")["food_offer_candidate"]
    assert candidate["query"] == "夹具A" and [o["features"]["grams"] for o in candidate["options"]] == [100, 200]
    assert "午餐" in result["reply"] and "晚餐" in result["reply"] and s.intake == before
    deliver(s, result); s = rebuild(s)
    chosen, _ = send(s, "第2个", [call("query_food_nutrition", {"choice": 2}), AIMessage(content="原晚餐A200克120千卡。")], mid="batch-snapshot-chosen")
    item = fact(chosen, "query_food_nutrition")["data"]["items"][0]
    assert item["grams"] == 200 and item["nutrition"]["kcal"] == 120 and s.intake == before
    partial, _ = send(s, "批查多个来源", [call("query_food_nutrition", {"queries": [
        {"food_query": "夹具", "grams": 100}, {"food_query": "夹具A", "grams": 100}]}), AIMessage(content="请选择首组来源。")], mid="batch-projection-partial")
    full = fact(partial, "query_food_nutrition")
    projected = model_view(full)
    assert full["data"]["queries"][0]["data"]["unfinished"][0]["candidates"]
    data = projected["data"]["queries"][0]["data"]
    assert "candidates" not in data["unfinished"][0] and data["options"][0]["features"]["per_100g"]
    fallback = progress_reply([full])
    assert "夹具A100克" in fallback and "60千卡" in fallback and s.intake == before
    def resolve_same_batch(messages):
        data = json.loads(next(m.content for m in reversed(messages) if isinstance(m, ToolMessage)))["data"]
        option = next(o for o in data["queries"][0]["data"]["options"] if o["name"] == "夹具A")
        return call("query_food_nutrition", {"queries": [{"source_ref": option["source_ref"], "grams": 100}]}, "batch-resolve")
    resolved, _ = send(s, "按已选来源核对", [call("query_food_nutrition", {"queries": [{"food_query": "夹具", "grams": 100}]}, "batch-options"),
        resolve_same_batch, AIMessage(content="所选夹具A100克为60千卡。")], mid="batch-same-message-resolved")
    assert resolved["reply"] == "所选夹具A100克为60千卡。" and not resolved["presented_candidates"]
    assert not s.load_session("user", "test").get("food_offer_candidate") and s.intake == before


def _r7_fixture():
    """Nutrition and original suggestions frozen from the R7 delivered receipt.

    reports/menu-candidate-evidence-r5-20261104.json:
    candidate.requests[4].receipt_state.result.{native_calls[2],tool_results[2]}.
    Only food facts are replayed; no deployment or user credentials are read.
    """
    s = fixture()
    rows = [food("米饭", 116, 25.9, 2.6, .3), food("卤鸭腿", 215, 9.3, 28.5, 7),
            food("鸡蛋", 139, 2.4, 13.1, 8.6), food("Artealca 煮甜玉米", 103, 14, 3.5, 2.6),
            food("土豆", 81, 17.8, 2.6, .2), food("鸡胸肉", None, 0, 21.5, 1.2)]
    rows[0]["units"] = [{"unit_name": "碗", "eat_weight": "180.00"}]
    rows[1]["units"] = [{"unit_name": "个", "eat_weight": "162.00"}]
    rows[2]["units"] = [{"unit_name": "只(中)", "eat_weight": "52.80"}, {"unit_name": "只(大)", "eat_weight": "61.60"}]
    rows[3]["units"] = [{"unit_name": "份", "eat_weight": "450.00"}]
    rows[4]["units"] = [{"unit_name": "个", "eat_weight": "99.60"}, {"unit_name": "中个", "eat_weight": "160.00"},
                         {"unit_name": "大个", "eat_weight": "216.20"}, {"unit_name": "小个", "eat_weight": "122.20"}]
    keys = ("rice", "duckleg", "egg", "corn", "potato", "chicken")
    sources = {"food:r7-" + key: {"food": row, "kind": "food"} for key, row in zip(keys, rows)}
    s.save_session("user", "test", {"food_sources": sources})
    grams = (180, 162, 105.6, 200, 200, 150)
    entries = [{"item_id": key, "source_ref": "food:r7-" + key, "grams": g,
                "servings": 2 if n < 2 else 1, "fixed": n < 2,
                "group": "午/晚餐（鸭腿饭）" if n < 2 else "早餐" if n < 4 else "加餐",
                "portion_basis": "assumed_takeout" if n < 2 else "suggested"}
               for n, (key, g) in enumerate(zip(keys, grams))]
    entries[0]["assumption"] = "按沙县鸭腿饭每份熟米饭1碗约180克估算"
    entries[1]["assumption"] = "每份1只卤鸭腿，可食约162克；不含卤蛋，配菜未计入"
    entries[2]["assumption"] = "约2只中等鸡蛋，水煮或微波加热"
    params = {"scope": "daily", "items": entries, "tolerance_kcal": 200,
              "constraints": ["每天固定两顿沙县小吃鸭腿饭，不加卤蛋", "微波炉加工", "单项食物份量不宜过多"],
              "adjustment": {"replacements": [{"item_id": key, "grams": g, "source_ref": "food:r7-" + key}
                                               for key, g in (("corn", 300), ("potato", 300), ("chicken", 200))]}}
    return s, params, rows


def _independent_r7_total(items, rows):
    from decimal import Decimal, ROUND_HALF_UP
    result = {k: Decimal(0) for k in ("carbs", "protein", "fat", "kcal")}
    for item in items:
        row = next(f for f in rows if f["name"] == item["matched_name"])
        grams = Decimal(str(item["per_serving"]["grams"])) * Decimal(str(item["servings"]))
        for k in result:
            per_100 = row[k + "_per_100g"]
            if per_100 is None:
                per_100 = Decimal(str(row["carbs_per_100g"])) * 4 + Decimal(str(row["protein_per_100g"])) * 4 + Decimal(str(row["fat_per_100g"])) * 9
            value = (Decimal(str(per_100)) * grams / 100).quantize(Decimal(".1"), rounding=ROUND_HALF_UP)
            assert item["nutrition"][k] == float(value)
            result[k] += value
    return {k: float(v) for k, v in result.items()}


def MQ16():
    """R7 real sources and original two suggestions reach tolerance in one calculation, never lose to the 158.4 g egg / 160 g chicken Pareto counterexample."""
    from unittest.mock import patch
    s, params, rows = _r7_fixture()
    with patch.object(s, "load_foods", side_effect=AssertionError("source replay must not load catalogue")), \
         patch("agent.storage.boohee_client.search_food", side_effect=AssertionError("calibration must not search")):
        result, model = send(s, "固定两餐，用原来源调建议量", [call("calculate_meal", params), AIMessage(content="采用已算好的菜单。")], mid="r7-portions")
    d = fact(result, "calculate_meal")["data"]
    assert len(result["native_calls"]) == 1 and len(model.requests) == 2
    assert d["fixed_subtotal"] == {"kcal": 1114.2, "carbs": 123.3, "protein": 101.7, "fat": 23.8}
    before, alternate, after = d["selection"]["candidates"]
    assert before["total"] == {"kcal": 1774.2, "carbs": 189.4, "protein": 160, "fat": 40.3}
    assert alternate["total"] == {"kcal": 2006.6, "carbs": 221.2, "protein": 176.8, "fat": 43.7}
    assert d["selection"]["selected"] == "portion_adjustment" and d["within_tolerance"]
    assert _independent_r7_total(d["items"], rows) == d["total"]
    # Calculate the acceptance witness independently from the source snapshot.
    witness = deepcopy(alternate["items"])
    for item in witness:
        if item["item_id"] in ("egg", "chicken"):
            g = 158.4 if item["item_id"] == "egg" else 160
            row = next(f for f in rows if f["name"] == item["matched_name"])
            item["per_serving"]["grams"] = g
            item["nutrition"] = {k: round(((row[k + "_per_100g"] if row[k + "_per_100g"] is not None else 96.8) * g / 100) + 1e-9, 1) for k in TARGET}
    witness_total = _independent_r7_total(witness, rows)
    assert witness_total == {"kcal": 2041.3, "carbs": 222.5, "protein": 175.2, "fat": 47.7}
    reference = {k: abs(TARGET[k] - witness_total[k]) for k in ("carbs", "protein", "fat")}
    actual = {k: abs(d["budget_difference"][k]) for k in reference}
    assert not (all(reference[k] <= actual[k] for k in reference) and any(reference[k] < actual[k] for k in reference))
    calibration = d["selection"]["calibration"]
    seed = next(c for c in (before, alternate) if c["variant"] == calibration["starting_variant"])
    changed = {c["item_id"] for c in calibration["changes"]}
    assert 1 <= len(changed) <= 2 and calibration["searched_items"] == 2
    for old, new in zip(seed["items"], after["items"]):
        if old["item_id"] not in changed:
            assert old == new
        assert old["source_ref"] == new["source_ref"] and old["servings"] == new["servings"]
        if old["item_id"] == "egg" and old["item_id"] in changed:
            assert "水煮或微波加热" in new["assumption"] and "约2只" not in new["assumption"]
        else:
            assert old["assumption"] == new["assumption"]
    assert not s.intake


def MQ17():
    """R7 all-user-given menu stays 2006.6/223.4; fixed quantities, assumptions, repetitions and max grams constrain correction."""
    s, params, rows = _r7_fixture()
    params.pop("adjustment")
    for item in params["items"]:
        item["portion_basis"] = "user_given"
        if item["item_id"] in ("corn", "potato"):
            item["grams"] = 300
        elif item["item_id"] == "chicken":
            item["grams"] = 200
    params["adjustment"] = {"replacements": [{"item_id": "egg", "grams": 158.4},
                                             {"item_id": "chicken", "grams": 160}]}
    result, _ = send(s, "全部份量已固定，只核算", [call("calculate_meal", params), AIMessage(content="保留指定量。")], mid="r7-all-given")
    d = fact(result, "calculate_meal")["data"]
    assert d["total"] == _independent_r7_total(d["items"], rows)
    assert d["total"]["kcal"] == 2006.6 and d["budget_difference"]["kcal"] == 223.4 and not d["within_tolerance"]
    assert d["selection"]["calibration"]["status"] == "no_adjustable_items"
    assert len(d["selection"]["candidates"]) == 1
    assert "用户给定量" in d["selection"]["rejected_adjustment"]
    params.pop("adjustment")
    # Only eggs are suggestions, capped at the original two source units.
    egg = next(i for i in params["items"] if i["item_id"] == "egg")
    egg.update(portion_basis="suggested", max_grams=105.6)
    result, _ = send(s, "鸡蛋最多两只，其余固定", [call("calculate_meal", params), AIMessage(content="保留上限。")], mid="r7-egg-cap")
    d = fact(result, "calculate_meal")["data"]
    assert d["total"]["kcal"] == 2006.6 and d["budget_difference"]["kcal"] == 223.4
    assert d["selection"]["calibration"]["status"] == "kept_original" and not s.intake


def MQ18():
    """A safe one-item correction stops there; an in-tolerance plan and no-tolerance legacy calculation never chase zero error."""
    s = fixture()
    s.save_document("user_settings", "user", "", {"targets": {"kcal": 2230, "carbs": 200, "protein": 150, "fat": 60}})
    entries = menu()
    entries[1].update(grams=350, servings=2, max_grams=450)
    params = {"scope": "daily", "items": entries, "tolerance_kcal": 200}
    result, _ = send(s, "建议量过多，保持两份", [call("calculate_meal", params), AIMessage(content="采用实算量。")], mid="one-portion")
    d = fact(result, "calculate_meal")["data"]
    assert d["within_tolerance"] and d["selection"]["calibration"]["searched_items"] == 1
    assert d["items"][1]["servings"] == 2 and d["items"][1]["per_serving"]["grams"] <= 450
    selected = deepcopy(s.load_session("user", "test")["meal_draft_candidate"]["items"])
    result, _ = send(s, "已经在容差内就沿用", [call("calculate_meal", {**params, "items": selected}), AIMessage(content="沿用这版。")], mid="inside-stop")
    final = fact(result, "calculate_meal")["data"]
    assert final["total"] == d["total"] and len(final["selection"]["candidates"]) == 1
    assert final["selection"]["calibration"]["status"] == "already_within_tolerance"
    params.pop("tolerance_kcal")
    result, _ = send(s, "未指定容差，只核算", [call("calculate_meal", params), AIMessage(content="按原建议计算。")], mid="legacy-no-tolerance")
    d = fact(result, "calculate_meal")["data"]
    assert d["items"][1]["per_serving"]["grams"] == 350 and d["total"]["kcal"] == 2600
    assert d["selection"]["calibration"]["status"] == "no_tolerance" and not s.intake


def MQ19():
    """Editing a delivered draft can calibrate only explicitly suggested replacements; numeric user replacements are fixed and every other item is identical."""
    s, params, rows = _r7_fixture()
    params.pop("adjustment")
    params.pop("tolerance_kcal")
    first, _ = send(s, "先展示原菜单", [call("calculate_meal", params), AIMessage(content="按原稿。")], mid="edit-scope-first")
    deliver(s, first)
    s = rebuild(s)
    original = deepcopy(s.load_session("user", "test")["meal_draft"])
    chicken = next(i for i in original["items"] if i["item_id"] == "chicken")
    # A suggested replacement can move; all original non-replacement
    # suggestions are frozen even though this creates a restrictive budget.
    replacement = {"item_id": "chicken", "source_ref": "food:r7-egg", "grams": 105.6, "portion_basis": "suggested", "max_grams": 211.2}
    second, _ = send(s, "只把鸡胸换成建议鸡蛋份量", [call("calculate_meal", {"draft_id": original["draft_id"],
        "tolerance_kcal": 200, "replacements": [replacement]}), AIMessage(content="仅调整指定项。")], mid="edit-scope-suggested")
    d = fact(second, "calculate_meal")["data"]
    candidate = s.load_session("user", "test")["meal_draft_candidate"]
    assert d["selection"]["calibration"]["eligible_item_ids"] == ["chicken"]
    for old, new in zip(original["items"], candidate["items"]):
        if old["item_id"] != "chicken":
            assert old == new
    assert next(i for i in candidate["items"] if i["item_id"] == "chicken")["grams"] <= 211.2
    assert chicken["grams"] == 150 and not s.intake
    replacement.pop("portion_basis")
    third, _ = send(s, "鸡胸换成可食105.6克鸡蛋，其他不动", [call("calculate_meal", {"draft_id": original["draft_id"],
        "tolerance_kcal": 200, "replacements": [replacement]}), AIMessage(content="保留明确克重。")], mid="edit-scope-given")
    d = fact(third, "calculate_meal")["data"]
    replaced = next(i for i in d["items"] if i["item_id"] == "chicken")
    assert replaced["portion_basis"] == "user_given" and replaced["per_serving"]["grams"] == 105.6
    assert d["selection"]["calibration"]["status"] == "no_adjustable_items" and not s.intake


def MQ20():
    """Rounded 105 g eggs still calibrate in real 52.8 g units; raw quantity assumptions cannot contradict the public menu."""
    s, params, rows = _r7_fixture()
    egg = next(i for i in params["items"] if i["item_id"] == "egg")
    egg.update(grams=105, assumption="约2只中蛋，可食105克；水煮或微波加热")
    result, _ = send(s, "按来源校正建议量", [call("calculate_meal", params), AIMessage(content="按实算菜单。")], mid="rounded-source-unit")
    d = fact(result, "calculate_meal")["data"]
    assert d["within_tolerance"] and _independent_r7_total(d["items"], rows) == d["total"]
    change = next(c for c in d["selection"]["calibration"]["changes"] if c["item_id"] == "egg")
    assert change["quantity"] == 3 and change["selected_grams"] == 158.4
    assert "约3只" in change["selected_assumption"] and "水煮或微波加热" in result["reply"]
    assert "约2只" not in result["reply"] and "可食105克" not in result["reply"]
    # Preserve full raw assumptions for audit, but never publish a quantity
    # accidentally attached by the model to a different food item.
    params.pop("adjustment")
    params.pop("tolerance_kcal")
    params["items"][0]["assumption"] = "按3只中等鸡蛋可食158克估算；固定组成估算"
    result, _ = send(s, "只核算既有份量", [call("calculate_meal", params), AIMessage(content="按实算份量。")], mid="misplaced-quantity-assumption")
    assert "按3只" not in result["reply"] and "固定组成估算" in result["reply"]
    assert "固定餐组成按所列来源和可食份量估算" in result["reply"]
    assert fact(result, "calculate_meal")["data"]["items"][0]["assumption"].startswith("按3只")


CHECKS = {f"MQ{n:02}": fn for n, fn in enumerate((MQ01, MQ02, MQ03, MQ04, MQ05, MQ06, MQ07, MQ08, MQ09, MQ10, MQ11, MQ12, MQ13, MQ14, MQ15, MQ16, MQ17, MQ18, MQ19, MQ20), 1)}
