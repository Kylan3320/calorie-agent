"""One create_agent runtime shared by Feishu, DSH and CLI.

The durable journal records native model calls before any effect. LangChain owns
model/tool routing; no AgentPlan, custom checkpoint backend or rule planner.
"""
from __future__ import annotations
import json
import os
import re
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
from .replies import model_view, project_messages, settings_view, render, progress_reply, proposal_prompt

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()

def session_lock(user_id, chat_id):
    with _LOCKS_GUARD:
        return _LOCKS.setdefault((user_id, chat_id), threading.RLock())

SYSTEM = """你是本人的饮食助手。结合当前消息和已有上下文，把用户要做的事情办妥，用自然、简洁的中文交流。
按明确的食用、记录、修改和设置意图调用工具。食物、份量、日期和指代优先沿用已知信息；数量和原文单位一并传入，由工具换算。候选供用户选择后接续原餐，本轮新返回的候选合并询问用户。
营养、账本和执行状态取自工具事实。查当前或历史摄入时先查询当前有效记录；记餐和改量已有meal_totals即可直接回复原餐合计。
记餐、改量默认一到两句：确认本次变化，给本餐热量；原餐estimated为true时自然说明“按份量估算”或“约多少克”，包括改量后同时保存常用餐的回复。用户要明细时再展开。常用餐名称按用户指定保存，其中的数字就是名称。
只问热量合计时，用一句话给合计作为完整答复。完成事项以结果收尾；多个必要缺项合并问，其他交流贴合用户当前的话题。
知识问题默认两三句：直接回答用户原问题，再给相关依据。检索词扩展只用于找资料，回答范围仍由用户消息决定。食物份量、比例及营养数字以本轮检索原文为准；资料缺少这些量时，直接用食物类别和搭配关系解释，例如“米饭配蔬菜和豆制品”。资料不足也先提供当下可用的判断原则或建议，例如饮食法可从食物选择、营养均衡和长期可行性解释，再用一句话说明具体规则或证据缺口。来源请求直接给来源和相关原文。
面向用户使用食物名称、自然指代和资料标题；记录ID、工具参数和source_id供内部操作。知识引用用本轮[K101]等标记，系统会替换成实际来源。
主动写入建议先propose，用户确认后执行。提案正文只写一句陈述解释建议，具体参数及唯一确认问题由系统交付。
"""

class DeadlineExceeded(RuntimeError):
    pass

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

class Runtime(AgentMiddleware):
    def __init__(self, message, store, config, preset, model, day, execution):
        self.message, self.store, self.config, self.preset, self.model = message, store, config, preset, model
        self.user_id = message["user_id"]
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
        self.session.setdefault("unfinished", {})
        if "messages" not in self.journal:
            history = messages_from_dict(self.session.get("messages", []))
            self.journal["messages"] = messages_to_dict(history + [HumanMessage(content=message["text"])])
            # A proposal only survives if the immediately preceding assistant asked for confirmation.
            proposal = self.session.get("proposal")
            text = message["text"].strip().lower().rstrip("。！! ")
            self.journal["confirmation"] = deepcopy(proposal) if proposal and proposal.get("presented") and text in BARE_CONFIRM else None
            if text in BARE_CANCEL:
                self.session.pop("proposal", None)
            elif text not in BARE_CONFIRM:
                self.session.pop("proposal", None)
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
        self.current["prepared"] = deepcopy(plan)
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
        if not self.preset.allow_write or not hasattr(self.store, "commit_action"):
            return self._execute(entry, handler)
        def perform():
            self._execute(entry, handler)
            return {"entry": deepcopy(entry), "session": deepcopy(self.session), "settings": deepcopy(self.settings)}
        committed = self.store.commit_action(entry["action_id"], perform)
        entry.update(deepcopy(committed["entry"]))
        self.session, self.settings = deepcopy(committed["session"]), deepcopy(committed["settings"])
        return ToolMessage(content=json.dumps(model_view(entry["result"]), ensure_ascii=False), tool_call_id=entry["call_id"], name=entry["tool"])

    def _execute(self, entry, handler=None):
        if "result" in entry:
            return ToolMessage(content=json.dumps(model_view(entry["result"]), ensure_ascii=False), tool_call_id=entry["call_id"], name=entry["tool"])
        self.check_deadline()
        self.current = entry
        self.prepared = deepcopy(entry.get("prepared"))
        name, args = entry["tool"], deepcopy(entry["args"])
        tool = TOOL_REGISTRY.get(name)
        if tool is None or name not in self.preset.allowed_tools:
            result = {"status": "error", "data": {}, "error": "tool_not_allowed"}
        elif entry["index"] > self.config.max_tool_calls:
            result = {"status": "limit", "data": {"question": "已保留进度，本轮达到工具调用上限"}}
        else:
            mode = args.get("mode", "execute")
            text = self.message["text"].strip().lower().rstrip("。！! ")
            confirmation = self.journal.get("confirmation")
            if tool.metadata["risk"] != "read_only" and (mode == "confirm" or text in BARE_CONFIRM):
                if not confirmation or confirmation["tool"] != name:
                    result = {"status": "pending", "data": {"question": "当前没有紧邻的待确认提案，请明确要执行的内容"}}
                else:
                    args = deepcopy(confirmation["args"])
                    self.session.pop("proposal", None)
                    result = tool.invoke(args)
                    self.journal["confirmation"] = None
            elif tool.metadata["risk"] != "read_only" and mode == "propose":
                args["mode"] = "execute"
                if name == "record_meal" and not args.get("date"):
                    args["date"] = self.day
                if name == "revise_intake" and not args.get("record_id"):
                    from .tools.meals import select_rows
                    rows = select_rows(self, [], latest=True)
                    if args.get("food_query"):
                        rows = [r for r in rows if r["food_name"] == args["food_query"]]
                    if len(rows) != 1:
                        raise ValueError("proposal requires one fixed intake target")
                    args["record_id"] = rows[0]["record_id"]
                target = rows[0] if name == "revise_intake" and not entry["args"].get("record_id") else (next((r for r in self.store.intake_items(self.user_id) if r["record_id"] == args.get("record_id")), {}) if name == "revise_intake" else {})
                self.session["proposal"] = {"target": {k:target[k] for k in ("food_name","date","grams") if k in target}, "proposal_id": self.action_id, "tool": name, "args": args,
                    "response_id": self.message["message_id"], "presented": False}
                result = {"status": "proposal", "data": {"proposal_id": self.action_id, "action": name, "parameters": args}}
            elif tool.metadata["risk"] != "read_only" and (mode == "cancel" or text in BARE_CANCEL) and not (name == "record_meal" and args.get("cancel_pending")):
                self.session.pop("proposal", None)
                result = {"status": "cancelled", "data": {}}
            else:
                result = handler() if handler else tool.invoke(args)
                if isinstance(result, ToolMessage):
                    result = json.loads(result.content)
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
        if messages and isinstance(messages[-1], AIMessage) and not messages[-1].tool_calls:
            messages[-1] = messages[-1].model_copy(update={"content":reply})
        elif not error:
            messages.append(AIMessage(content=reply))
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
                    middleware=[SummarizationMiddleware(summary_model or model,
                        trigger=("tokens", config.summary_trigger_tokens), keep=("tokens", config.summary_keep_tokens)),
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
