"""Ledger receipts exercise persisted tool facts without a model or cloud I/O."""
from copy import deepcopy
from types import SimpleNamespace

from agent.replies import model_view, tool_facts_reply
from agent.tools.registry import CURRENT_RUNTIME, TOOL_REGISTRY, entry
from agent.storage import boohee_client
from harness.eval.personal_cases import MEAL, setup, transport


class ReceiptRuntime:
    def __init__(self, store):
        self.store = store
        self.user_id = "user"
        self.session = {"unfinished": {}, "refs": []}
        self.settings = {}
        self.preset = SimpleNamespace(allow_write=True)
        self.prepared = None

    def prepare(self, value):
        self.prepared = deepcopy(value)

    def check_deadline(self):
        pass

    def add_refs(self, rows):
        self.session["refs"] = list(dict.fromkeys(self.session["refs"] + [row["record_id"] for row in rows]))


def invoke(rt, name, params, ident):
    rt.day = rt.store.today_string()
    rt.message = {"message_id": ident, "user_id": "user", "chat_id": "receipts", "text": "原始用户消息"}
    rt.action_message = rt.message
    rt.action_id = ident
    rt.prepared = None
    token = CURRENT_RUNTIME.set(rt)
    try:
        return {"tool": name, **TOOL_REGISTRY[name].invoke(params)}
    finally:
        CURRENT_RUNTIME.reset(token)


def rendered(fact):
    text = tool_facts_reply([fact])
    assert text is not None, fact
    return text


def RF01():
    """真实工具独立270→210→105→315口径，估算与多餐按最新meal_id。"""
    rt = ReceiptRuntime(setup())
    first = invoke(rt, "record_meal", MEAL, "receipt-original")
    assert first["data"]["total"] == {"carbs": 20, "protein": 16, "fat": 12, "kcal": 270}
    assert "270千卡" in rendered(first) and "估算" in rendered(first)
    revised = invoke(rt, "revise_intake", {"record_id": rt.store.intake[0]["record_id"], "grams": 100}, "receipt-revise")
    assert revised["data"]["meal_totals"][0]["total"]["kcal"] == 210
    assert "210千卡" in rendered(revised) and "270千卡" not in rendered(revised)
    # Reuse only the latest committed original meal, at half size.
    reused = invoke(rt, "record_meal", {"reuse": {"latest": True, "ratio": .5}}, "receipt-half")
    assert reused["data"]["total"]["kcal"] == 105
    assert "105千卡" in rendered(reused) and "315千卡" not in rendered(reused)
    assert "50克" in rendered(reused) and "2个" not in rendered(reused)
    today = invoke(rt, "query_today", {}, "receipt-today")
    assert today["data"]["total"]["kcal"] == 315 and "315千卡" in rendered(today)
    combined = tool_facts_reply([first, revised, reused, today])
    assert combined and "270千卡" not in combined and combined.count("210千卡") == 1 and combined.count("105千卡") == 1
    assert combined.count("315千卡") == 1 and "估算" in combined
    # Explicit reweighing clears only this meal's estimate, never another meal's.
    exact = invoke(rt, "revise_intake", {"record_id": rt.store.intake[1]["record_id"], "grams": 100}, "receipt-exact")
    assert exact["data"]["meal_totals"][0]["estimated"] is False and "估算" not in rendered(exact)
    deleted = invoke(rt, "revise_intake", {"record_id": rt.store.intake[0]["record_id"], "delete": True}, "receipt-delete")
    assert "已撤销" in rendered(deleted) and "150千卡" in rendered(deleted)
    assert "夹具A" in rendered(deleted) and "2026-10-31" in rendered(deleted)
    separate = ReceiptRuntime(setup())
    twins = [invoke(separate, "record_meal", {"items": [{"food_query": "夹具A", "grams": 100}]}, ident)
             for ident in ("receipt-twin-1", "receipt-twin-2")]
    text = tool_facts_reply(twins)
    assert text and "第1餐" in text and "第2餐" in text and text.count("60千卡") == 2 and "120千卡" not in text


def RF02():
    """部分写入、原问题与候选、跨日补全沿原餐合计以及取消原待办。"""
    rt = ReceiptRuntime(setup())
    partial = invoke(rt, "record_meal", {"meal_type": "lunch", "items": [
        {"food_query": "夹具A", "grams": 200}, {"food_query": "鱼", "grams": 100}, {"food_query": "夹具B"}]}, "receipt-partial")
    text = rendered(partial)
    assert "已记录" in text and "120千卡" in text
    assert "尚未记录" in text and "鱼" in text and "夹具B" in text and "100克" in text
    assert "请选择食物候选" in text and "1. 鱼（生）" in text and "请补充可食克重或有依据的份量" in text
    pending = rt.session["unfinished"]
    fish = next(item for item in pending.values() if item["food_query"] == "鱼")
    b = next(item for item in pending.values() if item["food_query"] == "夹具B")
    rt.store.today = "2026-11-01"

    def fish_transport(url, headers):
        if "code=fish" in url:
            return {"code": 0, "data": {"code": "fish", "name": "鱼（生）", "carbohydrate": 0, "protein": 20, "fat": 2, "calories": 110, "units": []}}
        return transport(url, headers)

    boohee_client.set_transport(fish_transport)
    completed = invoke(rt, "record_meal", {"items": [
        {"food_query": "鱼", "pending_id": fish["pending_id"], "choice": 1},
        {"food_query": "夹具B", "pending_id": b["pending_id"], "quantity": 2, "unit": "个"}]}, "receipt-complete")
    assert completed["data"]["total"]["kcal"] == 260
    assert completed["data"]["meal_totals"][0]["total"]["kcal"] == 380
    text = rendered(completed)
    assert "380千卡" in text and "260千卡" not in text and "2026-10-31午餐" in text and "2026-11-01" not in text
    assert "2个" in text and "估算" in text and not rt.session["unfinished"]
    target = next(row for row in rt.store.intake if row["food_name"] == "夹具B")
    revise_pending = invoke(rt, "revise_intake", {"record_id": target["record_id"], "quantity": 2, "unit": "袋"}, "receipt-revise-pending")
    text = rendered(revise_pending)
    assert "尚未修改" in text and "尚未记录" not in text and "2026-10-31午餐" in text and "2袋" in text
    assert target["grams"] == 100 and "当前合计" not in text
    pending_only = invoke(rt, "record_meal", {"date": "2026-10-30", "items": [{"food_query": "夹具B", "quantity": 2, "unit": "袋"}]}, "receipt-cancel-source")
    assert "2袋" in rendered(pending_only)
    ident = next(iter(rt.session["unfinished"]))
    cancelled = invoke(rt, "record_meal", {"cancel_pending": [ident]}, "receipt-cancel")
    text = rendered(cancelled)
    assert "已取消未完成项" in text and "2026-10-30" in text and "夹具B" in text and "2袋" in text
    assert "已记录" not in text and "当前合计" not in text and not rt.session["unfinished"]
    resolved = tool_facts_reply([pending_only, cancelled])
    assert resolved and "已取消" in resolved and "尚未记录" not in resolved
    ambiguous = invoke(ReceiptRuntime(setup()), "record_meal", {"items": [{"food_query": "夹具", "grams": 125}]}, "receipt-many-candidates")
    text = rendered(ambiguous)
    assert "请选择食物候选" in text and "125克" in text and "1. 夹具A" in text and "2. 夹具B" in text
    assert "已记录：" not in text


def RF03():
    """今日三种详情、历史三种详情与过滤范围完整；零记录不推断未进食。"""
    rt = ReceiptRuntime(setup())
    invoke(rt, "record_meal", MEAL, "receipt-query-seed")
    for detail in ("calories", "summary", "items"):
        fact = invoke(rt, "query_today", {"detail": detail}, "receipt-query-" + detail)
        text = rendered(fact)
        assert "270千卡" in text and "估算" in text
        if detail == "calories":
            assert not any(word in text for word in ("碳水", "蛋白质", "脂肪", "目标", "剩余", "夹具A"))
        else:
            assert all(word in text for word in ("碳水20克", "蛋白质16克", "脂肪12克", "目标", "剩余"))
        if detail == "items":
            assert "夹具A" in text and "200克" in text and "夹具B" in text and "2个" in text
    for detail in ("summary", "daily", "items"):
        params = {"start_date": "2026-10-30", "end_date": "2026-11-02", "meal_type": "lunch", "food_query": "夹具", "detail": detail}
        fact = invoke(rt, "query_history", params, "receipt-history-" + detail)
        text = rendered(fact)
        assert all(word in text for word in ("2026-10-30", "2026-11-02", "不含尾日", "午餐", "夹具", "270千卡", "碳水20克", "估算"))
        if detail == "daily":
            assert "2026-10-30未记录" in text and "2026-11-01未记录" in text and "2026-10-31已记录2项" in text
        if detail == "items":
            assert "夹具A" in text and "夹具B" in text and "200克" in text
    empty = ReceiptRuntime(setup())
    for name, details in (("query_today", ("calories", "summary", "items")), ("query_history", ("summary", "daily", "items"))):
        for detail in details:
            text = rendered(invoke(empty, name, {"detail": detail}, "receipt-empty-" + name + detail))
            assert "未记录" in text and "合计热量0千卡" not in text and "合计0千卡" not in text and "未进食" not in text and "没吃" not in text


def RF04():
    """缺字段、未知/错误/提案不直接完成，过期查询拒绝且不伪造新余额。"""
    rt = ReceiptRuntime(setup())
    meal = invoke(rt, "record_meal", MEAL, "receipt-invalid-seed")
    saved = deepcopy(meal)
    assert rendered(meal) and meal == saved
    for key in ("meal_totals", "write_status", "completed_count"):
        broken = deepcopy(meal)
        broken["data"].pop(key)
        assert tool_facts_reply([broken]) is None, key
    for status in ("error", "limit", "proposal", "unknown", "historical"):
        assert tool_facts_reply([{**meal, "status": status}]) is None
    assert tool_facts_reply([{**meal, "tool": "calculate_meal"}]) is None
    assert tool_facts_reply([]) is None and tool_facts_reply([{"tool": "record_meal", "status": "ok", "data": {}}]) is None
    assert tool_facts_reply([model_view(meal)]) is None
    today = invoke(rt, "query_today", {"detail": "summary"}, "receipt-before-write")
    missing_remaining = deepcopy(today)
    missing_remaining["data"].pop("remaining")
    assert tool_facts_reply([missing_remaining]) is None
    truncated_items = deepcopy(today)
    truncated_items["data"]["items"].pop()
    assert tool_facts_reply([truncated_items]) is None
    unknown_estimate = invoke(rt, "query_today", {}, "receipt-unknown-estimate")
    unknown_estimate["data"].pop("estimated")
    assert tool_facts_reply([unknown_estimate]) is None
    history = invoke(rt, "query_history", {"start_date": "2026-10-01", "end_date": "2026-11-01"}, "receipt-history-before-write")
    changed = invoke(rt, "revise_intake", {"record_id": rt.store.intake[0]["record_id"], "grams": 100}, "receipt-invalidating-write")
    assert tool_facts_reply([today, changed]) is None and tool_facts_reply([history, changed]) is None
    fresh = invoke(rt, "query_today", {"detail": "summary"}, "receipt-after-write")
    assert "210千卡" in tool_facts_reply([changed, fresh])
    # A write outside the earlier query range does not invalidate that result.
    other_day = invoke(rt, "record_meal", {"date": "2026-09-30", "items": [{"food_query": "夹具A", "grams": 100}]}, "receipt-unrelated-write")
    assert tool_facts_reply([history, other_day]) is not None
    # Legacy pending cancellation IDs alone cannot preserve original conditions.
    assert tool_facts_reply([{"tool": "record_meal", "status": "ok", "data": {"items": [], "meal_totals": [], "completed_count": 0, "cancelled": ["old"]}}]) is None


def RF05():
    """完成声明只进入四工具schema，默认false且调用时剔除业务控制字段。"""
    ledger = {"record_meal", "revise_intake", "query_today", "query_history"}
    for name, tool in TOOL_REGISTRY.items():
        fields = tool.args_schema.model_fields
        assert ("finish_after_tools" in fields) == (name in ledger)
        assert not tool.return_direct
        if name in ledger:
            assert fields["finish_after_tools"].default is False
            observed = []
            probe = entry(name, tool.args_schema, "probe", "read_only", lambda rt, p: observed.append(p) or p)
            params = {"finish_after_tools": True}
            token = CURRENT_RUNTIME.set(object())
            try:
                probe.invoke({})
                probe.invoke(params)
            finally:
                CURRENT_RUNTIME.reset(token)
            assert len(observed) == 2 and observed[0] == observed[1] and "finish_after_tools" not in observed[1]


CHECKS = {name: value for name, value in list(globals().items()) if name in {"RF01", "RF02", "RF03", "RF04", "RF05"}}
