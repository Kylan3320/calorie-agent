"""Conversation-domain acceptance through native tool entrypoints, without cloud/LLM."""
from copy import deepcopy

from langchain_core.messages import AIMessage

from agent import FakeStore
from agent.graph import mark_reply_presented
from agent.replies import model_view
from .personal_cases import MEAL, answer, call, send, setup


def rebuild(store):
    restored = FakeStore()
    restored.__dict__.update(deepcopy(store.__dict__))
    return restored


def deliver(store, result, chat="test"):
    mark_reply_presented(store, "user", chat, result)


def fact(result, name):
    return next(r for r in result["tool_results"] if r["tool"] == name)


def TD01():
    """Explicit multi-ID favorites/reuse are atomic, ordered, recoverable, and use saved snapshots."""
    s = setup()
    send(s, "记录夹具A200克和夹具B2个", [call("record_meal", MEAL), answer])
    aid, bid = [r["record_id"] for r in s.intake]
    send(s, "把这两项按B、A顺序保存为午餐", [call("update_user_settings", {
        "kind": "favorite", "values": {"name": "午餐", "record_ids": [bid, aid]}}), answer], mid="favorite-good")
    original = deepcopy(s.load_document("user_settings", "user")["favorites"]["午餐"])
    assert [i["matched_name"] for i in original] == ["夹具B", "夹具A"]
    send(s, "删除B", [call("revise_intake", {"record_id": bid, "delete": True}), answer], mid="delete-b")
    s = rebuild(s)
    for mid, refs in (("deleted", [aid, bid]), ("unknown", [aid, "never-shown"])):
        result, _ = send(s, "按指定两项覆盖午餐", [call("update_user_settings", {
            "kind": "favorite", "values": {"name": "午餐", "record_ids": refs}}), answer], mid=mid)
        data = fact(result, "update_user_settings")
        assert data["status"] == "pending" and data["data"]["selection"]["record_ids"] == refs
        assert s.load_document("user_settings", "user")["favorites"]["午餐"] == original
    assert s.load_session("user", "test")["failed_selection"]["record_ids"] == [aid, "never-shown"]
    before = deepcopy(s.intake)
    result, _ = send(s, "复用A和已删除的B", [call("record_meal", {"reuse": {"record_ids": [aid, bid]}}), AIMessage(content="B已删除，本次没有记录，请修订选择。")], mid="bad-reuse")
    assert fact(result, "record_meal")["status"] == "pending" and s.intake == before
    missing = fact(result, "record_meal")["data"]["selection"]["missing"]
    assert missing[0]["food_name"] == "夹具B"
    s = rebuild(s)
    send(s, "先记原来保存的半份午餐", [call("record_meal", {"reuse": {"favorite": "午餐", "ratio": .5}}), answer], mid="old-snapshot")
    assert [(r["food_name"], r["grams"], r["nutrition"]["kcal"]) for r in s.intake[-2:]] == [("夹具B", 50, 75), ("夹具A", 100, 60)]
    send(s, "常用午餐只保留A", [call("update_user_settings", {"kind": "favorite", "values": {"name": "午餐", "record_ids": [aid]}}), answer], mid="narrow")
    assert not s.load_session("user", "test").get("failed_selection")
    s = rebuild(s)
    send(s, "再记半份午餐", [call("record_meal", {"reuse": {"favorite": "午餐", "ratio": .5}}), answer], mid="new-snapshot")
    assert s.intake[-1]["nutrition"]["kcal"] == 60 and len(s.intake) == 5
    # A real record belonging to another user is still unavailable to this user.
    foreign = deepcopy(s.intake[0]); foreign.update(record_id="foreign", user_id="someone-else")
    s.intake.append(foreign)
    session = s.load_session("user", "test"); session["refs"].append("foreign"); s.save_session("user", "test", session)
    result, _ = send(s, "复用A和另一个用户的记录", [call("record_meal", {"reuse": {"record_ids": [aid, "foreign"]}}), AIMessage(content="另一个记录不可用，请重新选择。")], mid="foreign-reuse")
    assert fact(result, "record_meal")["status"] == "pending" and len(s.intake) == 6


def TD02():
    """Nutrition candidates retain source refs/order across reconstruction and stay separate from intake pending."""
    s = setup()
    send(s, "记录夹具A", [call("record_meal", {"items": [{"food_query": "夹具A"}]}), answer])
    pending = deepcopy(s.load_session("user", "test")["unfinished"])
    result, _ = send(s, "查询夹具营养", [call("query_food_nutrition", {"food_query": "夹具"}), AIMessage(content="请从营养选项中选择。")], mid="nutrition-options")
    data = fact(result, "query_food_nutrition")["data"]
    assert [o["name"] for o in data["options"][:2]] == ["夹具A", "夹具B"]
    assert all(o["source_ref"].startswith("food:") for o in data["options"])
    assert "1. 夹具A" in result["reply"] and "2. 夹具B" in result["reply"]
    assert result["presented_candidates"]["food_offer_candidate"].startswith("offer:")
    assert not s.load_session("user", "test").get("food_offer")
    deliver(s, result)
    s = rebuild(s)
    assert s.load_session("user", "test")["unfinished"] == pending
    result, _ = send(s, "第2个，每个的营养", [call("query_food_nutrition", {"choice": 2, "quantity": 1, "unit": "个"}), AIMessage(content="夹具B每个可食50克，估算75千卡。")], mid="nutrition-choice")
    chosen = fact(result, "query_food_nutrition")["data"]["items"][0]
    assert chosen["matched_name"] == "夹具B" and chosen["grams"] == 50
    assert chosen["nutrition"] == {"carbs": 0, "protein": 6, "fat": 5, "kcal": 75}
    assert "每个可食50克" in result["reply"] and "B-code" not in result["reply"]
    source_ref = chosen["source_ref"]
    s = rebuild(s)
    result, _ = send(s, "把这个来源按两份算入计划", [call("calculate_meal", {"scope": "daily", "items": [{"source_ref": source_ref, "grams": 50, "servings": 2, "portion_basis": "suggested"}]}), AIMessage(content="夹具B建议两份，每份50克，估算150千卡。")], mid="nutrition-plan")
    assert fact(result, "calculate_meal")["data"]["total"] == {"carbs": 0, "protein": 12, "fat": 10, "kcal": 150}
    assert not s.intake and s.load_session("user", "test")["unfinished"] == pending
    result, _ = send(s, "查询无效来源", [call("query_food_nutrition", {"source_ref": "food:not-returned"}), AIMessage(content="来源不可用，请重新查询食物。")], mid="invalid-source")
    assert fact(result, "query_food_nutrition")["status"] == "pending"

    # Same food in different meals must remain distinguishable in the actual
    # delivered choices, and choosing a saved intake keeps its original amount.
    snapshots = setup()
    send(snapshots, "午餐吃A100克", [call("record_meal", {"items": [{"food_query": "夹具A", "grams": 100}], "meal_type": "lunch"}), answer], mid="snapshot-lunch")
    send(snapshots, "晚餐吃A200克", [call("record_meal", {"items": [{"food_query": "夹具A", "grams": 200}], "meal_type": "dinner"}), answer], mid="snapshot-dinner")
    result, _ = send(snapshots, "查询A的营养快照", [call("query_food_nutrition", {"food_query": "夹具A"}), AIMessage(content="请从已记录份量中选择。")], mid="snapshot-options")
    options = fact(result, "query_food_nutrition")["data"]["options"]
    assert len(options) == 2 and options[0]["source_ref"] != options[1]["source_ref"]
    assert [(o["features"]["meal_type"], o["features"]["grams"]) for o in options] == [("lunch", 100), ("dinner", 200)]
    assert "1. 夹具A（2026-10-31，午餐，可食100克，60千卡）" in result["reply"]
    assert "2. 夹具A（2026-10-31，晚餐，可食200克，120千卡）" in result["reply"]
    deliver(snapshots, result)
    snapshots = rebuild(snapshots)
    result, _ = send(snapshots, "第2项", [call("query_food_nutrition", {"choice": 2}), AIMessage(content="这是已记录的晚餐营养快照。")], mid="snapshot-choice")
    chosen = fact(result, "query_food_nutrition")["data"]["items"][0]
    assert chosen["source_ref"] == options[1]["source_ref"] and chosen["grams"] == 200
    assert chosen["nutrition"] == {"carbs": 20, "protein": 4, "fat": 2, "kcal": 120}
    assert len(snapshots.intake) == 2
    # A different conversation cannot consume this conversation's opaque ref or its choice.
    result, _ = send(s, "查询此前来源", [call("query_food_nutrition", {"source_ref": source_ref}), AIMessage(content="这个会话还没有该来源，请重新查询。")], mid="other-source", chat="other")
    assert fact(result, "query_food_nutrition")["status"] == "pending"
    result, _ = send(s, "选择第1个", [call("query_food_nutrition", {"choice": 1}), AIMessage(content="请先查询并选择营养候选。")], mid="wrong-choice", chat="other")
    assert fact(result, "query_food_nutrition")["status"] == "pending"


def TD03():
    """Daily/full-day/remaining scopes use distinct targets and count repeated servings and consumed intake once."""
    s = setup()
    send(s, "今天吃了夹具A200克", [call("record_meal", {"items": [{"food_query": "夹具A", "grams": 200}]}), answer])
    s.save_document("user_settings", "user", "", {"targets": {"kcal": 1900}, "daily_targets": {s.today: {"kcal": 1500}}})
    # This case fixes portions to isolate scope/one-time intake subtraction;
    # suggested portions are now eligible for same-call calibration.
    menu = [{"food_query": "夹具A", "grams": 100, "servings": 2, "group": "午餐", "portion_basis": "user_given"},
            {"food_query": "夹具B", "quantity": 1, "unit": "个", "servings": 2, "group": "晚餐", "portion_basis": "user_given"}]
    for scope, target, consumed, diff in (("daily", 1900, 0, 1630), ("full_day", 1500, 0, 1230), ("remaining", 1500, 120, 1110)):
        s = rebuild(s)
        result, _ = send(s, "计算对应范围的菜单", [call("calculate_meal", {"scope": scope, "items": menu, "tolerance_kcal": 200}), AIMessage(content="菜单计算完成，份量为估算。")], mid="scope-" + scope)
        d = fact(result, "calculate_meal")["data"]
        assert d["total"] == {"carbs": 20, "protein": 16, "fat": 12, "kcal": 270}
        assert (d["targets"]["kcal"], d["consumed"]["kcal"], d["budget_difference"]["kcal"]) == (target, consumed, diff)
        assert [i["grams"] for i in d["items"]] == [200, 100]
        assert [i["per_serving"]["grams"] for i in d["items"]] == [100, 50]
        assert d["complete"] and not d["within_tolerance"] and d["estimated"]
    assert len(s.intake) == 1
    # Exercise the production COS source-row contract without a transport.
    class SourceTargets(FakeStore):
        def _get(self, key, default):
            assert key == "source:standards"
            return deepcopy(self.source_rows)
    sourced = SourceTargets(); sourced.__dict__.update(deepcopy(s.__dict__))
    sourced.source_rows = [{"kcal": 2100}, {"user_id": "user", "kcal": 2000},
        {"date": sourced.today, "kcal": 1700}, {"user_id": "user", "date": sourced.today, "kcal": 1600}]
    sourced.save_document("user_settings", "user", "", {"daily_targets": {sourced.today: {"kcal": 1500}}})
    for scope, target in (("daily", 2000), ("full_day", 1500), ("remaining", 1500)):
        result, _ = send(sourced, "使用来源中该范围的目标", [call("calculate_meal", {"scope": scope, "items": menu}), AIMessage(content="计算好了。")], mid="source-" + scope)
        assert fact(result, "calculate_meal")["data"]["targets"]["kcal"] == target
    deliver(sourced, result)
    displayed_id = fact(result, "calculate_meal")["data"]["draft_id"]
    sourced.today = "2026-11-01"
    sourced.save_document("user_settings", "user", "", {"daily_targets": {sourced.today: {"kcal": 1400}}})
    result, _ = send(sourced, "今天从这份草案继续", [call("calculate_meal", {"draft_id": displayed_id}), AIMessage(content="已按今天的目标重新计算。")], mid="source-next-day")
    d = fact(result, "calculate_meal")["data"]
    assert d["date"] == "2026-11-01" and d["targets"]["kcal"] == 1400 and d["consumed"]["kcal"] == 0
    assert d["total"]["kcal"] == 270 and d["budget_difference"]["kcal"] == 1130


def TD04():
    """Only delivered drafts become editable, and replacements preserve other groups, servings and constraints."""
    s = setup()
    menu = [{"item_id": "fixed", "food_query": "夹具A", "grams": 200, "servings": 2, "group": "午晚餐", "portion_basis": "suggested"},
            {"item_id": "breakfast", "food_query": "夹具B", "grams": 100, "group": "早餐", "portion_basis": "user_given"},
            {"item_id": "snack", "food_query": "夹具A", "grams": 100, "group": "加餐", "portion_basis": "suggested"}]
    first, _ = send(s, "每天两份固定餐，不加卤蛋，用微波炉搭配早餐加餐", [call("calculate_meal", {"scope": "daily", "items": menu, "constraints": ["固定餐每天两份", "不加卤蛋", "微波炉"]}), AIMessage(content="菜单已算好。")], mid="draft-first")
    first_id = fact(first, "calculate_meal")["data"]["draft_id"]
    assert "每份可食200克×2份" in first["reply"] and "菜单合计估算450千卡" in first["reply"]
    assert "不加卤蛋" in first["reply"] and first["presented_candidates"]["meal_draft_candidate"] == first_id
    assert not s.load_session("user", "test").get("meal_draft")
    deliver(s, first)
    s = rebuild(s)
    second, _ = send(s, "早餐换成A100克，其余保持", [call("calculate_meal", {"draft_id": first_id, "replacements": [{"item_id": "breakfast", "food_query": "夹具A", "grams": 100, "portion_basis": "suggested"}]}), AIMessage(content="午晚餐和加餐保持；早餐改为夹具A100克，建议菜单估算360千卡。")], mid="draft-second")
    d = fact(second, "calculate_meal")["data"]
    assert d["total"]["kcal"] == 360 and d["constraints"] == ["固定餐每天两份", "不加卤蛋", "微波炉"]
    assert [(i["item_id"], i["grams"], i["servings"]) for i in d["items"]] == [("fixed", 400, 2), ("breakfast", 100, 1), ("snack", 100, 1)]
    assert s.load_session("user", "test")["meal_draft"]["draft_id"] == first_id
    s = rebuild(s)  # Delivery has not happened: the preceding displayed draft remains active.
    deliver(s, second)
    second_id = d["draft_id"]
    assert s.load_session("user", "test")["meal_draft"]["draft_id"] == second_id
    deliver(s, first)  # An old response cannot roll a newer displayed draft back.
    assert s.load_session("user", "test")["meal_draft"]["draft_id"] == second_id
    invalid, _ = send(s, "继续旧草案", [call("calculate_meal", {"draft_id": first_id, "replacements": [{"item_id": "snack", "grams": 50}]}), AIMessage(content="请从最新已展示菜单继续修改。")], mid="draft-stale")
    assert fact(invalid, "calculate_meal")["status"] == "pending" and not s.intake


def TD05():
    """Partial plans label source/portion assumptions, preserve intake pending, and cannot claim tolerance success."""
    s = setup()
    s.save_document("user_settings", "user", "", {"targets": {"kcal": 60}})
    send(s, "记录夹具A", [call("record_meal", {"items": [{"food_query": "夹具A"}]}), answer])
    pending = deepcopy(s.load_session("user", "test")["unfinished"])
    result, _ = send(s, "估算外卖组成，但B的份量还不清楚", [call("calculate_meal", {"scope": "daily", "tolerance_kcal": 200, "constraints": ["外卖无卤蛋"], "items": [
        {"food_query": "夹具A", "grams": 100, "portion_basis": "assumed_takeout", "assumption": "假设可食组成100克，配方未核实"},
        {"food_query": "夹具B", "portion_basis": "suggested"}]}), AIMessage(content="已知部分为估算小计60千卡；外卖组成是假设，B尚缺份量，不能判断全份菜单是否满足目标。")], mid="partial-plan")
    d = fact(result, "calculate_meal")["data"]
    assert d["total"]["kcal"] == 60 and d["budget_difference"]["kcal"] == 0
    assert not d["complete"] and not d["within_tolerance"] and len(d["unfinished"]) == 1
    assert d["items"][0]["portion_basis"] == "assumed_takeout" and d["items"][0]["snapshot"]["estimated"]
    assert d["items"][0]["assumption"] == "假设可食组成100克，配方未核实"
    assert not s.intake and s.load_session("user", "test")["unfinished"] == pending
    deliver(s, result); s = rebuild(s)
    draft_id = d["draft_id"]
    result, _ = send(s, "B补50克", [call("calculate_meal", {"draft_id": draft_id, "replacements": [{"item_id": "I2", "grams": 50, "portion_basis": "user_given"}]}), AIMessage(content="B补为50克，菜单估算135千卡。")], mid="complete-plan")
    d = fact(result, "calculate_meal")["data"]
    assert d["complete"] and d["total"]["kcal"] == 135 and d["within_tolerance"]


def TD06():
    """Missing-time reminder intent survives another topic/reconstruction and completes the original rule exactly once."""
    s = setup(); s.save_document("user_settings", "user", "", {"feishu_chat_id": "bound-chat"})
    first, _ = send(s, "启用午餐提醒", [call("update_user_settings", {"kind": "reminder", "values": {"enabled": True, "meal_type": "lunch"}}), AIMessage(content="午餐提醒规则已保存，尚未启用；请补充提醒时间。")], mid="reminder-initial")
    d = fact(first, "update_user_settings")["data"]; rule_id = d["rule_id"]
    assert not d["enabled"] and d["missing"] == ["提醒时间 HH:MM"]
    send(s, "先聊别的", [AIMessage(content="可以，想聊什么？")], mid="reminder-topic")
    s = rebuild(s)
    assert s.load_session("user", "test")["reminder_pending"]["rule_id"] == rule_id
    result, _ = send(s, "12点", [call("update_user_settings", {"kind": "reminder", "values": {"meal_type": "lunch", "time": "12:00"}}), AIMessage(content="午餐提醒已启用，每天12点提醒。")], mid="reminder-time")
    d = fact(result, "update_user_settings")["data"]
    assert d["rule_id"] == rule_id and d["enabled"] and not d["missing"]
    rules = s.load_document("user_settings", "user")["reminders"]
    assert list(rules) == [rule_id] and rules[rule_id]["time"] == "12:00"
    assert not s.load_session("user", "test").get("reminder_pending")
    s = rebuild(s)
    again, model = send(s, "12点", [], mid="reminder-time")
    assert not model.requests and again["reply"] == result["reply"]
    assert s.load_document("user_settings", "user")["reminders"] == rules
    # Explicit cancellation of the disabled rule removes the saved enable intent.
    send(s, "启用晚餐提醒", [call("update_user_settings", {"kind": "reminder", "values": {"enabled": True, "meal_type": "dinner"}}), AIMessage(content="晚餐提醒已保存，尚缺时间。")], mid="dinner-pending")
    dinner_id = s.load_session("user", "test")["reminder_pending"]["rule_id"]
    send(s, "取消晚餐提醒", [call("update_user_settings", {"kind": "reminder", "values": {"rule_id": dinner_id, "enabled": False}}), answer], mid="dinner-cancel")
    assert not s.load_session("user", "test").get("reminder_pending")
    send(s, "启用提醒，每天8点", [call("update_user_settings", {"kind": "reminder", "values": {"enabled": True, "time": "08:00"}}), AIMessage(content="规则已保存，尚未启用；请补充餐次。")], mid="missing-meal")
    no_meal_id = s.load_session("user", "test")["reminder_pending"]["rule_id"]
    s = rebuild(s)
    result, _ = send(s, "早餐", [call("update_user_settings", {"kind": "reminder", "values": {"meal_type": "breakfast", "time": "08:00"}}), AIMessage(content="早餐提醒已启用。")], mid="complete-meal")
    assert fact(result, "update_user_settings")["data"]["rule_id"] == no_meal_id
    assert fact(result, "update_user_settings")["data"]["enabled"]
    send(s, "再启用晚餐提醒", [call("update_user_settings", {"kind": "reminder", "values": {"enabled": True, "meal_type": "dinner"}}), AIMessage(content="规则已保存，尚缺时间。")], mid="bare-cancel-open")
    send(s, "取消", [AIMessage(content="已取消这次启用请求。")], mid="bare-cancel")
    assert not s.load_session("user", "test").get("reminder_pending")


def TD07():
    """Historical follow-ups inherit and explicitly clear filters, and each detail preserves its actual scope."""
    s = setup()
    send(s, "午餐A200克", [call("record_meal", {"items": [{"food_query": "夹具A", "grams": 200}], "meal_type": "lunch"}), answer])
    send(s, "晚餐B100克", [call("record_meal", {"items": [{"food_query": "夹具B", "grams": 100}], "meal_type": "dinner"}), answer], mid="history-dinner")
    result, _ = send(s, "看今天午餐A合计", [call("query_history", {"period": "today", "meal_type": "lunch", "food_query": "夹具A"}), AIMessage(content="今天午餐夹具A合计120千卡。")], mid="history-summary")
    scope = fact(result, "query_history")["data"]
    assert scope["total"]["kcal"] == 120 and scope["filters"] == {"meal_type": "lunch", "food_query": "夹具A"}
    s = rebuild(s)
    for detail in ("daily", "items"):
        result, _ = send(s, "展开这组结果", [call("query_history", {"continue_scope": True, "detail": detail}), AIMessage(content="今天午餐夹具A合计120千卡。")], mid="history-" + detail)
        d = fact(result, "query_history")["data"]
        assert d["start_date"] == scope["start_date"] and d["end_date"] == scope["end_date"] and d["total"]["kcal"] == 120
        projected = model_view(fact(result, "query_history"))["data"]
        assert projected["meal_type"] == "lunch" and projected["food_query"] == "夹具A"
    result, _ = send(s, "看这天所有餐食", [call("query_history", {"continue_scope": True, "meal_type": "", "food_query": ""}), AIMessage(content="今天合计270千卡。")], mid="history-clear")
    d = fact(result, "query_history")["data"]
    assert d["count"] == 2 and d["total"]["kcal"] == 270 and d["filters"] == {"meal_type": "", "food_query": ""}

    # The requested weekly boundary, meal and food must all survive the real
    # reply and the reconstructed follow-up, including a current-ledger change.
    seeds = []
    for day, meal, name, grams, kcal in (
        ("2026-10-26", "breakfast", "鸡蛋", 50, 75),
        ("2026-11-01", "breakfast", "鸡蛋", 100, 150),
        ("2026-10-26", "lunch", "鸡蛋", 200, 300),
        ("2026-10-25", "breakfast", "鸡蛋", 50, 75),
        ("2026-11-02", "breakfast", "鸡蛋", 50, 75),
        ("2026-10-27", "breakfast", "米饭", 100, 120),
    ):
        seeds.append({"date": day, "meal_type": meal, "food_name": name,
                      "user_id": "user", "grams": grams, "kcal": kcal})
    weekly = FakeStore(today="2026-11-04", intake_seed=seeds)
    first, _ = send(weekly, "上周早餐的鸡蛋多少热量", [call("query_history", {
        "period": "last_week", "meal_type": "breakfast", "food_query": "鸡蛋"}),
        AIMessage(content="上周早餐鸡蛋共2项，合计225千卡。")], mid="weekly-eggs")
    scope = fact(first, "query_history")["data"]
    assert (scope["start_date"], scope["end_date"], scope["count"], scope["total"]["kcal"]) == ("2026-10-26", "2026-11-02", 2, 225)
    deliver(weekly, first); weekly = rebuild(weekly)
    # An independent change after the first answer proves the follow-up reads
    # the selected ledger again, rather than replaying the old count/total.
    weekly.intake[0]["status"] = "deleted"
    def count_current(messages):
        from langchain_core.messages import ToolMessage
        import json
        current = json.loads(next(m.content for m in reversed(messages) if isinstance(m, ToolMessage)))["data"]
        assert current["meal_type"] == "breakfast" and current["food_query"] == "鸡蛋"
        return AIMessage(content=f"上周早餐的鸡蛋目前{current['count']}项，合计{current['total']['kcal']:g}千卡。")
    for detail in ("summary", "daily", "items"):
        current, _ = send(weekly, "那有几项", [call("query_history", {"continue_scope": True, "detail": detail}), count_current], mid="weekly-count-" + detail)
        data = fact(current, "query_history")["data"]
        assert data["start_date"] == "2026-10-26" and data["end_date"] == "2026-11-02"
        assert data["count"] == 1 and data["total"]["kcal"] == 150 and "目前1项" in current["reply"]
        projected = model_view(fact(current, "query_history"))["data"]
        assert projected["meal_type"] == "breakfast" and projected["food_query"] == "鸡蛋"


def TD08():
    """原话鸭腿饭完整任务：两份无卤蛋组成假设、微波炉菜单、独立1200及跨轮只换鸡胸。"""
    # Offline source fixtures, deliberately fixed here; these are not claims
    # about a real takeaway shop's recipe or actual nutritional composition.
    foods = []
    for name, c, p, f, kcal in (("米饭", 28, 2, 0, 120), ("鸭腿肉", 0, 20, 10, 200),
                              ("鸡胸肉", 0, 20, 2, 100), ("西兰花", 4, 3, .2, 25),
                              ("鸡蛋", 2, 12, 10, 140), ("早餐夹具", 10, 10, 2, 100)):
        foods.append({"food_id": name, "name": name, "carbs_per_100g": c, "protein_per_100g": p,
                      "fat_per_100g": f, "kcal_per_100g": kcal, "detail_loaded": True})
    s = FakeStore(foods=foods, today="2026-11-04")
    s.save_document("user_settings", "user", "", {"targets": {"kcal": 1800}})
    send(s, "早餐已吃早餐夹具600克", [call("record_meal", {"meal_type": "breakfast", "items": [{"food_query": "早餐夹具", "grams": 600}]}), answer], mid="duck-breakfast")
    constraints = ["每天两顿沙县鸭腿饭", "两顿均不加卤蛋", "其余食材能用微波炉准备", "单项份量适量"]
    assumption = "每顿鸭腿饭暂按米饭250克和可食鸭腿肉100克组成估算；配方未核实，无卤蛋为用户要求"
    menu = [{"item_id": "rice", "group": "午晚两顿固定餐", "food_query": "米饭", "grams": 250, "servings": 2, "portion_basis": "assumed_takeout", "assumption": assumption},
            {"item_id": "duck", "group": "午晚两顿固定餐", "food_query": "鸭腿肉", "grams": 100, "servings": 2, "portion_basis": "assumed_takeout", "assumption": assumption},
            {"item_id": "chicken", "group": "微波炉搭配", "food_query": "鸡胸肉", "grams": 100, "portion_basis": "suggested"},
            {"item_id": "vegetable", "group": "微波炉搭配", "food_query": "西兰花", "grams": 200, "servings": 2, "portion_basis": "suggested"}]
    prompt = "每天两顿沙县鸭腿饭，都不加卤蛋，再搭配方便微波炉准备的食物；单项适量，可差200千卡。"
    daily, _ = send(s, prompt, [call("calculate_meal", {"scope": "daily", "items": menu, "constraints": constraints, "tolerance_kcal": 200}), AIMessage(content="给你一份带组成假设的初版。")], mid="duck-daily")
    d = fact(daily, "calculate_meal")["data"]
    assert d["total"] == {"carbs": 156, "protein": 82, "fat": 22.8, "kcal": 1200}
    assert d["consumed"]["kcal"] == 0 and d["budget_difference"]["kcal"] == 600 and not d["within_tolerance"]
    remaining, _ = send(s, "同样菜单按今天剩余安排，已有早餐600千卡", [call("calculate_meal", {"scope": "remaining", "items": menu, "constraints": constraints, "tolerance_kcal": 200}), AIMessage(content="已按今天剩余安排重新核算。")], mid="duck-remaining")
    d = fact(remaining, "calculate_meal")["data"]
    assert d["total"]["kcal"] == 1200 and d["consumed"]["kcal"] == 600 and d["budget_difference"]["kcal"] == 0 and d["within_tolerance"]
    assert "250克×2份" in remaining["reply"] and "100克×2份" in remaining["reply"]
    assert "配方未核实" in remaining["reply"] and "不加卤蛋" in remaining["reply"] and "微波炉" in remaining["reply"]
    deliver(s, remaining); s = rebuild(s)
    old = deepcopy(s.load_session("user", "test")["meal_draft"])
    changed, _ = send(s, "把鸡胸肉换成鸡蛋", [call("calculate_meal", {"draft_id": old["draft_id"], "replacements": [{"item_id": "chicken", "food_query": "鸡蛋", "grams": 100, "portion_basis": "suggested"}]}), AIMessage(content="已只替换鸡胸肉，其余组合保留。")], mid="duck-egg")
    d = fact(changed, "calculate_meal")["data"]
    assert d["total"] == {"carbs": 158, "protein": 74, "fat": 30.8, "kcal": 1240}
    assert d["consumed"]["kcal"] == 600 and d["budget_difference"]["kcal"] == -40
    assert d["constraints"] == constraints and d["complete"] and d["within_tolerance"]
    for before, after in zip(old["items"], s.load_session("user", "test")["meal_draft_candidate"]["items"]):
        if before["item_id"] != "chicken":
            assert before == after
    assert len(s.intake) == 1 and s.intake[0]["nutrition"]["kcal"] == 600
    assert not s.load_session("user", "another-chat").get("meal_draft")


def TD09():
    """提醒已提交后模型失败，实际异常回执说明关闭并问时间；重建后只启用原规则。"""
    from agent import run
    from .personal_cases import CONFIG, NativeFake
    s = setup()
    s.save_document("user_settings", "user", "", {"feishu_chat_id": "bound-chat"})
    def fail(messages):
        raise RuntimeError("injected model failure after settings commit")
    result = run({"message_id": "reminder-model-failed", "user_id": "user", "chat_id": "test", "text": "帮我启用午餐提醒"},
        store=s, config=CONFIG, model=NativeFake(responses=[call("update_user_settings", {"kind": "reminder", "values": {"meal_type": "lunch", "enabled": True}}), fail]))
    assert result["terminal"] and result["status"] == "error"
    assert "已保存" in result["reply"] and "尚未启用" in result["reply"] and "时间" in result["reply"]
    rules = s.load_document("user_settings", "user")["reminders"]
    assert len(rules) == 1 and not next(iter(rules.values()))["enabled"]
    rule_id = next(iter(rules))
    deliver(s, result); s = rebuild(s)
    updated, _ = send(s, "12点", [call("update_user_settings", {"kind": "reminder", "values": {"time": "12:00"}}), AIMessage(content="午餐提醒已启用。")], mid="reminder-after-failure")
    assert fact(updated, "update_user_settings")["data"]["rule_id"] == rule_id
    rules = s.load_document("user_settings", "user")["reminders"]
    assert list(rules) == [rule_id] and rules[rule_id]["enabled"] and rules[rule_id]["time"] == "12:00"


CHECKS = {f"TD{n:02}": fn for n, fn in enumerate((TD01, TD02, TD03, TD04, TD05, TD06, TD07, TD08, TD09), 1)}
