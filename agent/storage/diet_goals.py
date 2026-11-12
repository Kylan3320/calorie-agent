"""Versioned diet delegations, trigger binding and publication by the message writer."""
from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import time

from . import memory
from .bitable_adapter import stable_uuid


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _local(config, epoch):
    return datetime.fromtimestamp(epoch, timezone(timedelta(hours=config.timezone_offset_hours)))


def load(store, user, ident=""):
    ident = ident or store.load_document("diet_goal_owner", user).get("id", "")
    return store.load_document("diet_goal", user, ident) if ident else {}


def save(store, user, goal):
    store.save_document("diet_goal", user, goal["id"], goal)
    index = store.load_document("diet_goal_active", "system")
    before = index.get(user)
    if goal["status"] == "ended" and goal.get("run", {}).get("status") != "stop_requested":
        if before == goal["id"]:
            index.pop(user)
    else:
        index[user] = goal["id"]
    if before != index.get(user):
        store.save_document("diet_goal_active", "system", "", index)


def _stop(goal):
    run = goal.get("run", {})
    if run.get("status") in {"queued", "running"}:
        run["status"] = "terminated" if run["status"] == "queued" else "stop_requested"


def _queue(goal, key, kind, detail, now):
    goal.setdefault("pending", {})[key] = {"kind": kind, "detail": detail, "time": now}


def _status(goal):
    run = goal.get("run", {})
    spec = goal["spec"]
    result = {key: goal[key] for key in ("id", "revision", "status", "auto", "notify")}
    result.update(objective=spec["objective"], starts=spec.get("starts"), ends=spec.get("ends"),
                  constraints=spec.get("constraints", []), dated_constraints=spec.get("dated_constraints", {}),
                  locks=spec.get("locks", []), done_when=spec.get("done_when"),
                  next_step=goal.get("next_step", ""), waiting_for=goal.get("waiting_for", ""),
                  last_change=goal.get("last_change", ""), last_check=goal.get("last_check"),
                  run={key: run[key] for key in ("id", "status", "day", "diagnostic") if key in run},
                  latest=goal.get("latest"), delivered=goal.get("delivered"),
                  delivery_status=goal.get("delivery", {}).get("status"),
                  pending=bool(goal.get("pending")))
    if goal["status"] == "active" and goal["auto"]:
        result["triggers"] = spec.get("triggers", [])
        result["check_time"] = spec.get("check_time")
    return result


def _artifact(store, user, goal, version):
    return store.load_document("diet_goal_plan", user, f"{goal['id']}:{version}") if version else {}


def _show_artifact(store, user, goal, latest):
    version = goal.get("latest") if latest else goal.get("delivered")
    artifact = _artifact(store, user, goal, version)
    if not artifact:
        return {"status": "pending", "data": {"goal": _status(goal), "reason": "no_artifact"}}
    if latest and goal["status"] != "ended" and artifact["revision"] != goal["revision"]:
        return {"status": "pending", "data": {"goal": _status(goal), "reason": "revised_plan_not_ready"}}
    return {"status": "ok", "data": {"goal": _status(goal), "artifact": {
        "id": goal["id"], "version": version, "dates": sorted(artifact["plans"]),
        "reply": render(artifact), "latest": latest}}}


def query(rt, params):
    goal = load(rt.store, rt.user_id, params.get("goal_id", ""))
    if not goal:
        return {"status": "pending", "data": {"reason": "goal_not_found"}}
    if goal["status"] in {"active", "paused"} and rt.day > goal["spec"]["ends"]:
        _end(goal)
    if params.get("operation") in {"show_latest", "show_delivered"}:
        quote = params.get("view_quote", "")
        if not quote or quote not in rt.message["text"]:
            return {"status": "pending", "data": {"goal": _status(goal), "reason": "artifact_view_not_authorized"}}
        return _show_artifact(rt.store, rt.user_id, goal, params["operation"] == "show_latest")
    return {"status": "ok", "data": {"goal": _status(goal)}}


def control_receipt(data):
    goal, operation = data["goal"], data["operation"]
    run = goal["run"].get("status")
    stopping = "已请求停止当前运行，尚在退出中的调用结果不会采用。" if run == "stop_requested" else ""
    if operation in {"start", "revise"}:
        verb = "已建立" if operation == "start" else "已修改"
        scope = f"{goal['starts']}至{goal['ends']}，{goal['objective']}。"
        notices = "主动通知已开启。" if goal["notify"] else "主动通知关闭。"
        return verb + "饮食委托。" + scope + ("已排队核算，可稍后查看进度。" if goal["pending"] else "") + notices + stopping
    if operation == "request_run":
        return "已排队手动检查。核算完成后可查看结果。"
    if operation == "stop_run":
        return stopping or "当前没有继续运行的任务。后续自动触发按原设置执行。"
    if operation == "set_auto":
        return ("已开启" if goal["auto"] else "已关闭") + "后续自动触发。" + ("当前运行继续。" if run == "running" else "")
    if operation == "pause":
        return "委托已暂停，保留已有计划。" + stopping
    if operation == "resume":
        return "委托已恢复。" + ("已排队检查当前适用的变化。" if goal["pending"] else "")
    if operation == "set_notifications":
        return ("已开启" if goal["notify"] else "已关闭") + "主动通知。"
    return "委托已结束。" + stopping


def _missing(spec, notices):
    return [key for key in ("starts", "ends", "done_when") if not spec.get(key)] + [
        key for key, missing in (("check_time", "time" in spec.get("triggers", []) and not spec.get("check_time")),
                                ("destination", notices and not spec.get("destination"))) if missing]


def _check_scope(spec):
    first, last = spec.get("starts"), spec.get("ends")
    if first and last and first > last:
        raise ValueError("委托结束日期早于开始日期")
    if first and last and any(not first <= lock["date"] <= last for lock in spec.get("locks", [])):
        raise ValueError("锁定餐次须在委托日期内")


def manage(rt, params):
    store, user = rt.store, rt.user_id
    if not params.get("user_quote") or params["user_quote"] not in rt.message["text"]:
        return {"status": "pending", "data": {"reason": "current_authorization_required"}}
    operation = params["operation"]
    goal = load(store, user, params.get("goal_id", ""))
    now = time.time()
    if operation == "start":
        current = load(store, user)
        if current.get("status") in {"active", "paused"} and rt.day > current["spec"]["ends"]:
            _end(current)
            save(store, user, current)
        if current and current["status"] != "ended":
            return {"status": "pending", "data": {"reason": "active_goal_exists", "goal": _status(current)}}
        ident = "diet-goal:" + stable_uuid(f"{user}:{rt.action_id}")
        goal = {"id": ident, "revision": 1, "input_revision": 0, "auto": True, "notify": False,
                "status": "preparing", "spec": {}, "pending": {}, "run": {}, "activated": now}
        store.save_document("diet_goal_owner", user, "", {"id": ident})
    elif not goal or goal["status"] == "ended":
        return {"status": "pending", "data": {"reason": "no_open_goal"}}
    elif params.get("revision") != goal["revision"]:
        return {"status": "pending", "data": {"reason": "goal_changed", "goal": _status(goal)}}
    elif goal["status"] in {"active", "paused"} and rt.day > goal["spec"]["ends"]:
        _end(goal)
        save(store, user, goal)
        return {"status": "pending", "data": {"reason": "goal_expired", "goal": _status(goal)}}

    if operation in {"start", "revise"}:
        scope = dict(params.get("spec", {}))
        changed_dates = params.get("change_dates", [])
        if changed_dates and "constraints" in scope:
            conditions = scope.pop("constraints")
            scope["dated_constraints"] = {**goal["spec"].get("dated_constraints", {}), **{day: conditions for day in changed_dates}}
        spec = {**goal["spec"], **scope}
        spec.setdefault("done_when", spec["objective"])
        if "locks" in scope or params.get("unlock"):
            unlocked = {(item["date"], item["meal"]) for item in params.get("unlock", [])}
            locks = {(item["date"], item["meal"]) for item in [*goal["spec"].get("locks", []), *scope.get("locks", [])]} - unlocked
            spec["locks"] = [{"date": day, "meal": meal} for day, meal in sorted(locks)]
        if spec.pop("notify_here", False) and rt.message.get("delivery_source") == "feishu":
            spec["destination"] = rt.message["chat_id"]
        notify = params.get("enabled", goal["notify"])
        _check_scope(spec)
        if changed_dates and not all(spec.get("starts", day) <= day <= spec.get("ends", day) for day in changed_dates):
            return {"status": "pending", "data": {"reason": "change_dates_outside_scope", "goal": _status(goal)}}
        missing = _missing(spec, notify)
        if missing and goal["status"] != "preparing":
            return {"status": "pending", "data": {"reason": "scope_incomplete", "missing": missing, "goal": _status(goal)}}
        if operation == "revise":
            goal["revision"] += 1
            _stop(goal)
        goal.update(spec=spec, notify=notify, last_change=params["user_quote"])
        if goal["status"] == "preparing" and not missing:
            goal.update(status="active", activated=now)
        if goal["status"] == "active":
            _queue(goal, rt.action_id, "scope", {"request": params["user_quote"], "dates": changed_dates}, now)
        if missing:
            save(store, user, goal)
            return {"status": "pending", "data": {"reason": "scope_incomplete", "missing": missing, "goal": _status(goal)}}
    elif operation == "request_run":
        if goal["status"] != "active":
            return {"status": "pending", "data": {"reason": "goal_not_active", "goal": _status(goal)}}
        _queue(goal, rt.action_id, "manual", params["user_quote"], now)
    elif operation == "stop_run":
        if goal["run"].get("status") not in {"queued", "running", "stop_requested"}:
            goal["pending"] = {}
        _stop(goal)
        if goal.get("delivery", {}).get("status") == "pending" and goal["delivery"]["run_id"] == goal.get("run", {}).get("id"):
            goal["delivery"]["status"] = "superseded"
    elif operation == "set_auto":
        goal["auto"] = params["enabled"]
        if not goal["auto"]:
            goal["pending"] = {key: event for key, event in goal["pending"].items() if event["kind"] in {"manual", "scope"}}
        else:
            goal["activated"] = now
    elif operation == "pause":
        _stop(goal)
        goal["status"] = "paused"
        if goal.get("delivery", {}).get("status") == "pending":
            goal["delivery"]["status"] = "superseded"
    elif operation == "resume":
        missing = _missing(goal["spec"], goal["notify"])
        if missing:
            return {"status": "pending", "data": {"reason": "scope_incomplete", "missing": missing, "goal": _status(goal)}}
        if goal["status"] == "paused":
            goal.update(status="active", activated=now)
            _queue(goal, rt.action_id, "resume", params["user_quote"], now)
    elif operation == "set_notifications":
        if params.get("notify_here") and rt.message.get("delivery_source") == "feishu":
            goal["spec"]["destination"] = rt.message["chat_id"]
        if params["enabled"] and not goal["spec"].get("destination"):
            return {"status": "pending", "data": {"reason": "destination_required", "goal": _status(goal)}}
        goal["notify"] = params["enabled"]
        if not goal["notify"] and goal.get("delivery", {}).get("status") == "pending":
            goal["delivery"]["status"] = "superseded"
    elif operation == "end":
        _end(goal)
    save(store, user, goal)
    return {"status": "ok", "data": {"goal": _status(goal), "operation": operation}}


def _end(goal):
    _stop(goal)
    goal.update(status="ended", auto=False, pending={})
    if goal.get("delivery", {}).get("status") == "pending":
        goal["delivery"]["status"] = "superseded"


def context(store, user):
    goal = load(store, user)
    return _status(goal) if goal else {}


def register_event(store, user, kind, source, detail, now, days=()):
    goal = load(store, user)
    if (not goal or goal["status"] not in {"active", "paused"} or not goal["auto"]
            or kind not in goal["spec"].get("triggers", [])):
        return
    if days and not any(goal["spec"]["starts"] <= day <= goal["spec"]["ends"] for day in days):
        return
    key = f"{goal['id']}:{kind}:{source}"
    if store.load_document("diet_goal_event", user, key):
        return
    store.save_document("diet_goal_event", user, key, {"time": now})
    goal["input_revision"] += 1
    _queue(goal, kind, kind, detail, now)
    save(store, user, goal)


def _time_slot(goal, config, now):
    local = _local(config, now)
    spec = goal["spec"]
    if (not goal["auto"] or "time" not in spec.get("triggers", []) or not spec.get("check_time")
            or local.date().isoformat() < spec["starts"]):
        return ""
    slot = datetime.fromisoformat(f"{local.date()}T{spec['check_time']}").replace(tzinfo=local.tzinfo)
    if slot.timestamp() < goal["activated"] or slot.timestamp() > now or goal.get("last_slot") == slot.isoformat():
        return ""
    return slot.isoformat()


def fact_stamp(store, user, goal):
    spec = goal["spec"]
    end = (date.fromisoformat(spec["ends"]) + timedelta(days=1)).isoformat()
    intake = sorted((row["record_id"], row["date"], row["meal_type"], row["grams"], row["nutrition"])
                    for row in store.intake_items(user, spec["starts"], end))
    settings = {key: value for key, value in store.load_document("user_settings", user).items()
                if key in {"targets", "daily_targets", "preferences", "portions"}}
    view = memory.state(store, user)
    relevant = {key: ref for key, ref in view.get("current", {}).items() if key.startswith("condition:")}
    standards = [row for row in store._get("source:standards", []) if row.get("user_id", "") in {"", user}
                 and (not row.get("date") or spec["starts"] <= row["date"] <= spec["ends"])] if hasattr(store, "_get") else []
    return _hash([intake, settings, standards, relevant, view.get("redaction_revision"), view.get("experience_revision")])


def due(goal, config, now):
    if goal["status"] not in {"active", "paused"}:
        return False
    day = _local(config, now).date().isoformat()
    if day > goal["spec"]["ends"]:
        return True
    return (goal["status"] == "active" and goal["run"].get("status") not in {"running", "queued", "stop_requested"}
            and bool(goal.get("pending") or _time_slot(goal, config, now)))


def claim(store, user, ident, config, now):
    goal = load(store, user, ident)
    if not goal or not due(goal, config, now):
        return {"status": "skipped"}
    day = _local(config, now).date().isoformat()
    if day > goal["spec"]["ends"]:
        _end(goal)
        save(store, user, goal)
        return {"status": "ended"}
    slot = _time_slot(goal, config, now)
    if slot:
        goal["last_slot"] = slot
        _queue(goal, slot, "time", slot, now)
    events = goal["pending"]
    goal["pending"] = {}
    identity = stable_uuid(f"{goal['id']}:{goal['revision']}:{_hash(events)}")
    if prior := goal.get("run"):
        store.save_document("diet_goal_run", user, prior["id"], prior)
    goal["run"] = {"id": identity, "status": "queued", "day": day, "events": events, "queued_at": now,
                   "revision": goal["revision"], "input_revision": goal["input_revision"]}
    save(store, user, goal)
    return {"status": "queued", "run_id": identity}


def begin(store, user, ident, run_id, config, now):
    goal = load(store, user, ident)
    run = goal.get("run", {})
    if (goal.get("status") != "active" or run.get("id") != run_id or run.get("status") != "queued"
            or _local(config, now).date().isoformat() > goal["spec"]["ends"]):
        return {"status": "skipped"}
    run["events"].update(goal["pending"])
    goal["pending"] = {}
    run.update(status="running", input_revision=goal["input_revision"], stamp=fact_stamp(store, user, goal), started_at=now)
    save(store, user, goal)
    return {"status": "running", "run_id": run_id}


def valid(store, user, goal, run, config, now, *, check_facts=True):
    return bool(goal and goal["status"] == "active" and goal["run"].get("id") == run["id"]
                and goal["run"]["status"] == "running" and goal["revision"] == run["revision"]
                and goal["input_revision"] == run["input_revision"]
                and _local(config, now).date().isoformat() <= goal["spec"]["ends"]
                and (not check_facts or fact_stamp(store, user, goal) == run["stamp"]))


def baseline(store, user, goal):
    return _artifact(store, user, goal, goal.get("latest")).get("plans", {})


def render(artifact):
    from ..planning import candidate_presentations
    lines = [candidate_presentations({"meal_draft_candidate": plan}, plan["message_id"])["text"]
             for _, plan in sorted(artifact["plans"].items())]
    if artifact.get("meals") and not {"breakfast", "lunch", "dinner"}.issubset(artifact["meals"]):
        lines = [line.replace("当日完整菜单（", "当日委托安排（", 1) for line in lines]
    if artifact.get("missing_dates"):
        lines.append("尚未完成 " + "、".join(artifact["missing_dates"]))
    if artifact.get("waiting_for"):
        lines.append("待你决定 " + artifact["waiting_for"])
    if artifact.get("next_step"):
        lines.append("下一步 " + artifact["next_step"])
    if artifact.get("reason"):
        lines.append("调整依据 " + artifact["reason"])
    if artifact.get("preparation"):
        lines.append("备餐方法 " + artifact["preparation"])
    return "\n\n".join(lines)


def _plan_digest(plans):
    return _hash({day: {"scope": plan["scope"], "targets": plan["calculation"]["targets"],
                        "items": [[item["group"], item["matched_name"], item["per_serving"]["grams"],
                                   item["servings"], item["nutrition"]] for item in plan["calculation"]["items"]]}
                  for day, plan in plans.items()})


def _missing_dates(goal, plans, today):
    spec = goal["spec"]
    if not spec.get("meals") and not plans:
        return []
    meals = {MEALS[meal] for meal in spec.get("meals", [])}
    day = date.fromisoformat(max(spec["starts"], today))
    end = date.fromisoformat(spec["ends"])
    missing = []
    while day <= end:
        value = day.isoformat()
        included = {item["group"] for item in plans.get(value, {}).get("calculation", {}).get("items", [])}
        if not included or not meals.issubset(included):
            missing.append(value)
        day += timedelta(days=1)
    return missing


MEALS = {"breakfast": "早餐", "lunch": "午餐", "dinner": "晚餐", "snack": "加餐"}


def publish(store, user, ident, run_id, candidate, config, now):
    def apply():
        goal = load(store, user, ident)
        run = goal.get("run", {})
        if run.get("id") != run_id or run.get("status") not in {"running", "stop_requested"}:
            return {"status": "obsolete"}
        binding = {key: run[key] for key in ("revision", "input_revision", "stamp")}
        if (not valid(store, user, goal, run, config, now) or candidate.get("status") == "obsolete"
                or candidate.get("binding") != binding):
            run.update(status="terminated", diagnostic="scope_sources_or_control_changed", finished_at=now)
            save(store, user, goal)
            return {"status": "obsolete"}
        if candidate.get("status") == "failed":
            run.update(status="failed", diagnostic=candidate["diagnostic"], finished_at=now, usage=candidate.get("usage", {}))
            goal["last_check"] = now
            save(store, user, goal)
            return {"status": "failed"}
        old = _artifact(store, user, goal, goal.get("latest"))
        first, last = (goal["spec"][key] for key in ("starts", "ends"))
        plans = {day: draft for day, draft in old.get("plans", {}).items() if first <= day <= last}
        plans.update(candidate["plans"])
        missing = _missing_dates(goal, plans, run["day"])
        waiting = candidate.get("waiting_for", "")
        next_step = candidate.get("next_step") or ("核对尚未完成的日期和餐次" if missing else "")
        preparation = candidate.get("preparation") or old.get("preparation", "")
        signature = _hash([_plan_digest(plans), preparation])
        changed = bool(plans and (signature != old.get("signature") or goal["revision"] != old.get("revision")))
        new_question = bool(waiting and waiting != goal.get("waiting_for"))
        artifact = old
        if changed:
            version = goal.get("latest", 0) + 1
            artifact = {"id": ident, "version": version, "revision": goal["revision"], "run_id": run_id,
                        "signature": signature, "plans": plans, "missing_dates": missing,
                        "meals": goal["spec"].get("meals", []), "reason": candidate["reason"],
                        "next_step": next_step, "waiting_for": waiting, "preparation": preparation}
            store.save_document("diet_goal_plan", user, f"{ident}:{version}", artifact)
            goal["latest"] = version
            if goal.get("delivery", {}).get("status") == "pending":
                goal["delivery"]["status"] = "superseded"
            goal["last_change"] = candidate["reason"]
        scheduled = goal["spec"].get("scheduled_delivery") and any(event["kind"] == "time" for event in run["events"].values())
        if candidate.get("notify") and goal["notify"] and (changed or new_question or scheduled):
            version = artifact.get("version") if changed or scheduled else None
            text = render(artifact) if version else ("饮食委托待你决定 " + waiting if waiting else "")
            if text:
                goal["delivery"] = {"id": stable_uuid(f"goal-notice:{ident}:{run_id}"), "status": "pending",
                                    "version": version, "revision": goal["revision"], "run_id": run_id,
                                    "destination": goal["spec"]["destination"], "text": text,
                                    "stamp": run["stamp"], "input_revision": run["input_revision"]}
        goal.update(waiting_for=waiting, next_step=next_step, last_check=now, missing_dates=missing)
        run.update(status="waiting" if waiting else "complete", finished_at=now, usage=candidate.get("usage", {}))
        if candidate.get("diagnostic"):
            run["diagnostic"] = candidate["diagnostic"]
        save(store, user, goal)
        return {"status": "applied", "latest": goal.get("latest"), "changed": changed}

    return store.commit_action(f"diet-goal-publish:{run_id}", apply)


def mark_delivered(store, user, ident, version):
    goal = load(store, user, ident)
    artifact = _artifact(store, user, goal, version) if goal else {}
    if (goal.get("status") in {"active", "paused"} and goal.get("latest") == version and artifact
            and artifact["revision"] == goal["revision"] and goal.get("delivered") != version):
        goal["delivered"] = version
        if goal.get("delivery", {}).get("status") == "pending" and goal["delivery"]["version"] == version:
            goal["delivery"]["status"] = "superseded"
        save(store, user, goal)


def deliver(store, user, ident, delivery_id, config, now, send):
    goal = load(store, user, ident)
    delivery = goal.get("delivery", {})
    if delivery.get("id") != delivery_id or delivery.get("status") != "pending":
        return {"status": "obsolete"}
    current = (goal["status"] == "active" and goal["notify"] and goal["revision"] == delivery["revision"]
               and goal["spec"]["destination"] == delivery["destination"]
               and _local(config, now).date().isoformat() <= goal["spec"]["ends"]
               and goal["input_revision"] == delivery["input_revision"] and fact_stamp(store, user, goal) == delivery["stamp"]
               and (delivery["version"] is None or goal.get("latest") == delivery["version"]))
    if not current:
        delivery["status"] = "superseded"
        save(store, user, goal)
        return {"status": "obsolete"}
    response = send(delivery["destination"], delivery["text"], uuid_key=delivery_id)
    message_id = response.get("data", {}).get("message_id")
    if not message_id:
        raise RuntimeError("goal notification delivery not confirmed")
    delivery.update(status="delivered", message_id=message_id, delivered_at=time.time())
    if delivery["version"] is not None:
        goal["delivered"] = delivery["version"]
    save(store, user, goal)
    return {"status": "delivered", "message_id": message_id}
