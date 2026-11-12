"""User-owned sources and versioned memory. Mutations use the message writer."""
from copy import deepcopy
from datetime import date, timedelta
import hashlib
import json
import re
import time


REF = re.compile(r"(turn|draft|condition|setting|feedback|note|theme):([0-9a-f]{24}):([1-9][0-9]*)\Z")


def identity(user, kind, key):
    value = json.dumps([user, kind, key], ensure_ascii=False, separators=(",", ":"))
    return f"{kind}:" + hashlib.sha256(value.encode()).hexdigest()[:24]


def reference(user, kind, key, version=1):
    return f"{identity(user, kind, key)}:{version}"


def ref_id(ref):
    return ref.rsplit(":", 1)[0]


def is_withdrawn(view, ref):
    matched = REF.fullmatch(ref or "")
    return bool(matched and int(matched[3]) <= view.get("revoked", {}).get(ref_id(ref), 0))


def state(store, user):
    return store.load_document("memory_state", user)


def redaction_revision(store, user):
    return state(store, user).get("redaction_revision", 0)


def turn_redactions(value, ident):
    return value.get("redactions", {}).get(ident, [])


def redact_text(value, text):
    for quote in value.get("redacted_quotes", []):
        text = text.replace(quote, "[已撤回]")
    return text


def turn_text(value, ident, text, *, assistant=False):
    quotes = turn_redactions(value, ident)
    if quotes and (assistant or "" in quotes):
        return ""
    return redact_text(value, text)


def applicable(item, day, draft_id=None):
    if item.get("status") not in {"active", "usable"}:
        return False
    if item.get("starts") and day < item["starts"] or item.get("ends") and day > item["ends"]:
        return False
    return not item.get("draft_id") or item["draft_id"] == draft_id


def legacy_settings(store, user, view):
    settings = store.load_document("user_settings", user)
    locations = [(section, "", fields) for section in ("preferences", "targets", "portions", "favorites", "reminders")
                 if (fields := settings.get(section))]
    locations.extend(("daily_targets", day, fields) for day, fields in settings.get("daily_targets", {}).items())
    for section, day, fields in locations:
        for key, value in fields.items():
            ref = reference(user, "setting", [section, day, key])
            if ref_id(ref) in view.get("current", {}) or is_withdrawn(view, ref):
                continue
            yield {"kind": "setting", "ref": ref, "version": 1, "status": "active", "section": section,
                   "key": key, "setting_date": day, "text": f"{key} {value}", "value": value, "legacy": True}


def get(store, user, ref, *, view=None):
    """Resolve only in the bound user's namespace, including historical versions."""
    if not REF.fullmatch(ref or ""):
        return None, "not_found"
    view = state(store, user) if view is None else view
    key = ref_id(ref)
    if is_withdrawn(view, ref):
        return None, "withdrawn"
    item = store.load_document("memory_object", user, ref)
    if not item and ref.startswith("setting:"):
        item = next((setting for setting in legacy_settings(store, user, view) if setting["ref"] == ref), None)
    if not item:
        return None, "not_found"
    if item["kind"] == "turn" and turn_redactions(view, item["turn_id"]):
        return None, "withdrawn"
    if item["kind"] == "feedback" and is_withdrawn(view, item["target_ref"]):
        return None, "withdrawn"
    latest = view.get("current", {}).get(key, ref)
    if latest != ref:
        item["status"] = "superseded"
    return item, "ok"


def dependencies_valid(store, user, refs, day, *, view, draft_id=None, ancestry=()):
    for ref in refs:
        if ref in ancestry:
            return False
        item, status = get(store, user, ref, view=view)
        if status != "ok" or item["status"] not in {"active", "delivered", "usable"}:
            return False
        if any(redact_text(view, quote["text"]) != quote["text"] for quote in item.get("quotes", [])):
            return False
        if item["kind"] == "condition" and not applicable(item, day, draft_id):
            return False
        if item["kind"] == "setting":
            settings = store.load_document("user_settings", user)
            fields = settings.get(item["section"], {})
            if item.get("setting_date"):
                fields = fields.get(item["setting_date"], {})
            if fields.get(item["key"]) != item["value"] or item["key"] not in fields:
                return False
        if item.get("dependencies") and not dependencies_valid(store, user, item["dependencies"], day,
                view=view, draft_id=draft_id, ancestry=(*ancestry, ref)):
            return False
    return True


def save(store, user, kind, key, payload, *, view=None, catalog=None, new_version=False):
    """Write a source/version and its catalog entry inside the caller's commit."""
    view = state(store, user) if view is None else view
    catalog = store.load_document("memory_index", user) if catalog is None else catalog
    base = identity(user, kind, key)
    previous = view.get("current", {}).get(base) or catalog.get("items", {}).get(base, {}).get("ref")
    if previous and not new_version:
        old = store.load_document("memory_object", user, previous)
        if all(old.get(k) == v for k, v in payload.items()):
            return old
    version = int(previous.rsplit(":", 1)[1]) + 1 if previous else 1
    ref = f"{base}:{version}"
    item = {**deepcopy(payload), "kind": kind, "ref": ref, "version": version}
    if previous:
        item["previous"] = previous
    store.save_document("memory_object", user, ref, item)
    catalog.setdefault("items", {})[base] = {k: item[k] for k in
        ("kind", "ref", "date", "chat_id", "text", "status", "turn_id") if k in item}
    store.save_document("memory_index", user, "", catalog)
    if kind in {"condition", "setting", "note", "theme"}:
        view.setdefault("current", {})[base] = ref
        store.save_document("memory_state", user, "", view)
    return item


def delivered_turn(store, user, chat, ident, source, *, register=True):
    from langchain_core.messages import AIMessage, HumanMessage, messages_from_dict

    view = state(store, user)
    if turn_redactions(view, ident):
        return
    dialogue = []
    for message in messages_from_dict(source["messages"]):
        if isinstance(message, HumanMessage):
            role = "user"
        elif not source.get("assistant_reply") and isinstance(message, AIMessage) and message.additional_kwargs.get("fact_scope") == "dialogue":
            role = "assistant"
        else:
            continue
        content = turn_text(view, ident, str(message.content), assistant=role == "assistant")
        if content:
            dialogue.append({"role": role, "text": content})
    if source.get("assistant_reply"):
        content = turn_text(view, ident, source["assistant_reply"], assistant=True)
        if content:
            dialogue.append({"role": "assistant", "text": content})
    item = save(store, user, "turn", ident, {"turn_id": ident, "chat_id": chat,
        "date": source.get("date", ""), "received_at": source.get("received_at"),
        "status": "delivered", "text": "\n".join(part["text"] for part in dialogue),
        "dialogue": dialogue}, view=view)
    if register and source.get("review_sources"):
        marks = []
        for mark in source["review_sources"]:
            ref = mark["ref"]
            target, status = get(store, user, ref, view=view)
            if status == "ok" and project(store, user, target, source.get("date") or store.today_string(), view):
                marks.append(mark)
        if marks:
            store.save_document("memory_review", user, item["ref"], {"sources": marks,
                "draft_ref": reference(user, "draft", source["draft"]["draft_id"]) if source.get("draft") else ""})
            enqueue(store, user, item["ref"])
    if register and ident in view.get("feedback", {}):
        view["feedback"].pop(ident)
        store.save_document("memory_state", user, "", view)


def delivered_draft(store, user, chat, turn_id, draft, *, register=True):
    item = save(store, user, "draft", draft["draft_id"], {"turn_id": turn_id, "chat_id": chat,
        "date": draft.get("received_date", draft["date"]), "status": "delivered", "draft": draft,
        "text": " ".join(["菜单", draft["scope"], draft["date"], *draft.get("constraints", []),
                           *(part.get("food_query", "") for part in draft["items"])])})
    return item


def enqueue(store, user, ref):
    work = store.load_document("memory_work", user)
    pending = work.setdefault("pending", {})
    if ref in pending or ref in work.get("processed", {}):
        return
    pending[ref] = time.time()
    store.save_document("memory_work", user, "", work)
    queue = store.load_document("memory_active", "system")
    if user not in queue:
        queue[user] = min(pending.values())
        store.save_document("memory_active", "system", "", queue)


def source(rt, quote=""):
    if quote and quote not in rt.message["text"]:
        raise ValueError("memory source quote must occur verbatim in the current user message")
    return {"turn_id": rt.human_id, "source_ref": reference(rt.user_id, "turn", rt.human_id),
            "quote": quote, "chat_id": rt.message["chat_id"], "date": rt.day}


def condition_dates(values, day):
    anchor = date.fromisoformat(day)
    period = values.get("period")
    if period == "today":
        return day, day
    if period == "this_week":
        start = anchor - timedelta(days=anchor.weekday())
        return start.isoformat(), (start + timedelta(days=6)).isoformat()
    start = values.get("starts") or day
    end = values.get("ends", "")
    date.fromisoformat(start)
    if end and date.fromisoformat(end) < date.fromisoformat(start):
        raise ValueError("condition ends before its start")
    return start, end


def withdraw(store, user, item, view):
    key = ref_id(item["ref"])
    view.setdefault("revoked", {})[key] = item["version"]
    if item.get("legacy"):
        view.setdefault("current", {})[key] = item["ref"]
    related = [item]
    seen = set()
    while related:
        record = related.pop()
        if not record or record["ref"] in seen:
            continue
        seen.add(record["ref"])
        links = [record.get("previous")]
        if record["kind"] == "note":
            links += [*record.get("supports", []), *record.get("refutes", [])]
        related.extend(store.load_document("memory_object", user, ref) for ref in links if ref)
        ident, quote = record.get("turn_id"), record.get("quote", "")
        if ident:
            quotes = view.setdefault("redactions", {}).setdefault(ident, [])
            if quote not in quotes:
                quotes.append(quote)
        quotes = view.setdefault("redacted_quotes", [])
        fragments = [quote, record.get("text", "") if record["kind"] in {"condition", "note"} else ""]
        for fragment in fragments:
            if fragment and fragment not in quotes:
                quotes.append(fragment)
    view["redaction_revision"] = view.get("redaction_revision", 0) + 1
    view["experience_revision"] = view.get("experience_revision", 0) + 1
    store.save_document("memory_state", user, "", view)


def _keywords(text):
    words = set(re.findall(r"[a-z0-9]{3,}", text.casefold()))
    for group in re.findall(r"[\u3400-\u9fff]+", text):
        words.update(group[i:i + 2] for i in range(len(group) - 1))
    return words


def context_view(store, user, day, turn_id, local_draft, text=""):
    view = state(store, user)
    conditions, themes, experiences = [], [], {}
    for key, ref in view.get("current", {}).items():
        if not key.startswith("condition:"):
            continue
        item, status = get(store, user, ref, view=view)
        if status != "ok" or not applicable(item, day, local_draft.get("draft_id")):
            continue
        if item.get("source_only") and item["turn_id"] != turn_id and ref not in local_draft.get("condition_refs", []):
            continue
        conditions.append({k: item[k] for k in ("ref", "text", "ends")})
    terms = _keywords(text)
    for ref in view.get("entries", [])[:8]:
        item, status = get(store, user, ref, view=view)
        if status == "ok" and item["status"] == "usable" and dependencies_valid(
                store, user, item.get("dependencies", []), day, view=view):
            themes.append({"ref": ref, "title": item["title"]})
            for link in item.get("links", []):
                if not link.startswith("note:"):
                    continue
                note, reason = get(store, user, link, view=view)
                if reason != "ok" or note["status"] != "usable":
                    continue
                score = len(terms & _keywords(note["context"] + " " + note["text"]))
                if score >= 2:
                    experiences[link] = (score, {"ref": link, "context": note["context"][:160], "text": note["text"][:240]})
    relevant = [item for _, item in sorted(experiences.values(), key=lambda entry: -entry[0])[:2]]
    result = {key: entries for key, entries in (("conditions", conditions), ("themes", themes), ("experiences", relevant)) if entries}
    if themes:
        result["theme_navigation"] = {"operation": "search", "kinds": ["theme"]}
    return result


def draft_constraints(store, user, draft, day, view, *, historical=False):
    base = draft.get("base_constraints", draft.get("constraints", []))
    constraints = [text for text in base if redact_text(view, text) == text]
    visible_refs = []
    for ref in draft.get("condition_refs", []):
        if not historical:
            ref = view.get("current", {}).get(ref_id(ref), ref)
        item, reason = get(store, user, ref, view=view)
        if reason == "ok" and (historical or applicable(item, day, draft.get("draft_id"))):
            constraints.append(item["text"])
            visible_refs.append(ref)
    return list(dict.fromkeys(constraints)), visible_refs


def project(store, user, item, day, view):
    item = deepcopy(item)
    status, kind = item["status"], item["kind"]
    if kind == "condition" and status == "active" and item.get("ends") and day > item["ends"]:
        item["status"] = "expired"
    if kind in {"note", "theme", "feedback"}:
        refs = item.get("dependencies", [])
        if any(get(store, user, ref, view=view)[1] == "withdrawn" for ref in refs):
            return None
        if any(redact_text(view, quote["text"]) != quote["text"] for quote in item.get("quotes", [])):
            return None
        if status in {"usable", "delivered"} and not dependencies_valid(store, user, refs, day, view=view):
            return None
    if kind == "draft":
        draft = item["draft"]
        draft.pop("food_sources", None)
        draft["constraints"], draft["condition_refs"] = draft_constraints(store, user, draft, day, view, historical=True)
        draft.pop("base_constraints", None)
        draft.pop("unfinished", None)
        draft["experience_refs"] = [ref for ref in draft.get("experience_refs", [])
                                    if not is_withdrawn(view, ref)]
        # These free-text fields can echo a withdrawn source. The stored version
        # remains available to the calculator with its verified food snapshots.
        for part in draft.get("items", []):
            if part.get("assumption"):
                part["assumption"] = redact_text(view, part["assumption"])
        if "calculation" in draft:
            draft["calculation"] = {k: draft["calculation"][k] for k in ("total", "complete", "targets", "budget_difference")
                                    if k in draft["calculation"]}
    if kind == "turn":
        for part in item.get("dialogue", []):
            part["text"] = redact_text(view, part["text"])
    for key in ("text", "quote", "title", "context"):
        if item.get(key):
            item[key] = redact_text(view, item[key])
    return item


def query(rt, params):
    from .cos_journal import CommitUnavailable

    try:
        return _query(rt, params)
    except (CommitUnavailable, OSError):
        return {"status": "error", "error": "memory_storage_unavailable", "data": {"reason": "storage_error"}}


def _query(rt, params):
    store, user = rt.store, rt.user_id
    view = state(store, user)
    catalog = store.load_document("memory_index", user)
    if params["operation"] == "read":
        item, reason = get(store, user, params["ref"], view=view)
        visible = project(store, user, item, rt.day, view) if item else None
        if not visible:
            if item:
                reason = "inactive"
            elif reason == "not_found" and params["ref"] in store.load_document("memory_backfill", user).get("uncovered_refs", {}):
                reason = "not_covered"
            return {"status": "pending", "data": {"reason": reason, "coverage": catalog.get("coverage", {})}}
        return {"status": "ok", "data": {"item": visible}}
    phrase = params.get("query", "").strip().casefold()
    words = _keywords(phrase) or set(phrase.split())
    kinds = params.get("kinds", [])
    first, last = params.get("start_date", ""), params.get("end_date", "")
    items = list(catalog.get("items", {}).values())
    if not kinds or "setting" in kinds:
        items.extend(legacy_settings(store, user, view))
    def score(item):
        text = redact_text(view, item.get("text", "")).casefold()
        if not phrase:
            return 1
        if phrase in text:
            return 1 + len(words)
        if params.get("match") == "phrase":
            return 0
        return len(words & _keywords(text))

    ranked = [(score(item), item) for item in items if not kinds or item["kind"] in kinds]
    ordered = [item for relevance, item in sorted(ranked, key=lambda row: (row[0], row[1].get("date", ""), row[1]["ref"]),
                                                 reverse=True) if relevance]
    cursor = params.get("cursor", "")
    if cursor:
        found = next((i for i, entry in enumerate(ordered) if entry["ref"] == cursor), None)
        if found is None:
            return {"status": "pending", "data": {"reason": "cursor_changed"}}
        ordered = ordered[found + 1:]
    limit = params.get("limit", 6)
    results = []
    next_cursor = ""
    for entry in ordered:
        if kinds and entry["kind"] not in kinds or first and entry.get("date", "") < first or last and entry.get("date", "") >= last:
            continue
        item, reason = get(store, user, entry["ref"], view=view)
        visible = project(store, user, item, rt.day, view) if reason == "ok" else None
        if not visible:
            continue
        if len(results) == limit:
            next_cursor = results[-1]["ref"]
            break
        result = {key: visible[key] for key in ("kind", "ref", "date", "chat_id", "status") if key in visible}
        result["preview"] = visible.get("text", "")[:240]
        results.append(result)
    return {"status": "ok", "data": {"results": results, "next_cursor": next_cursor,
            "search_status": "matches" if results else "no_matches",
            "coverage": catalog.get("coverage", {"complete": False})}}


def refresh_result(store, user, day, fact):
    """Refresh a persisted tool projection before replaying it to the model."""
    view = state(store, user)
    data = fact.get("data", {})
    if "item" in data:
        item, status = get(store, user, data["item"]["ref"], view=view)
        visible = project(store, user, item, day, view) if item else None
        if visible:
            return {**fact, "data": {"item": visible}}
        return {**fact, "status": "pending", "data": {"reason": status if status != "ok" else "inactive"}}
    if "results" not in data:
        return fact
    results = []
    for entry in data["results"]:
        item, status = get(store, user, entry["ref"], view=view)
        visible = project(store, user, item, day, view) if item else None
        if status == "ok" and visible:
            results.append({**entry, "preview": visible.get("text", "")[:240], "status": visible["status"]})
    return {**fact, "data": {**data, "results": results}}


def settings_sources(rt, previous, updated, kind):
    sections = {"preferences": ("preferences",), "targets": ("targets", "daily_targets"),
                "portion": ("portions",), "reminder": ("reminders",), "favorite": ("favorites",)}.get(kind, ())
    view = state(rt.store, rt.user_id)
    for section in sections:
        if section == "daily_targets":
            old = previous.get(section, {}).get(rt.day, {})
            current = updated.get(section, {}).get(rt.day, {})
        else:
            old, current = previous.get(section, {}), updated.get(section, {})
        for key in old.keys() | current.keys():
            if key in old and key in current and old[key] == current[key]:
                continue
            location = [section, rt.day if section == "daily_targets" else "", key]
            base = identity(rt.user_id, "setting", location)
            if key in old and base not in view.get("current", {}):
                save(rt.store, rt.user_id, "setting", location, {"section": section, "key": key,
                     "setting_date": location[1], "text": f"{key} {old[key]}", "status": "active", "value": old[key]}, view=view)
            origin = source(rt, rt.message["text"].strip())
            if section == "daily_targets":
                origin["setting_date"] = rt.day
            label = f"{key} {current[key]}" if key in current else key
            save(rt.store, rt.user_id, "setting", location, {**origin, "section": section, "key": key,
                "text": label, "status": "active" if key in current else "inactive", "value": current.get(key)}, view=view)


def _condition(rt, values, view, target):
    origin = source(rt, values["quote"])
    starts, ends = condition_dates(values, rt.day)
    key = target.get("key") if target else rt.action_id
    draft_id = values.get("draft_id", "")
    item = save(rt.store, rt.user_id, "condition", key, {**origin, "key": key, "text": values["text"],
                "starts": starts, "ends": ends, "draft_id": draft_id,
                "source_only": not ends and not draft_id, "status": "active"}, view=view)
    return {"kind": "condition", "operation": "revise" if target else "save", "ref": item["ref"],
            "text": item["text"], "ends": ends}


def _feedback(rt, values, view, target):
    origin = source(rt, values["quote"])
    store, user = rt.store, rt.user_id
    signal = values["signal"]
    item = save(store, user, "feedback", rt.action_id, {**origin, "target_ref": target["ref"], "signal": signal,
                "status": "delivered", "text": origin["quote"], "dependencies": [origin["source_ref"]]}, view=view)
    view.setdefault("feedback", {}).setdefault(rt.human_id, []).append(item["ref"])
    if target["kind"] == "note" and signal in {"reject", "correct"}:
        content = {k: v for k, v in target.items() if k not in {"ref", "kind", "version", "previous"}}
        content["status"] = "conflict"
        content["refutes"] = list(dict.fromkeys([*target.get("refutes", []), item["ref"]]))
        save(store, user, "note", target["key"], content, view=view)
        view["experience_revision"] = view.get("experience_revision", 0) + 1
    store.save_document("memory_state", user, "", view)
    return {"kind": "feedback", "operation": "feedback", "ref": item["ref"], "target_ref": target["ref"],
            "stopped_experience": target["kind"] == "note" and signal in {"reject", "correct"}}


def update(rt, kind, values):
    store, user = rt.store, rt.user_id
    view = state(store, user)
    operation = values["operation"]
    quote = values.get("quote", "")
    if quote and quote not in rt.message["text"]:
        return {"status": "pending", "data": {"kind": kind, "reason": "quote_mismatch",
                "question": "需要用当前消息里的原话关联这次修改。"}}
    ref = values.get("ref", "")
    target, reason = get(store, user, ref, view=view) if ref else (None, "not_found")
    if ref and (reason != "ok" or target["status"] == "superseded"):
        return {"status": "pending", "data": {"kind": kind, "reason": reason if reason != "ok" else "superseded",
                "question": "该记忆引用已不可用于修改，请查找当前条目。"}}
    if kind == "condition" and operation in {"save", "revise"} and (not target or target["kind"] == "condition"):
        try:
            data = _condition(rt, values, view, target)
        except ValueError:
            return {"status": "pending", "data": {"kind": kind, "reason": "invalid_dates",
                    "question": "请明确这个条件适用的日期范围。"}}
    elif kind == "condition" and operation == "end" and target and target["kind"] == "condition":
        content = {k: v for k, v in target.items() if k not in {"ref", "kind", "version", "previous"}}
        content.update(status="inactive", ended_by=source(rt, values.get("quote", ""))["source_ref"])
        item = save(store, user, "condition", target["key"], content, view=view)
        data = {"kind": "condition", "operation": "end", "ref": item["ref"], "text": target["text"]}
    elif kind == "feedback" and target and target["kind"] == "note" and values.get("signal") in {"reject", "correct"}:
        data = _feedback(rt, values, view, target)
    elif operation == "withdraw" and target:
        if target["kind"] == "setting":
            updated = store.load_document("user_settings", user)
            fields = updated.get(target["section"], {})
            if target.get("setting_date"):
                fields = fields.get(target["setting_date"], {})
            if target["key"] in fields and fields[target["key"]] == target["value"]:
                del fields[target["key"]]
                store.save_document("user_settings", user, "", updated)
                rt.settings = updated
        withdraw(store, user, target, view)
        data = {"kind": "memory", "operation": "withdraw", "ref": target["ref"], "withdrawn_kind": target["kind"]}
    else:
        return {"status": "pending", "data": {"kind": kind, "reason": "invalid_target",
                "question": "请明确要修改的记忆条目或菜单。"}}
    return {"status": "ok", "data": data}
