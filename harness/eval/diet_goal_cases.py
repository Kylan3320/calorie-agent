"""Targeted proof of goal versions, delegated calculation and delivery boundaries."""
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import json
import time

from langchain_core.messages import AIMessage, ToolMessage

from agent import FakeStore, run
from agent.graph import mark_reply_presented
from agent.storage import diet_goal_runtime as background, diet_goals, experience
from agent.storage.cos_journal import Journal
from harness.eval.personal_cases import CONFIG, NativeFake, call
from harness.eval.production_pipeline_cases import Objects


def _day(days=0):
    return (date.fromisoformat(experience.local_day(CONFIG)) + timedelta(days=days)).isoformat()


def _reply(content="已经处理这条委托。"):
    return AIMessage(content=content)


def _food(name, kcal):
    return {"food_id": name, "name": name, "kcal_per_100g": kcal,
            "carbs_per_100g": 20, "protein_per_100g": 10, "fat_per_100g": 1, "detail_loaded": True}


def _fixture():
    return FakeStore(foods=[_food("米饭", 116), _food("鸡胸肉", 133)], today=_day())


def _send(store, user_text, mid, steps, *, chat="first", user="user", delivered=True, model=None, config=CONFIG):
    model = model or NativeFake(responses=steps)
    message = {"message_id": mid, "user_id": user, "chat_id": chat, "delivery_source": "feishu", "text": user_text}
    result = run(message, store=store, config=config, model=model)
    assert result["status"] == "ok", result["errors"]
    if delivered:
        mark_reply_presented(store, user, chat, result)
    return result


def _control(store, operation, mid, *, text=None, goal=None, **kwargs):
    text = text or operation
    args = {"operation": operation, "user_quote": text, **kwargs}
    if goal:
        args.update(goal_id=goal["id"], revision=goal["revision"])
    return _send(store, text, mid, [call("manage_diet_goal", args), _reply()], chat="other")["tool_results"][0]


def _submit(messages, *, notify=True, reason="按现有食材安排午餐"):
    result = json.loads(next(message.content for message in reversed(messages) if isinstance(message, ToolMessage)))
    ids = [item["draft_id"] for item in result["data"]["days"] if item["status"] == "ok"]
    return call("submit_goal_result", {"outcome": "plan", "selected_drafts": ids, "reason": reason, "notify": notify}, "submit")


def _tick(store, progress, model=None, *, publish=True, send=None, config=CONFIG, now=None):
    events = []
    result = background.run_next(store, progress, config, dispatch=events.append, model=model, now=now)
    if not events or not publish:
        return result, events[0] if events else None
    return background.writer(store, events[0], config, send=send, now=now), events[0]


def _plan(days, food="米饭"):
    return call("calculate_meal", {"days": [{"date": day, "items": [{"food_query": food, "grams": 100, "group": "午餐"}]}
                                            for day in days]})


def DG01():
    """Weekly tool results retain locks and separate ready, delivered and stopped work."""
    store = _fixture()
    progress = Journal(Objects(), "goal-test/sync/")
    days = [_day(n) for n in range(1, 8)]
    initial = _control(store, "start", "start", enabled=True, spec={"objective": "跟进一周午餐", "done_when": "七天午餐安排",
        "starts": days[0], "ends": days[-1], "meals": ["lunch"], "triggers": ["feedback", "intake"], "notify_here": True})
    assert initial["status"] == "ok"
    ident = diet_goals.load(store, "user")["id"]
    assert _tick(store, progress)[0]["status"] == "running"
    assert _tick(store, progress, NativeFake(responses=[_plan(days), _submit]))[0]["status"] == "applied"
    goal = diet_goals.load(store, "user")
    assert goal["latest"] == 1 and not goal.get("delivered") and not goal["missing_dates"]
    plans = diet_goals.baseline(store, "user", goal)
    assert {day: plan["calculation"]["total"]["kcal"] for day, plan in plans.items()} == {day: 116 for day in days}

    status = _send(store, "只看状态", "status", [call("query_diet_goal", {}), _reply()], chat="lookup")
    assert "goal_delivery_ref" not in status and not diet_goals.load(store, "user").get("delivered")
    denied = _send(store, "查看这个委托", "other-user", [call("query_diet_goal", {"goal_id": ident}), _reply()], user="another")
    assert denied["tool_results"][0]["data"]["reason"] == "goal_not_found"
    show = _send(store, "看最新菜单", "show", [call("query_diet_goal", {"operation": "show_latest", "view_quote": "看最新菜单"}), _reply()], delivered=False)
    assert all(day in show["reply"] for day in days) and not diet_goals.load(store, "user").get("delivered")
    mark_reply_presented(store, "user", "first", show)
    assert diet_goals.load(store, "user")["delivered"] == 1

    _control(store, "revise", "revise", goal=goal, spec={"constraints": ["只用微波炉"], "locks": [{"date": days[0], "meal": "lunch"}]})
    stale_show = _send(store, "看修订后的菜单", "stale-show", [call("query_diet_goal", {"operation": "show_latest", "view_quote": "看修订后的菜单"}), _reply()])
    assert stale_show["tool_results"][0]["data"]["reason"] == "revised_plan_not_ready"
    assert _tick(store, progress)[0]["status"] == "running"
    active = diet_goals.load(store, "user")
    _control(store, "set_auto", "auto-off", goal=active, enabled=False)
    assert diet_goals.load(store, "user")["run"]["status"] == "running"
    _tick(store, progress, NativeFake(responses=[_plan(days[:2], "鸡胸肉"), _submit]))
    revised = diet_goals.load(store, "user")
    assert revised["latest"] == 2 and revised["delivered"] == 1 and not revised["auto"]
    plans = diet_goals.baseline(store, "user", revised)
    assert plans[days[0]]["calculation"]["total"]["kcal"] == 116
    assert plans[days[1]]["calculation"]["total"]["kcal"] == 133
    assert plans[days[2]]["calculation"]["total"]["kcal"] == 116
    send_calls = []

    def send(dest, text, uuid_key):
        send_calls.append((dest, text, uuid_key))
        if len(send_calls) == 1:
            raise ConnectionError("simulated delivery interruption")
        return {"data": {"message_id": "sent"}}

    try:
        _tick(store, progress, send=send)
    except ConnectionError:
        pass
    else:
        raise AssertionError("failed delivery was treated as confirmed")
    assert diet_goals.load(store, "user")["delivered"] == 1
    assert _tick(store, progress, send=send)[0]["status"] == "delivered"
    assert send_calls[0] == send_calls[1] and diet_goals.load(store, "user")["delivered"] == 2

    _control(store, "request_run", "manual", goal=revised)
    _tick(store, progress)
    _, delayed = _tick(store, progress, NativeFake(responses=[_plan(days[1:2]), _submit]), publish=False)
    _control(store, "stop_run", "stop", goal=revised)
    assert diet_goals.load(store, "user")["run"]["status"] == "stop_requested"
    assert background.writer(store, delayed, CONFIG, send=send)["status"] == "obsolete"
    assert diet_goals.load(store, "user")["latest"] == 2
    _control(store, "pause", "pause", goal=revised)
    assert _tick(store, progress)[0]["status"] == "idle"
    _control(store, "resume", "resume", goal=revised)
    _tick(store, progress)
    _control(store, "set_notifications", "mute", goal=revised, enabled=False)
    unchanged = call("submit_goal_result", {"outcome": "unchanged", "reason": "现有午餐仍适用", "notify": True})
    assert not _tick(store, progress, NativeFake(responses=[unchanged]))[0]["changed"]
    assert _tick(store, progress)[0]["status"] == "idle"
    _control(store, "end", "end", goal=revised)
    assert _tick(store, progress)[0]["status"] == "idle"
    assert not store.intake and not store.load_document("user_settings", "user")


def DG02():
    """Each authorised time slot is unique and changed sources fence a pending result."""
    store = _fixture()
    progress = Journal(Objects(), "goal-time/sync/")
    first, last = _day(1), _day(2)
    local = timezone(timedelta(hours=CONFIG.timezone_offset_hours))

    def clock(day, hour):
        return datetime.fromisoformat(f"{day}T{hour}:00").replace(tzinfo=local).timestamp()

    _control(store, "start", "time-start", spec={"objective": "检查两天的午餐", "starts": first, "ends": last,
              "triggers": ["time"], "check_time": "09:00"})
    unchanged = call("submit_goal_result", {"outcome": "unchanged", "reason": "本次检查暂无调整"})
    _tick(store, progress)
    _tick(store, progress, NativeFake(responses=[unchanged]))
    assert _tick(store, progress, now=clock(first, "08:59"))[0]["status"] == "idle"
    result, claim = _tick(store, progress, now=clock(first, "09:00"))
    assert result["status"] == "running"
    assert background.writer(store, claim, CONFIG, send=None, now=clock(first, "09:00")) == result
    _, ready = _tick(store, progress, NativeFake(responses=[_plan([first]), _submit]), publish=False, now=clock(first, "09:01"))
    assert ready["candidate"]["status"] == "ready"
    store.save_document("user_settings", "user", "", {"targets": {"kcal": 1800}})
    assert background.writer(store, ready, CONFIG, send=None, now=clock(first, "09:01"))["status"] == "obsolete"
    assert not diet_goals.load(store, "user").get("latest")
    assert _tick(store, progress, now=clock(first, "09:02"))[0]["status"] == "idle"
    next_run, _ = _tick(store, progress, now=clock(last, "09:00"))
    assert next_run["status"] == "running" and next_run["run_id"] != result["run_id"]
    assert _tick(store, progress, now=clock(_day(3), "09:00"))[0]["status"] == "ended"
    assert not diet_goals.load(store, "user")["auto"]
    _control(store, "start", "second-goal", spec={"objective": "重新安排午餐", "starts": first, "ends": last})
    new_goal = diet_goals.load(store, "user")
    _control(store, "stop_run", "stop-before-claim", goal=new_goal)
    assert _tick(store, progress)[0]["status"] == "idle" and diet_goals.load(store, "user")["auto"]


def E09():
    """真实模型跟进一周午餐、局部改计划并保护锁定餐次，暂停拦住迟到发布。"""
    from agent.model import DeepSeekModel
    from harness.eval import conversation_cases

    config = replace(CONFIG, model="deepseek-v4-flash")
    store = _fixture()
    store.foods[0].update(type="熟", aliases=["熟米饭", "白米饭", "白米饭（熟）"])
    store.foods[1].update(type="熟", aliases=["熟鸡胸肉", "鸡胸肉（熟）"])
    store.foods.append({**_food("鸡蛋", 139), "type": "熟", "aliases": ["熟鸡蛋", "水煮鸡蛋", "煮鸡蛋"],
                        "units": [{"unit": "个", "eat_weight": 50}]})
    progress = Journal(Objects(), "goal-real/sync/")
    days = [_day(n) for n in range(1, 8)]
    settings = {"targets": {"kcal": 1900}}
    store.save_document("user_settings", "user", "", settings)

    def send(text, mid, chat="first", delivered=True):
        result = run({"message_id": mid, "user_id": "user", "chat_id": chat, "delivery_source": "feishu", "text": text},
                     store=store, config=config, model=DeepSeekModel(model_name=config.model))
        conversation_cases.TRANSCRIPT.append({"id": mid, "user": text, "result": result})
        assert result["status"] == "ok", result["errors"]
        if delivered:
            mark_reply_presented(store, "user", chat, result)
        return result

    def tick(label, *, publish=True):
        result, event = _tick(store, progress, publish=publish, config=config)
        if event and "candidate" in event:
            candidate = event["candidate"]
            plans = {day: {"total": draft["calculation"]["total"], "targets": draft["calculation"]["targets"],
                           "items": draft["calculation"]["items"]} for day, draft in candidate.get("plans", {}).items()}
            event = {**event, "candidate": {**candidate, "plans": plans}}
        conversation_cases.TRANSCRIPT.append({"id": label, "result": result, "event": event})
        return result

    send(f"请从 {days[0]} 到 {days[-1]} 持续帮我跟进每天的午餐，先把七天的午餐都排好。只用家里已有的米饭、鸡胸肉和鸡蛋，这三样都已经煮熟，"
         "尽量一起备餐，按我现在的营养目标考虑，午餐大约四百千卡。我反馈食材或设备变化时再调整，平时不用主动通知。", "goal-start")
    goal = diet_goals.load(store, "user")
    assert goal.get("status") == "active" and not goal["notify"], goal
    assert tick("goal-claim")["status"] == "running"
    assert tick("goal-initial-plan")["status"] == "applied"
    goal = diet_goals.load(store, "user")
    initial = diet_goals.baseline(store, "user", goal)
    assert sorted(initial) == days and not goal.get("delivered")
    assert all(plan["calculation"]["targets"]["kcal"] == 1900 and plan["calculation"]["complete"] for plan in initial.values())

    status = send("我那周的午餐现在安排到哪了？先只告诉我进度和以后什么时候再调整。", "goal-status", "another")
    assert "goal_delivery_ref" not in status and not diet_goals.load(store, "user").get("delivered")
    show = send("把现在最新的一周午餐安排给我看看。", "goal-show", "another", delivered=False)
    assert all(day in show["reply"] for day in days) and not diet_goals.load(store, "user").get("delivered")
    mark_reply_presented(store, "user", "another", show)
    assert diet_goals.load(store, "user")["delivered"] == 1

    send(f"{days[0]} 的午餐我已经备好，锁定别改。只改 {days[1]} 的午餐，那天米饭吃完了，只有鸡胸肉和鸡蛋，"
         "能用的设备只有微波炉。其他日期保留原样，仍然不用主动通知。", "goal-change", "another")
    revised = diet_goals.load(store, "user")
    assert revised["revision"] > goal["revision"] and {"date": days[0], "meal": "lunch"} in revised["spec"]["locks"]
    assert tick("goal-change-claim")["status"] == "running"
    send("以后别自动检查或调整了，手头正在做的这次请继续，我以后要改会自己来问。", "goal-auto-off", "another")
    assert not diet_goals.load(store, "user")["auto"] and diet_goals.load(store, "user")["run"]["status"] == "running"
    assert tick("goal-updated-plan")["status"] == "applied"
    revised = diet_goals.load(store, "user")
    updated = diet_goals.baseline(store, "user", revised)
    assert revised["latest"] == 2 and revised["delivered"] == 1
    assert all(updated[day] == initial[day] for day in days if day != days[1])
    assert {item["matched_name"] for item in updated[days[1]]["calculation"]["items"]} <= {"鸡胸肉", "鸡蛋"}

    manual = send("现在手动帮我检查一遍这周的安排还合不合适。", "goal-manual-check", "another")
    assert "goal_delivery_ref" not in manual and "已排队" in manual["reply"]
    assert tick("goal-manual-claim")["status"] == "running"
    events = []
    background.run_next(store, progress, config, dispatch=events.append)
    assert events and events[0]["operation"] == "publish" and events[0]["candidate"]["status"] == "ready"
    send("整个午餐委托先暂停，正在检查的结果先不要采用。", "goal-pause", "another")
    stale = background.writer(store, events[0], config, send=None)
    conversation_cases.TRANSCRIPT.append({"id": "goal-late-candidate", "result": stale})
    assert stale["status"] == "obsolete" and diet_goals.load(store, "user")["latest"] == 2
    assert tick("goal-paused")["status"] == "idle"
    assert not store.intake and store.load_document("user_settings", "user") == settings


E09.real_only = True
CHECKS = {"DG01": DG01, "DG02": DG02}
