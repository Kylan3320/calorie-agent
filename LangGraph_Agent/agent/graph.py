"""One create_agent runtime shared by Feishu, DSH and CLI.

The durable journal records native model calls before any effect. LangChain owns
model/tool routing; no AgentPlan, custom checkpoint backend or rule planner.
"""
from __future__ import annotations
import json
import os
import threading
import time
import uuid
import hashlib
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from langchain.agents import create_agent
from langchain.agents.middleware import AgentMiddleware, SummarizationMiddleware, ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage, messages_to_dict, messages_from_dict
from pydantic import ValidationError
from .storage.state_store import request_scope
from .storage.telemetry import EVENTS, message_scope
from .storage.timeutil import business_scope, task_date_scope, BUSINESS_DATE, WORK_DEADLINE
from .config import load_config
from .policy import resolve_preset
from .storage import create_store
from .storage.bitable_adapter import message_key
from .model import DeepSeekModel
from .tools.registry import TOOL_REGISTRY, CURRENT_RUNTIME
from .tools.settings import BARE_CONFIRM, BARE_CANCEL
from .replies import model_view, project_messages, history_messages, settings_view, render, progress_reply, proposal_prompt

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()

def session_lock(user_id, chat_id):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault((user_id, chat_id), threading.RLock())

SYSTEM = """你是本人的饮食助手。结合当前消息和已有上下文，把用户要做的事情办妥，用自然、简洁的中文交流。
按明确的食用、记录、修改和设置意图调用工具。食物、份量、日期和指代优先沿用已知信息；数量和原文单位一并传入，由工具换算。候选供用户选择后接续原餐，本轮新返回的候选合并询问用户。
营养、账本和执行状态取自本次工具事实；历史操作引用用于定位，摄入和目标重新查询。记餐和改量已有meal_totals即可直接回复原餐合计。
记餐、改量简短确认并给本餐热量；原餐estimated为true时说明估算。明细按需展开，常用餐名称按用户原文保存。
只问热量合计时，用一句话给合计作为完整答复。完成事项以结果收尾；多个必要缺项合并问，其他交流贴合用户当前的话题。
知识回答贴合用户问题，检索词扩展只用于找资料。食物份量、比例和营养数字须有本轮证据；不足时给可支持的定性解释，简短指出影响结论的缺口。来源请求给实际出处和相关原文。
面向用户使用食物名称、自然指代和资料标题；记录ID、工具参数和source_id供内部操作。知识引用用本轮[K101]等标记，系统会替换成实际来源。
主动写入建议先propose，用户确认后执行。提案正文只写一句陈述解释建议，具体参数及唯一确认问题由系统交付。
"""

SUMMARY = """压缩饮食对话，只保留用户意图、已给条件、偏好及话题背景。
摄入数值、目标生效值和执行状态由工具与独立状态读取，不写入摘要；未完事项和记录引用已有独立保存。
对话：{messages}"""

class DeadlineExceeded(RuntimeError):
    pass


class HistorySummarization(SummarizationMiddleware):
    """Use the built-in compressor only on completed conversation turns."""
    def __init__(self, *args, current_message_id, **kwargs):
        super().__init__(*args, **kwargs)
        self.current_message_id = current_message_id

    def before_model(self, state, runtime):
        messages = state["messages"]
        start = next(i for i, m in enumerate(messages) if m.id == self.current_message_id)
        if not start:
            return None
        update = super().before_model({**state, "messages": messages[:start]}, runtime)
        if update is not None:
            for message in update["messages"]:
                if message.additional_kwargs.get("lc_source") == "summarization":
                    message.additional_kwargs["fact_scope"] = "dialogue"
            update["messages"].extend(messages[start:])
        return update


def _execution_messages(journal, message, human_id):
    messages = messages_from_dict(journal["messages"])
    start = next((i for i in reversed(range(len(messages))) if messages[i].id == human_id), None)
    if start is None:
        start = next((i for i in reversed(range(len(messages)))
                      if isinstance(messages[i], HumanMessage) and messages[i].content == message["text"]), None)
    if start is not None:
        messages[start] = messages[start].model_copy(update={"id": human_id})
        return history_messages(messages[:start]) + messages[start:]

    # A pre-fix execution may have compressed its active user message away.
    # Rebuild that exchange from the original receipt and durable native calls.
    calls = journal["calls"]
    call_ids = {c["call_id"] for c in calls}
    final = messages[-1] if messages and isinstance(messages[-1], AIMessage) and not messages[-1].tool_calls else None
    history = [m for m in messages if m is not final
               and not (isinstance(m, ToolMessage) and m.tool_call_id in call_ids)
               and not (isinstance(m, AIMessage) and any(c["id"] in call_ids for c in m.tool_calls))]
    current = [HumanMessage(content=message["text"], id=human_id)]
    pending = []
    batches = {m.tool_calls[0]["id"]: m for m in messages if isinstance(m, AIMessage)
               and m.tool_calls and all(c["id"] in call_ids for c in m.tool_calls)}
    tools = {m.tool_call_id: m for m in messages if isinstance(m, ToolMessage) and m.tool_call_id in call_ids}
    by_id = {entry["call_id"]: entry for entry in calls}
    covered = set()
    def tool_message(entry):
        return tools.get(entry["call_id"]) or ToolMessage(content=json.dumps(model_view(entry["result"]), ensure_ascii=False),
                                                        tool_call_id=entry["call_id"], name=entry["tool"])
    for entry in calls:
        if entry["call_id"] in covered:
            continue
        batch = batches.get(entry["call_id"])
        if batch is not None:
            current.append(batch)
            for call in batch.tool_calls:
                covered.add(call["id"])
                saved = by_id[call["id"]]
                if "result" in saved:
                    current.append(tool_message(saved))
            continue
        call = {"name": entry["tool"], "args": deepcopy(entry["args"]), "id": entry["call_id"], "type": "tool_call"}
        if "result" in entry:
            current.extend([AIMessage(content="", tool_calls=[call]), tool_message(entry)])
        else:
            pending.append(call)
    if pending:
        current.append(AIMessage(content="", tool_calls=pending))
    if final is not None:
        current.append(final)
    return history_messages(history) + current

def _audit_messages(serialized):
    """Audit references the tool facts already saved in calls and messages once."""
    result = deepcopy(serialized)
    for message in result:
        if message.get("type") == "tool":
            data = message["data"]
            content = data.pop("content", None)
            if content is not None:
                data["content_sha256"] = hashlib.sha256(str(content).encode()).hexdigest()
            data["result_ref"] = data["tool_call_id"]
    return result

def _saved_result(journal):
    return {**journal["result"], "model_requests": journal["model_requests"],
            "tool_results": [e["result"] for e in journal["calls"] if "result" in e],
            "native_calls": [{k:v for k,v in e.items() if k not in {"prepared", "result"}} for e in journal["calls"]]}


def _intake_version(row):
    fields = ("record_id", "date", "grams", "nutrition", "snapshot", "status", "agent_revision")
    return hashlib.sha256(json.dumps({k:row.get(k) for k in fields}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class Runtime(AgentMiddleware):
    def __init__(self, message, store, config, preset, model, day, execution):
        self.message, self.store, self.config, self.preset, self.model = message, store, config, preset, model
        self.user_id = message["user_id"]
        self.human_id = "calorie-user:" + message_key(message)
        self.started = time.monotonic()
        from .storage.timeutil import PROCESSING_DEADLINE
        self.total_deadline = min(self.started + config.deadline_seconds, PROCESSING_DEADLINE.get() or float("inf"))
        self.deadline = self.total_deadline - 5
        self.task = execution
        self.journal = deepcopy(self.task["payload"]) if self.task else {}
        self.session = deepcopy(self.journal.get("session") or store.load_session(self.user_id, message["chat_id"]))
        self.settings = deepcopy(self.journal.get("settings") or store.load_document("user_settings", self.user_id))
        self.day = self.journal.get("day") or day or store.today_string()
        self.journal.setdefault("previous_session_message_id",self.session.get("last_message_id"))
        live_session = store.load_session(self.user_id, message["chat_id"])
        self.session_superseded = live_session.get("last_message_id") not in {
            None, self.journal["previous_session_message_id"], message["message_id"]}
        self.journal.setdefault("message", message)
        self.journal.setdefault("day", self.day)
        self.journal.setdefault("calls", [])
        self.journal.setdefault("model_calls", 0)
        self.journal.setdefault("model_requests", [])
        for request in self.journal["model_requests"]:
            request["messages"] = _audit_messages(request["messages"])
        self.current = None
        self.prepared = None
        self.pending_before = {}
        self.failed = False
        self.session.setdefault("unfinished", {})
        if "messages" not in self.journal:
            history = history_messages(messages_from_dict(self.session.get("messages", [])))
            self.journal["messages"] = messages_to_dict(history + [HumanMessage(content=message["text"], id=self.human_id)])
            # A proposal only survives if the immediately preceding assistant asked for confirmation.
            proposal = self.session.get("proposal")
            text = message["text"].strip().lower().rstrip("。！! ")
            self.journal["confirmation"] = deepcopy(proposal) if proposal and proposal.get("presented") and text in BARE_CONFIRM else None
            if text in BARE_CANCEL:
                self.session.pop("proposal", None)
            elif text not in BARE_CONFIRM:
                self.session.pop("proposal", None)
        else:
            self.journal["messages"] = messages_to_dict(_execution_messages(self.journal, message, self.human_id))
        self._persisted = None
        if preset.allow_write and not self.task:
            self.journal["session"] = {k:deepcopy(v) for k,v in self.session.items() if k != "messages"}
            self.journal["settings"] = deepcopy(self.settings)
            self.task = store.begin_execution(message, self.journal)
            self._persisted = json.dumps([self.journal,False],sort_keys=True,ensure_ascii=False)
        self.persist()

    def persist(self, completed=False):
        # Native messages are persisted in journal.messages during execution and
        # session.messages at completion. Session/prepared facts need no second copy.
        self.journal["session"] = {k:deepcopy(v) for k,v in self.session.items() if k != "messages"}
        for entry in self.journal["calls"]:
            entry.get("prepared", {}).get("session", {}).pop("messages", None)
        self.journal["settings"] = deepcopy(self.settings)
        if self.preset.allow_write:
            state = json.dumps([self.journal, completed], sort_keys=True, ensure_ascii=False)
            if state != self._persisted:
                from .storage.telemetry import span
                with span("execution_save", completed=completed, bytes=len(state.encode())):
                    self.store.save_execution(self.task, self.journal, completed=completed)
                self._persisted = state

    def check_deadline(self):
        if time.monotonic() >= self.deadline:
            raise DeadlineExceeded("message processing deadline reached")

    @property
    def action_id(self):
        return self.current["action_id"]

    @property
    def action_message(self):
        return {**self.message, "message_id": f"{message_key(self.message)}:agent:{self.current['index']}"}

    def prepare(self, plan):
        self.prepared = deepcopy(plan)
        if self.pending_before:
            self.prepared["pending_before"] = deepcopy(self.pending_before)
        self.current["prepared"] = deepcopy(self.prepared)
        self.persist()
        if self.preset.allow_write and hasattr(self.store, "flush_preparation"):
            self.store.flush_preparation()

    def add_refs(self, rows):
        self.session["refs"] = list(dict.fromkeys(self.session.get("refs", []) + [r["record_id"] for r in rows]))

    def wrap_model_call(self, request, handler):
        self.check_deadline()
        # Summarization middleware runs before this point; persist its standard messages.
        request = request.override(messages=project_messages(request.messages))
        self.journal["messages"] = messages_to_dict(request.messages)
        self.journal["model_calls"] += 1
        # Keep the current native exchange as audit evidence; older full history is
        # already persisted once in messages. Do not multiply it into every request.
        current_start = next((i for i,m in reversed(list(enumerate(request.messages)))
                              if isinstance(m, HumanMessage) and m.content == self.message["text"]), 0)
        tools = [dict(name=t.name, description=t.description, schema=t.get_input_schema().model_json_schema(), risk=t.metadata["risk"]) for t in request.tools]
        tool_digest = hashlib.sha256(json.dumps(tools, sort_keys=True).encode()).hexdigest()
        known_tools = next((r for r in self.journal["model_requests"] if r.get("tools_digest") == tool_digest), None)
        self.journal["model_requests"].append({
            "messages": _audit_messages(messages_to_dict(request.messages[current_start:])), "history_message_count": current_start,
            "tools": [t["name"] for t in tools] if known_tools else tools, "tools_digest": tool_digest})
        # Persist the reservation before invoking a provider, including failures.
        self.persist()
        if isinstance(self.model, DeepSeekModel):
            self.model.request_timeout = max(1, min(40, self.deadline-time.monotonic()))
        self.check_deadline()
        response = handler(request)
        ai = response.result[0]
        self.journal["messages"] = messages_to_dict([*request.messages, ai])
        for c in ai.tool_calls:
            if any(e["call_id"] == c["id"] for e in self.journal["calls"]):
                raise ValueError("model reused a native call ID")
            index = len(self.journal["calls"]) + 1
            self.journal["calls"].append({"call_id": c["id"], "tool": c["name"], "args": deepcopy(c["args"]),
                "index": index, "action_id": f"{message_key(self.message)}:agent:{index}"})
        # Entire batch is durable before ToolNode may execute its first call.
        if ai.tool_calls:
            self.persist()
        return response

    @business_scope
    def execute(self, entry, handler=None):
        if self.failed:
            raise RuntimeError("earlier tool execution failed; restore the original task")
        session, settings = deepcopy(self.session), deepcopy(self.settings)
        messages = deepcopy(self.journal["messages"])
        confirmation = deepcopy(self.journal.get("confirmation"))
        had_result = "result" in entry
        def perform():
            self._execute(entry, handler)
            self._save_pending_progress(session)
            return {"entry": deepcopy(entry), "session": deepcopy(self.session), "settings": deepcopy(self.settings)}
        try:
            committed = (self.store.commit_action(entry["action_id"], perform)
                         if self.preset.allow_write and hasattr(self.store, "commit_action") else perform())
        except BaseException:
            # Retain durable preparation, but never checkpoint uncommitted facts.
            self.failed = True
            self.session, self.settings = session, settings
            self.journal["messages"] = messages
            self.journal["confirmation"] = confirmation
            if not had_result:
                entry.pop("result", None)
            self._persisted = None
            raise
        entry.update(deepcopy(committed["entry"]))
        self.session, self.settings = deepcopy(committed["session"]), deepcopy(committed["settings"])
        return ToolMessage(content=json.dumps(model_view(entry["result"]), ensure_ascii=False), tool_call_id=entry["call_id"], name=entry["tool"])

    def _save_pending_progress(self, previous_session):
        before = previous_session.get("unfinished", {})
        after = self.session.get("unfinished", {})
        changed = {key for key in before.keys() | after.keys() if before.get(key) != after.get(key)}
        if not self.preset.allow_write or not changed:
            return
        live = self.store.load_session(self.user_id, self.message["chat_id"])
        pending = live.setdefault("unfinished", {})
        for key in changed:
            if pending.get(key) != before.get(key):
                raise ValueError("unfinished item has changed; start a new request")
            if key in after:
                pending[key] = deepcopy(after[key])
            else:
                pending.pop(key, None)
        live["refs"] = list(dict.fromkeys(live.get("refs", []) + self.session.get("refs", [])))
        if live.get("last_meal", []) == previous_session.get("last_meal", []):
            live["last_meal"] = deepcopy(self.session.get("last_meal", []))
        self.store.save_session(self.user_id, self.message["chat_id"], live)

    def _invoke_tool(self, tool, args, handler=None):
        if tool.name == "record_meal":
            ids = [item["pending_id"] for item in args.get("items", []) if item.get("pending_id")]
            ids += args.get("cancel_pending", [])
            if len(ids) != len(set(ids)):
                raise ValueError("unfinished item may be used only once in an action")
            if ids:
                if self.prepared is not None and "pending_before" not in self.prepared:
                    raise ValueError("legacy unfinished preparation requires a new request")
                self.pending_before = deepcopy(self.prepared["pending_before"] if self.prepared is not None else
                                               {key:self.session.get("unfinished", {}).get(key) for key in ids})
                live = (self.store.load_session(self.user_id, self.message["chat_id"])
                        if self.session_superseded else self.session)
                if any(self.pending_before.get(key) is None or
                       live.get("unfinished", {}).get(key) != self.pending_before[key] for key in ids):
                    raise ValueError("unfinished item has changed or completed; start a new request")
        result = handler() if handler else tool.invoke(args)
        if isinstance(result, ToolMessage):
            if result.status == "error":
                return {"status": "error", "data": {}, "error": str(result.content)}
            return json.loads(result.content)
        return result

    def _dispatch_tool(self, tool, args, handler):
        mode = args.get("mode", "execute")
        text = self.message["text"].strip().lower().rstrip("。！! ")
        if tool.metadata["risk"] == "read_only":
            return self._invoke_tool(tool, args, handler)
        if mode == "confirm" or text in BARE_CONFIRM:
            confirmation = self.journal.get("confirmation")
            self.session.pop("proposal", None)
            if not confirmation or confirmation["tool"] != tool.name or self.session_superseded:
                self.journal["confirmation"] = None
                return {"status": "pending", "data": {"question": "原提案已失效，请明确要执行的内容"}}
            if tool.name == "revise_intake":
                from .tools.meals import select_rows
                rows = select_rows(self, [confirmation["args"]["record_id"]])
                if not rows or confirmation.get("target_version") != _intake_version(rows[0]):
                    self.journal["confirmation"] = None
                    return {"status": "pending", "data": {"question": "提案对应的摄入已变化或缺少版本依据，请重新发起修改"}}
            confirmed_args = deepcopy(confirmation["args"])
            if confirmation.get("target_ratio") is not None:
                confirmed_args.pop("grams", None)
                confirmed_args["ratio"] = confirmation["target_ratio"]
            result = self._invoke_tool(tool, confirmed_args)
            self.journal["confirmation"] = None
            return result
        if mode == "propose":
            from .tools.meals import select_rows
            from .storage.timeutil import round_number
            args = deepcopy(args)
            args["mode"] = "execute"
            proposal = {"proposal_id": self.action_id, "tool": tool.name, "args": args,
                        "response_id": self.message["message_id"], "presented": False}
            if tool.name == "record_meal" and not args.get("date"):
                args["date"] = self.day
            if tool.name == "revise_intake":
                rows = select_rows(self, [args["record_id"]] if args.get("record_id") else [], latest=not args.get("record_id"))
                if args.get("food_query"):
                    from .storage.food_match import normalize_food_name
                    rows = [row for row in rows if normalize_food_name(row["food_name"]) == normalize_food_name(args["food_query"])]
                if len(rows) != 1:
                    return {"status": "pending", "data": {"question": "请明确要修改的摄入项", "items": rows}}
                target = rows[0]
                args["record_id"] = target["record_id"]
                if args.get("ratio") is not None:
                    proposal["target_ratio"] = args.pop("ratio")
                    args["grams"] = round_number(target["grams"] * proposal["target_ratio"])
                proposal.update(target={k:target[k] for k in ("food_name", "date", "grams")}, target_version=_intake_version(target))
            self.session["proposal"] = proposal
            return {"status": "proposal", "data": {"proposal_id": self.action_id, "action": tool.name, "parameters": args}}
        if (mode == "cancel" or text in BARE_CANCEL) and not (tool.name == "record_meal" and args.get("cancel_pending")):
            self.session.pop("proposal", None)
            return {"status": "cancelled", "data": {}}
        return self._invoke_tool(tool, args, handler)

    def _execute(self, entry, handler=None):
        if "result" in entry:
            return ToolMessage(content=json.dumps(model_view(entry["result"]), ensure_ascii=False), tool_call_id=entry["call_id"], name=entry["tool"])
        self.check_deadline()
        self.current = entry
        self.prepared = deepcopy(entry.get("prepared"))
        self.pending_before = {}
        name, args = entry["tool"], deepcopy(entry["args"])
        tool = TOOL_REGISTRY.get(name)
        if tool is None or name not in self.preset.allowed_tools:
            result = {"status": "error", "data": {}, "error": "tool_not_allowed"}
        elif entry["index"] > self.config.max_tool_calls:
            result = {"status": "limit", "data": {"question": "已保留进度，本轮达到工具调用上限"}}
        else:
            try:
                args = tool.args_schema.model_validate(args).model_dump(exclude_none=True, exclude_unset=True)
                result = self._dispatch_tool(tool, args, handler)
            except ValidationError as exc:
                result = {"status": "error", "data": {}, "error": f"invalid_tool_arguments:{exc}"}
        result.update(tool=name, call_id=entry["call_id"], action_id=entry["action_id"])
        entry["result"] = result
        messages = messages_from_dict(self.journal["messages"])
        if not any(isinstance(m, ToolMessage) and m.tool_call_id == entry["call_id"] for m in messages):
            messages.append(ToolMessage(content=json.dumps(model_view(result), ensure_ascii=False), tool_call_id=entry["call_id"], name=name))
        self.journal["messages"] = messages_to_dict(messages)
        self.persist()
        return ToolMessage(content=json.dumps(model_view(result), ensure_ascii=False), tool_call_id=entry["call_id"], name=name)

    def wrap_tool_call(self, request, handler):
        entry = next(e for e in self.journal["calls"] if e["call_id"] == request.tool_call["id"])
        return self.execute(entry, lambda: handler(request))

    def finish(self, error=""):
        facts = [e["result"] for e in self.journal["calls"] if "result" in e]
        if not error and any("result" not in e for e in self.journal["calls"]):
            error = "tool_limit_reached"
        messages = messages_from_dict(self.journal["messages"])
        text = messages[-1].content if messages and isinstance(messages[-1], AIMessage) and not messages[-1].tool_calls else ""
        reply, adjustments = render(str(text), facts)
        if not text or error:
            reply = progress_reply(facts)
            if error:
                reply += "\n本轮未完成，已保留执行进度。"
        proposal = self.session.get("proposal")
        if proposal:
            adjustments.append({"reason":"proposal_confirmation_added","proposal_id":proposal["proposal_id"]})
            confirmation = proposal_prompt(proposal)
            reply = reply.replace(confirmation, "").rstrip() + "\n" + confirmation
        # Keep native paired history, and save the actual delivered reply for future references.
        fact_scope = "business" if any(f["tool"] != "query_knowledge" for f in facts) else ("knowledge" if facts else "dialogue")
        if messages and isinstance(messages[-1], AIMessage) and not messages[-1].tool_calls:
            messages[-1] = messages[-1].model_copy(update={"content":reply, "additional_kwargs": {**messages[-1].additional_kwargs, "fact_scope": fact_scope}})
        elif not error:
            messages.append(AIMessage(content=reply, additional_kwargs={"fact_scope": fact_scope}))
        self.session["messages"] = messages_to_dict(messages)
        self.journal["messages"] = messages_to_dict(messages)
        self.session["last_message_id"] = self.message["message_id"]
        self.session["last_execution_complete"] = not bool(error)
        result = {"status": "error" if error else "ok", "reply": reply, "reply_source": "fallback" if error or not text else ("agent_adjusted" if adjustments else "agent"),
            "storage_revision": self.store.journal.revision if hasattr(self.store, "journal") else None,
            "tool_results": facts, "native_calls": [{k:v for k,v in e.items() if k not in {"prepared", "result"}} for e in self.journal["calls"]],
            "model_requests": self.journal["model_requests"], "model_calls": self.journal["model_calls"],
            "model_reply": str(text), "response_adjustments": adjustments,
            "proposal_id": proposal.get("proposal_id") if proposal else None,
            "guard_result": {"checked_scope": "structured_tools_and_source_references", "fallback_used": bool(error or not text), "violations": adjustments},
            "latency_events": [e for e in (EVENTS.get() or []) if e.get("message_id")==self.message["message_id"]],
            "errors": [error] if error else [], "latency_ms": int((time.monotonic()-self.started)*1000), "trace_id": "native_"+uuid.uuid4().hex[:12]}
        if not error:
            self.journal["result"] = {k:v for k,v in result.items() if k not in {"model_requests", "native_calls", "tool_results"}}
        self.persist(completed=not error)
        if self.preset.allow_write and not self.session_superseded:
            self.store.save_session(self.user_id,self.message["chat_id"],self.session)
        trace = {"schema_version": "calorie_agent_trace.v2", "created_at": datetime.now(timezone.utc).isoformat(),
            **self.message, "raw_text": self.message["text"], "preset": self.preset.name, "model": self.config.model,
            "retriever": self.config.knowledge_retriever, **result}
        line = json.dumps(trace, ensure_ascii=False)
        print(line)
        if os.getenv("TRACE_ENABLED", "true").lower() != "false":
            path = Path(os.getenv("TRACE_PATH", "/tmp/traces.jsonl")); path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as f: f.write(line+"\n")
        return result

@request_scope
@message_scope
@task_date_scope
def run(message, *, store=None, config=None, intake_date=None, model=None, summary_model=None):
    config = config or load_config()
    preset = resolve_preset(config.preset, config.allowed_tools)
    store = store or create_store(config, preset)
    if preset.store_kind == "fake" and store.kind() != "fake":
        raise ValueError("shadow_fakewrite cannot access Bitable")
    message = {**message, "user_id": store.load_user_aliases().get(message["user_id"], message["user_id"])}
    with session_lock(message["user_id"], message["chat_id"]):
        store.prefetch_message(message)
        execution = store.load_execution(message) if preset.allow_write else None
        if execution:
            BUSINESS_DATE.set(execution["payload"].get("day") or intake_date)
        if execution and execution["payload"].get("result"):
            # Replays return the saved response without reopening the journal or
            # overwriting a newer conversation with this message's old snapshot.
            journal = execution["payload"]
            if "previous_session_message_id" in journal:
                session = store.load_session(message["user_id"],message["chat_id"])
                if (session.get("last_message_id") == journal["previous_session_message_id"] or
                    (session.get("last_message_id")==message["message_id"] and not session.get("last_execution_complete"))):
                    store.save_session(message["user_id"],message["chat_id"],{**journal["session"],"messages":journal["messages"],"last_message_id":message["message_id"]})
            return _saved_result(journal)
        model = model or DeepSeekModel(model_name=config.model)
        rt = Runtime(message, store, config, preset, model, intake_date, execution)
        token = CURRENT_RUNTIME.set(rt)
        from .storage.timeutil import PROCESSING_DEADLINE
        deadline_token = PROCESSING_DEADLINE.set(rt.total_deadline)
        work_token = WORK_DEADLINE.set(rt.deadline)
        error = ""
        try:
            # Resume the saved native batch, using its original arguments, targets and dates.
            for entry in rt.journal["calls"]:
                if "result" not in entry:
                    rt.execute(entry)
            messages = project_messages(messages_from_dict(rt.journal["messages"]))
            final_already_saved = isinstance(messages[-1], AIMessage) and not messages[-1].tool_calls
            remaining = config.max_model_calls - rt.journal["model_calls"]
            if not final_already_saved and remaining > 0:
                context = {"received_date": rt.day, "user_settings": settings_view(rt.settings),
                    "unfinished": rt.session.get("unfinished", {}), "refs": rt.session.get("refs", []),
                    "last_meal": rt.session.get("last_meal", []), "proposal": rt.session.get("proposal")}
                agent = create_agent(model=model, tools=[t for n,t in TOOL_REGISTRY.items() if n in preset.allowed_tools],
                    system_prompt=SYSTEM+"\n可信会话事实："+json.dumps(context, ensure_ascii=False),
                    middleware=[HistorySummarization(summary_model or model, current_message_id=rt.human_id,
                        trigger=("tokens", config.summary_trigger_tokens), keep=("tokens", config.summary_keep_tokens), summary_prompt=SUMMARY),
                        ModelCallLimitMiddleware(run_limit=remaining, exit_behavior="end"),
                        ToolCallLimitMiddleware(run_limit=max(1, config.max_tool_calls-len(rt.journal["calls"])), exit_behavior="end"), rt])
                final = agent.invoke({"messages": messages}, config={"max_concurrency": 1, "recursion_limit": 40})
                rt.journal["messages"] = messages_to_dict(final["messages"])
            result = rt.finish()
        except DeadlineExceeded:
            result = rt.finish("deadline_reached")
        except Exception as exc:
            if "result" in rt.journal: raise
            # Preserve journal. No automatic model retries or changed plans.
            result = rt.finish(f"{type(exc).__name__}:{str(exc)[:200]}")
        finally:
            WORK_DEADLINE.reset(work_token)
            PROCESSING_DEADLINE.reset(deadline_token)
            CURRENT_RUNTIME.reset(token)
        return result

__all__ = ["run", "session_lock"]


def mark_reply_presented(store, user_id, chat_id, result):
    """Called by the entry only after successful handoff of the saved reply."""
    if not result.get("proposal_id"):
        return
    user_id = store.load_user_aliases().get(user_id, user_id)
    session = store.load_session(user_id, chat_id)
    proposal = session.get("proposal")
    if proposal and proposal.get("proposal_id") == result["proposal_id"]:
        proposal["presented"] = True
        store.save_session(user_id, chat_id, session)
