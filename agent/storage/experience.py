"""Experience sources and the message writer's publication contract."""
from datetime import datetime, timedelta, timezone
import hashlib
import json

from . import memory


def local_day(config):
    return datetime.now(timezone(timedelta(hours=config.timezone_offset_hours))).date().isoformat()


def source_stamp(snapshot):
    records = {record["ref"]: record for record in [*snapshot["materials"], *snapshot["related"]]}
    payload = json.dumps([records, snapshot["entry_refs"]], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def read_for_review(store, user, ref, day):
    view = memory.state(store, user)
    item, reason = memory.get(store, user, ref, view=view)
    if reason != "ok":
        return {"status": reason, "ref": ref}
    visible = memory.project(store, user, item, day, view)
    return {"status": "ok", "item": visible} if visible else {"status": "inactive", "ref": ref}


def snapshot(store, user, material_refs, day):
    """Read only queued materials and their directly associated records."""
    view = memory.state(store, user)
    materials, related = [], {}
    for ref in material_refs:
        item, status = memory.get(store, user, ref, view=view)
        if status != "ok" or not memory.dependencies_valid(store, user, [ref], day, view=view):
            continue
        item = memory.project(store, user, item, day, view)
        if not item:
            continue
        review = store.load_document("memory_review", user, ref)
        marks = []
        for mark in review.get("sources", []):
            evidence, reason = memory.get(store, user, mark["ref"], view=view)
            if reason == "ok" and memory.project(store, user, evidence, day, view):
                marks.append({**mark, "reason": memory.redact_text(view, mark["reason"])})
        if marks:
            item["review_sources"] = marks
        materials.append(item)
        draft = item.get("draft", {})
        target_ref = item.get("target_ref")
        current_target = view.get("current", {}).get(memory.ref_id(target_ref)) if target_ref else None
        for related_ref in (*item.get("dependencies", []), target_ref, current_target, review.get("draft_ref"),
                            *(mark["ref"] for mark in marks if mark["ref"] != ref),
                            draft.get("parent_ref"), *draft.get("experience_refs", []), *draft.get("condition_refs", [])):
            if not related_ref or related_ref in related:
                continue
            target, reason = memory.get(store, user, related_ref, view=view)
            visible = memory.project(store, user, target, day, view) if reason == "ok" else None
            if visible:
                related[related_ref] = visible
    anchors = {memory.ref_id(ref) for ref in related}
    links = view.get("experience_links", {})
    for _ in range(2):
        next_anchors = set()
        for anchor in sorted(anchors):
            for base in links.get(anchor, [])[-8:]:
                ref = view.get("current", {}).get(base)
                if not ref or ref in related:
                    continue
                item, reason = memory.get(store, user, ref, view=view)
                visible = memory.project(store, user, item, day, view) if reason == "ok" else None
                if visible:
                    related[ref] = visible
                    next_anchors.add(base)
        anchors = next_anchors
    entry_refs = [ref for ref in view.get("entries", [])[:8]
                  if memory.dependencies_valid(store, user, [ref], day, view=view)]
    return {"user_id": user, "base_revision": view.get("experience_revision", 0),
            "materials": materials, "related": list(related.values()), "entry_refs": entry_refs}


def _versioned_ref(user, view, kind, key):
    previous = view.get("current", {}).get(memory.identity(user, kind, key))
    version = int(previous.rsplit(":", 1)[1]) + 1 if previous else 1
    return memory.reference(user, kind, key, version)


def _prepare(store, user, day, view, changes, entries):
    updates, proposed = [], {}
    for change in changes:
        kind, key = change["kind"], change["key"]
        if kind not in {"note", "theme"} or not key or (kind, key) in proposed:
            raise ValueError("candidate may only update unique notes and themes")
        if memory.identity(user, kind, key) in view.get("revoked", {}):
            raise ValueError("candidate cannot restore a withdrawn experience")
        proposed[kind, key] = _versioned_ref(user, view, kind, key)
    for change in changes:
        kind, key = change["kind"], change["key"]
        text, status = change["text"].strip(), change["status"]
        if not text or status not in {"pending", "usable", "conflict", "inactive"}:
            raise ValueError("invalid experience status or text")
        payload = {"key": key, "date": day, "text": text, "status": status}
        if kind == "note":
            supports, refutes = change.get("supports", []), change.get("refutes", [])
            refs = list(dict.fromkeys([*supports, *refutes, *change.get("conditions", [])]))
            if not refs or not memory.dependencies_valid(store, user, refs, day, view=view):
                raise ValueError("experience source is unavailable")
            evidence = {ref: memory.get(store, user, ref, view=view)[0] for ref in supports}
            quotes = change.get("quotes", [])
            if any(quote.get("ref") not in evidence or not quote.get("text") or not any(
                    part["role"] == "user" and quote["text"] in memory.redact_text(view, part["text"])
                    for part in evidence[quote["ref"]].get("dialogue", [])) for quote in quotes):
                raise ValueError("experience quote must be present in its cited user source")
            if status == "usable" and (refutes or not (quotes or any(
                    item["kind"] == "feedback" and item.get("signal") in {"accept", "correct", "result"}
                    for item in evidence.values()))):
                raise ValueError("usable experience requires supporting feedback or result")
            payload.update(context=change["context"], supports=supports, refutes=refutes, quotes=quotes, dependencies=refs)
        else:
            refs = []
            for link in change.get("links", []):
                ref = proposed.get((link["kind"], link["key"])) if "key" in link else link.get("ref")
                if not ref or not ref.startswith(("note:", "theme:", "draft:", "turn:")):
                    raise ValueError("invalid theme link")
                if ref not in proposed.values() and not memory.dependencies_valid(store, user, [ref], day, view=view):
                    raise ValueError("theme link is unavailable")
                refs.append(ref)
            if not refs or proposed[kind, key] in refs:
                raise ValueError("theme requires linked material")
            payload.update(title=change["title"], links=refs, dependencies=refs)
        updates.append((kind, key, payload))
    if entries is not None:
        entry_refs = []
        for entry in entries:
            ref = proposed.get(("theme", entry["key"])) if "key" in entry else entry.get("ref")
            if not ref or not ref.startswith("theme:"):
                raise ValueError("memory entry requires a theme")
            if ref not in proposed.values() and not memory.dependencies_valid(store, user, [ref], day, view=view):
                raise ValueError("entry theme is unavailable")
            entry_refs.append(ref)
    else:
        entry_refs = None
    return updates, entry_refs


def _complete(store, user, work, refs, status):
    pending = work.setdefault("pending", {})
    for ref in refs:
        pending.pop(ref, None)
        work.get("attempts", {}).pop(ref, None)
        work.get("budgets", {}).pop(ref, None)
        work.setdefault("processed", {})[ref] = status
    queue = store.load_document("memory_active", "system")
    if pending:
        queue[user] = min(pending.values())
    else:
        queue.pop(user, None)
    store.save_document("memory_work", user, "", work)
    store.save_document("memory_active", "system", "", queue)


def apply_candidate(store, event, config):
    candidate = event["candidate"]
    user, batch_id = candidate["user_id"], candidate["batch_id"]
    day = local_day(config)

    def commit():
        work = store.load_document("memory_work", user)
        refs = candidate["materials"]
        if not refs or any(ref not in work.get("pending", {}) for ref in refs):
            return {"status": "obsolete"}
        view = memory.state(store, user)
        current = snapshot(store, user, refs, day)
        valid = {item["ref"] for item in current["materials"]}
        invalid = [ref for ref in refs if ref not in valid]
        if invalid:
            _complete(store, user, work, invalid, "source_inactive")
        stale_read = False
        for source in candidate.get("source_reads", []):
            from .conversation import digest
            result = read_for_review(store, user, source["ref"], day)
            if result["status"] != "ok" or digest(result["item"]) != source["digest"]:
                stale_read = True
                break
        if (invalid or candidate["base_revision"] != current["base_revision"]
                or candidate["source_stamp"] != source_stamp(current) or stale_read):
            remaining = [ref for ref in refs if ref in valid]
            budget = candidate.get("budget", {})
            if candidate.get("attempts", 1) >= 2 or budget.get("elapsed_ms", 0) >= 20000 or budget.get("generated_tokens", 0) >= 4096:
                _complete(store, user, work, remaining, "retry_required")
            elif remaining:
                retries = work.setdefault("attempts", {})
                retries.update({ref: candidate.get("attempts", 1) for ref in remaining})
                if budget:
                    work.setdefault("budgets", {}).update({ref: budget for ref in remaining})
                store.save_document("memory_work", user, "", work)
            return {"status": "obsolete"}
        status = candidate["status"]
        if status in {"empty", "failed"}:
            _complete(store, user, work, refs, status)
            return {"status": status}
        if status != "ready":
            raise ValueError("unknown experience candidate status")
        try:
            updates, entries = _prepare(store, user, day, view, candidate["changes"], candidate.get("entries"))
        except (KeyError, TypeError, ValueError) as error:
            _complete(store, user, work, refs, "invalid_candidate")
            return {"status": "invalid_candidate", "reason": str(error)}
        for kind, key, payload in updates:
            item = memory.save(store, user, kind, key, payload, view=view, new_version=True)
            links = view.setdefault("experience_links", {})
            for ref in payload["dependencies"]:
                source, reason = memory.get(store, user, ref, view=view)
                linked = [ref]
                if reason == "ok" and source.get("target_ref"):
                    linked.append(source["target_ref"])
                for linked_ref in linked:
                    items = links.setdefault(memory.ref_id(linked_ref), [])
                    base = memory.ref_id(item["ref"])
                    if base not in items:
                        items.append(base)
        if entries is not None:
            view["entries"] = entries
        view["experience_revision"] = current["base_revision"] + 1
        store.save_document("memory_state", user, "", view)
        _complete(store, user, work, refs, "published")
        return {"status": "applied", "batch_id": batch_id}

    return store.commit_action("experience-apply:" + memory.identity(user, "note", batch_id), commit)
