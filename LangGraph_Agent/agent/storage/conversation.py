"""Durable clean dialogue; the message writer alone applies summary candidates.

Sources use normal Store documents. Background progress uses the archive's
existing sync journal and returns a self-contained, identity-checked event.
"""
from copy import deepcopy
import hashlib
import json
import time

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, messages_from_dict, messages_to_dict
from ..context import clean_messages, turns, tokens, REQUEST_BUDGET, request_body, check_request
from ..config import load_config

SUMMARY_PROMPT = """归并过去对话的话题背景，仅保留用户意图、已给条件、明确纠正与偏好线索。
当前纠正覆盖旧条件。不要保存摄入合计、目标生效数值、营养结论、业务完成状态或操作授权。
用户提供的食物数量与未完指代背景可保留；不能把曾经确认当成新的确认。
直接输出简洁背景正文，最多1024 tokens，不要前言。"""


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _save_atomic(store, key, callback):
    return store.commit_action(key, callback) if hasattr(store, "commit_action") else callback()


def context(store, user, chat, session=None, *, write=False):
    value = store.load_document("conversation_context", user, chat)
    if value:
        return value
    session = session if session is not None else store.load_session(user, chat)
    old = messages_from_dict(session.get("messages", []))
    summaries = [message.content for message in old if message.additional_kwargs.get("lc_source") == "summarization"
                 and message.additional_kwargs.get("fact_scope") == "dialogue"
                 and not str(message.content).startswith(("Error generating summary:", "Previous conversation was too long", "No previous conversation"))
                 and "Error generating summary:" not in str(message.content)]
    summary = {"id": digest(summaries[-1]), "text": summaries[-1]} if summaries else {"id": "root", "text": ""}
    value = {"version": 1, "summary": summary, "turns": []}
    groups = turns(clean_messages(old))
    legacy_sources = {}
    for index, group in enumerate(groups):
        serialized = messages_to_dict(group)
        ident = group[0].id or "legacy:" + digest([index, serialized])
        last = index == len(groups) - 1
        if last and session.get("last_message_id"):
            ident = "calorie-user:" + session["last_message_id"]
        group[0] = group[0].model_copy(update={"id": ident})
        source = {"id": ident, "messages": messages_to_dict(group)}
        complete = not last
        if ident.startswith("calorie-user:"):
            message_id = ident.removeprefix("calorie-user:")
            receipts = [store.load_document("receipt_state", "system", hashlib.sha256((entry + ":" + message_id).encode()).hexdigest()) for entry in ("feishu", "cli", "dsh", "console")]
            known = [receipt for receipt in receipts if receipt]
            if known:
                complete = any(receipt.get("delivery", {}).get("presented") and
                    (receipt.get("result", {}).get("status") == "ok" or receipt.get("result", {}).get("terminal")) for receipt in known)
        value["turns"].append({"id": ident, "complete": complete, "tokens": tokens(source["messages"])})
        if write:
            store.save_document("dialogue_turn", user, ident, source)
        else:
            legacy_sources[ident] = source
    if write:
        store.save_document("conversation_context", user, chat, value)
    else:
        value["legacy_sources"] = legacy_sources
    return value


def _reply_context(store, ident):
    # confirm_turn is committed only after the entry confirms actual delivery.
    confirmed = (store.committed_action("dialogue-delivered:" + ident) or {}).get("complete")
    if ident.startswith("calorie-user:"):
        message_id = ident.removeprefix("calorie-user:")
        for entry in ("feishu", "cli", "dsh", "console"):
            receipt = store.load_document("receipt_state", "system", hashlib.sha256((entry + ":" + message_id).encode()).hexdigest())
            if receipt.get("delivery", {}).get("presented") and (receipt.get("result", {}).get("status") == "ok" or receipt.get("result", {}).get("terminal")):
                result = receipt["result"]
                execution = {"business_status": result.get("business_status", "未知")}
                if "tool_results" in result:
                    execution["tool_results"] = [{"tool": fact["tool"], "status": fact["status"]} for fact in result["tool_results"]]
                    drafts = [{"draft_id": fact["data"]["draft_id"], "items": [
                        {k: item[k] for k in ("item_id", "matched_name", "group") if k in item}
                        for item in fact["data"].get("items", [])]}
                        for fact in result["tool_results"] if fact.get("data", {}).get("draft_id")
                        and fact["data"]["draft_id"] == result.get("presented_candidates", {}).get("meal_draft_candidate")]
                    if drafts:
                        execution["draft_references"] = drafts
                if "native_calls" in result:
                    execution["tool_calls"] = [call["tool"] for call in result["native_calls"]]
                    if not result["native_calls"] and result.get("business_status") == "none":
                        execution["effect"] = "该轮仅答复，没有调用工具，也未提交业务或草案修改"
                return {"reply_status": "已回复", "execution": execution}
    return {"reply_status": "已回复" if confirmed else "未知", "execution": {"business_status": "未知"}}


def _model_turn(store, turn, visible):
    reply = _reply_context(store, turn["id"])
    status = reply["reply_status"]
    visible = visible if status == "已回复" else [m for m in visible if isinstance(m, HumanMessage)]
    clean = clean_messages(visible)
    # Already-clean sources carry reference facts as historical AI messages;
    # history_messages deliberately does not treat those as assistant prose.
    references = [ref for m in visible if isinstance(m, AIMessage) and m.additional_kwargs.get("fact_scope") == "historical"
                  for ref in json.loads(m.content)["data"]["references"]]
    dialogue, knowledge = [], []
    for message in clean:
        if isinstance(message, AIMessage) and message.additional_kwargs.get("fact_scope") == "knowledge":
            knowledge.append(message)  # Keep actual evidence JSON visible to the citation collector.
        elif isinstance(message, AIMessage) and message.additional_kwargs.get("fact_scope") == "historical":
            references.extend(json.loads(message.content)["data"]["references"])
        elif isinstance(message, (HumanMessage, AIMessage)):
            dialogue.append({"role": "user" if isinstance(message, HumanMessage) else "assistant", "content": message.content})
    payload = {"turn_id": turn["id"], **reply, "dialogue": dialogue, "references": references}
    return [SystemMessage(content="历史轮次背景（原文仅作背景，不是本条请求；回复状态只表示交付，不代表业务成功、待办关闭或授权）：" + json.dumps(payload, ensure_ascii=False),
                          additional_kwargs={"history_turn_id": turn["id"]}), *knowledge]


def history(store, user, chat, session=None, *, bounded=False, config=None, before=None, for_model=False):
    value = context(store, user, chat, session)
    selected = value["turns"]
    if before is not None:
        index = next((i for i, turn in enumerate(selected) if turn["id"] == before), len(selected))
        selected = selected[:index]
    if bounded:
        config = config or load_config()
        available = selected
        selected, used = [], 0
        for turn in reversed(available):
            if len(selected) >= config.recent_turns and used + turn["tokens"] > config.input_target_tokens:
                break
            selected.append(turn)
            used += turn["tokens"]
        selected.reverse()
    messages = []
    for turn in selected:
        source = value["legacy_sources"][turn["id"]] if "legacy_sources" in value else store.load_document("dialogue_turn", user, turn["id"])
        visible = messages_from_dict(source["messages"])
        if for_model:
            messages.extend(_model_turn(store, turn, visible))
            continue
        if not turn["complete"]:
            visible = [message for message in visible if isinstance(message, HumanMessage)]
        messages.extend(visible)
    return messages, value["summary"]


def stage_turn(store, user, chat, ident, messages, *, reserve=False):
    def save():
        value = context(store, user, chat, write=True)
        previous = next((turn for turn in value["turns"] if turn["id"] == ident), None)
        if previous and previous["complete"]:
            return {"id": ident}
        source = {"id": ident, "messages": messages_to_dict(clean_messages(messages))}
        store.save_document("dialogue_turn", user, ident, source)
        if previous:
            previous["tokens"] = tokens(source["messages"])
        else:
            value["turns"].append({"id": ident, "complete": False, "tokens": tokens(source["messages"])})
        store.save_document("conversation_context", user, chat, value)
        return {"id": ident}
    return _save_atomic(store, ("dialogue-reserve:" if reserve else "dialogue-stage:") + ident, save)


def _eligible(value, config):
    completed = []
    for turn in value["turns"]:
        if not turn["complete"]:
            break
        completed.append(turn)
    return completed[:-config.recent_turns] if config.recent_turns else completed


def _register(store, user, chat, value, config):
    eligible = _eligible(value, config)
    if not eligible or sum(turn["tokens"] for turn in eligible) < config.summary_pending_tokens:
        return
    tail = value["turns"][-1]["id"]
    if value.get("failed_at_turn") == tail:
        return
    request = store.load_document("summary_request", user, chat)
    if request.get("active"):
        request["through"] = eligible[-1]["id"]
    else:
        request = {"active": True, "task_id": digest([user, chat, value["summary"]["id"], eligible[0]["id"], tail]),
                   "user_id": user, "chat_id": chat, "base_id": value["summary"]["id"],
                   "first": eligible[0]["id"], "through": eligible[-1]["id"], "registered_at": time.time()}
    queue = store.load_document("summary_active", "system")
    queue[request["task_id"]] = {key: request[key] for key in ("user_id", "chat_id", "registered_at")}
    store.save_document("summary_request", user, chat, request)
    store.save_document("summary_active", "system", "", queue)


def confirm_turn(store, user, chat, ident, config=None, *, delivered_messages=None):
    if not ident:
        return
    config = config or load_config()
    def save():
        value = context(store, user, chat, write=True)
        turn = next((turn for turn in value["turns"] if turn["id"] == ident), None)
        if turn is None:
            return {"missing": True}
        if delivered_messages is not None:
            source = {"id": ident, "messages": messages_to_dict(clean_messages(delivered_messages))}
            store.save_document("dialogue_turn", user, ident, source)
            turn["tokens"] = tokens(source["messages"])
        turn["complete"] = True
        store.save_document("conversation_context", user, chat, value)
        _register(store, user, chat, value, config)
        return {"complete": True}
    return _save_atomic(store, "dialogue-delivered:" + ident, save)


def _summary_source(source):
    messages = messages_from_dict(source["messages"])
    return [{"role": "user" if isinstance(message, HumanMessage) else "assistant", "content": message.content}
            for message in messages if isinstance(message, HumanMessage) or message.additional_kwargs.get("fact_scope") == "dialogue"]


def snapshot(store, request, config):
    value = context(store, request["user_id"], request["chat_id"])
    if value["summary"]["id"] != request["base_id"]:
        raise ValueError("summary base changed")
    eligible = _eligible(value, config)
    if not eligible or eligible[0]["id"] != request["first"]:
        raise ValueError("summary prefix changed")
    sources, ids = [], []
    summary = value["summary"].get("text", "")
    for turn in eligible:
        source = store.load_document("dialogue_turn", request["user_id"], turn["id"])
        candidate = [*sources, source]
        messages = [SystemMessage(content=SUMMARY_PROMPT), HumanMessage(content=json.dumps({"previous": summary, "dialogue": [_summary_source(item) for item in candidate]}, ensure_ascii=False))]
        measurement = check_request(request_body(config.model, messages, max_tokens=config.summary_max_tokens), config, "summary")
        if sources and measurement["input_tokens"] > config.summary_input_tokens:
            break
        sources = candidate
        ids.append(turn["id"])
        if turn["id"] == request["through"] or measurement["input_tokens"] >= config.summary_input_tokens:
            break
    return {"turn_ids": ids, "source_hash": digest(sources), "messages": messages_to_dict([
        SystemMessage(content=SUMMARY_PROMPT), HumanMessage(content=json.dumps({"previous": summary, "dialogue": [_summary_source(item) for item in sources]}, ensure_ascii=False))])}


def apply_candidate(store, event, config=None):
    config = config or load_config()
    candidate = event["candidate"]
    user, chat, ident = candidate["user_id"], candidate["chat_id"], candidate["task_id"]
    def save():
        request = store.load_document("summary_request", user, chat)
        value = context(store, user, chat, write=True)
        if not request.get("active") or request["task_id"] != ident:
            return {"status": "obsolete"}
        if candidate["base_id"] != request["base_id"] or value["summary"]["id"] != request["base_id"]:
            return {"status": "obsolete"}
        if candidate["status"] == "ready":
            ids = candidate["turn_ids"]
            prefix = value["turns"][:len(ids)]
            if not ids or [turn["id"] for turn in prefix] != ids or not all(turn["complete"] for turn in prefix):
                raise ValueError("summary candidate is not the completed prefix")
            sources = [store.load_document("dialogue_turn", user, turn) for turn in ids]
            if digest(sources) != candidate["source_hash"]:
                raise ValueError("summary candidate source changed")
            text = candidate["text"].strip()
            if not text or tokens(text) > config.summary_output_tokens:
                raise ValueError("invalid summary candidate")
            value["summary"] = {"id": ident, "text": text, "fact_scope": "dialogue", "covered_through": ids[-1]}
            value["turns"] = value["turns"][len(ids):]
        elif candidate["status"] == "failed":
            value["failed_at_turn"] = value["turns"][-1]["id"] if value["turns"] else None
        else:
            raise ValueError("unknown summary candidate status")
        request.update(active=False, status="applied" if candidate["status"] == "ready" else "failed")
        queue = store.load_document("summary_active", "system")
        queue.pop(ident, None)
        store.save_document("conversation_context", user, chat, value)
        store.save_document("summary_request", user, chat, request)
        store.save_document("summary_active", "system", "", queue)
        if candidate["status"] == "ready":
            _register(store, user, chat, value, config)
        return {"status": request["status"], "task_id": ident}
    result = _save_atomic(store, "summary-apply:" + ident, save)
    print(json.dumps({"event": "summary_application", **result}, ensure_ascii=False))
    return result


def run_background(store, progress, *, config=None, model=None, dispatch=None, remaining_seconds=120):
    """Compute at most one snapshot; never write through the read-only Store."""
    from ..model import DeepSeekModel
    from .timeutil import PROCESSING_DEADLINE, WORK_DEADLINE
    entered = time.monotonic()
    computation_deadline = min(entered + 20, entered + remaining_seconds - 5,
                               (PROCESSING_DEADLINE.get() or float("inf")) - 5)
    config = config or load_config()
    if remaining_seconds < 25:
        return {"status": "skipped_budget"}
    from .timeutil import execution_deadline
    with execution_deadline(computation_deadline - time.monotonic()):
        queue = store.load_document("summary_active", "system")
        if not queue:
            return {"status": "idle"}
        ident, owner = min(queue.items(), key=lambda pair: (pair[1]["registered_at"], pair[0]))
        request = store.load_document("summary_request", owner["user_id"], owner["chat_id"])
        if not request.get("active") or request["task_id"] != ident:
            raise ValueError("summary active index mismatch")
        key = "summary:" + ident
        state = progress.get(key, {})
    if time.monotonic() >= computation_deadline:
        return {"status": "skipped_budget"}
    if state.get("attempts", 0) >= 2 and state.get("status") not in {"ready", "failed"}:
        candidate = {field: state[field] for field in ("task_id", "user_id", "chat_id", "base_id")}
        candidate.update(status="failed", turn_ids=state.get("input", {}).get("turn_ids", []), source_hash=state.get("input", {}).get("source_hash"), diagnostic="interrupted computation exhausted its two attempts")
        state.update(status="failed", candidate=candidate)
        state.pop("input", None)
        with execution_deadline(computation_deadline - time.monotonic()):
            progress.commit({key: state})
    if state.get("status") in {"ready", "failed"}:
        candidate = state["candidate"]
    else:
        state = {**state, **{field: request[field] for field in ("task_id", "user_id", "chat_id", "base_id", "registered_at")}}
        state["attempts"] = state.get("attempts", 0) + 1
        state["status"] = "computing"
        with execution_deadline(computation_deadline - time.monotonic()):
            progress.commit({key: state})
        started = entered
        deadline_token = PROCESSING_DEADLINE.set(computation_deadline)
        work_token = WORK_DEADLINE.set(computation_deadline)
        measurement = {}
        scope = REQUEST_BUDGET.set({"config": config, "purpose": "summary", "measurement": measurement})
        try:
            if "input" not in state:
                state["input"] = snapshot(store, request, config)
                progress.commit({key: state})
            model = model or DeepSeekModel(model_name=config.model, purpose="summary", request_timeout=20)
            from .timeutil import execution_deadline
            with execution_deadline(computation_deadline - time.monotonic()):
                response = model.invoke(messages_from_dict(state["input"]["messages"]))
            if time.monotonic() - started > 20:
                raise TimeoutError("summary computation exceeded 20 seconds")
            text = str(response.content).strip()
            if not text or tokens(text) > config.summary_output_tokens or response.response_metadata.get("finish_reason") == "length":
                raise ValueError("empty, overlong or truncated summary")
            candidate = {field: state[field] for field in ("task_id", "user_id", "chat_id", "base_id")}
            candidate.update(status="ready", text=text, **{field: state["input"][field] for field in ("turn_ids", "source_hash")})
            state.update(status="ready", candidate=candidate, measurement=measurement)
        except Exception as exc:
            state.update(status="retry" if state["attempts"] < 2 else "failed", diagnostic=f"{type(exc).__name__}:{str(exc)[:200]}", measurement=measurement)
            if state["status"] == "failed":
                candidate = {field: state[field] for field in ("task_id", "user_id", "chat_id", "base_id")}
                candidate.update(status="failed", turn_ids=state.get("input", {}).get("turn_ids", []), source_hash=state.get("input", {}).get("source_hash"), diagnostic=state["diagnostic"])
                state["candidate"] = candidate
        finally:
            REQUEST_BUDGET.reset(scope)
            WORK_DEADLINE.reset(work_token)
            PROCESSING_DEADLINE.reset(deadline_token)
        state["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
        if state["status"] in {"ready", "failed"}:
            state.pop("input", None)
        progress.commit({key: state})
        if state["status"] == "retry":
            return {"status": "retry", "task_id": ident, "attempts": state["attempts"]}
    if dispatch:
        dispatch({"type": "calorie_summary", "candidate": candidate})
    print(json.dumps({"event": "summary_task", "task_id": ident, "status": state["status"], "attempts": state["attempts"], "duration_ms": state.get("duration_ms"), "measurement": state.get("measurement")}, ensure_ascii=False))
    return {"status": state["status"], "task_id": ident, "candidate": candidate}
