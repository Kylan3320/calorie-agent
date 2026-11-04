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
from langchain.agents.middleware import AgentMiddleware, ModelCallLimitMiddleware, ToolCallLimitMiddleware
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage, messages_to_dict, messages_from_dict
from langchain_core.utils.function_calling import convert_to_openai_tool
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
from .replies import model_view, project_messages, history_messages, state_view, render, progress_reply, proposal_prompt, tool_facts_reply
from .context import select_messages, REQUEST_BUDGET, InputBudgetExceeded, ModelRequestFailed, TruncatedGeneration, StateCheckpointFailed
from .storage import conversation

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
record_meal、revise_intake、query_today、query_history 的本批结果若足以回应整条请求（包括必要缺项问询），每个调用显式给出finish_after_tools=true，系统直接用完整工具事实回执。
完整请求尚有后续工具、解释、建议或知识回答未覆盖时不声明完成。先查历史再改量，查历史批次不是完成；记餐后还问今日剩余，须取得所需查询后才完成。
"""

class DeadlineExceeded(RuntimeError):
    pass


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
        self.task = execution
        self.journal = deepcopy(self.task["payload"]) if self.task else {}
        self.journal.setdefault("budget_started_at", message.get("budget_started_at") if message.get("budget_started_at") is not None else message.get("received_at") if message.get("received_at") is not None else time.time())
        self.journal.setdefault("legacy_budget", message.get("legacy_budget", message.get("received_at") is None))
        self.journal.setdefault("deadline_at", message.get("deadline_at") or self.journal["budget_started_at"] + config.deadline_seconds)
        self.total_deadline = self.started + self.journal["deadline_at"] - time.time()
        self.recovery_deadline = min(self.started + config.deadline_seconds, PROCESSING_DEADLINE.get() or float("inf"))
        self.deadline = min(self.total_deadline, self.recovery_deadline) - 5
        self.journal.setdefault("model_steps", [])
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
        conversation.context(store, self.user_id, message["chat_id"], live_session, write=preset.allow_write)
        if preset.allow_write:
            conversation.stage_turn(store, self.user_id, message["chat_id"], self.human_id,
                [HumanMessage(content=message["text"], id=self.human_id)], reserve=True)
        if "messages" not in self.journal:
            self.journal["messages"] = messages_to_dict([HumanMessage(content=message["text"], id=self.human_id)])
            # A proposal only survives if the immediately preceding assistant asked for confirmation.
            proposal = self.session.get("proposal")
            text = message["text"].strip().lower().rstrip("。！! ")
            self.journal["confirmation"] = deepcopy(proposal) if proposal and proposal.get("presented") and text in BARE_CONFIRM else None
            if text in BARE_CANCEL:
                self.session.pop("proposal", None)
            elif text not in BARE_CONFIRM:
                self.session.pop("proposal", None)
        else:
            restored = _execution_messages(self.journal, message, self.human_id)
            start = next(i for i, item in enumerate(restored) if item.id == self.human_id)
            self.journal["messages"] = messages_to_dict(restored[start:])
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

    def try_tool_facts(self):
        if self.journal.get("direct_reply"):
            return True
        calls = self.journal["calls"]
        if not calls or not calls[-1].get("batch_id"):
            return False
        batch = [entry for entry in calls if entry.get("batch_id") == calls[-1]["batch_id"]]
        complete = all(entry.get("finish_after_tools") is True and "result" in entry and
                       entry["tool"] in {"record_meal", "revise_intake", "query_today", "query_history"} for entry in batch)
        self.journal["completion"] = {"batch_id": calls[-1]["batch_id"], "declared": [entry.get("finish_after_tools", False) for entry in batch], "adopted": False}
        if not complete:
            return False
        # The final batch declares its results sufficient. Keep earlier writes
        # and non-success results; successful reads may have only informed it.
        selected = []
        for entry in calls:
            if "result" not in entry:
                continue
            tool = TOOL_REGISTRY.get(entry["tool"])
            if (entry not in batch and entry["result"].get("status") == "ok"
                    and tool is not None and tool.metadata["risk"] == "read_only"):
                continue
            selected.append(entry["result"])
        text = tool_facts_reply(selected)
        if text is None:
            return False
        self.journal["completion"]["adopted"] = True
        self.journal["direct_reply"] = text
        return True

    def terminate(self, reason):
        # An unavailable/poisoned Store raises here and retains the original
        # recovery task. A confirmed absence is not permission to write again.
        from .storage.timeutil import BUSINESS_IO
        token = BUSINESS_IO.set(False)
        try:
            for entry in self.journal["calls"]:
                if "result" in entry:
                    continue
                committed = self.store.committed_action(entry["action_id"]) if hasattr(self.store, "committed_action") else None
                if committed is not None:
                    entry.update(deepcopy(committed["entry"]))
                    self.session, self.settings = deepcopy(committed["session"]), deepcopy(committed["settings"])
                else:
                    entry["terminated"] = reason
            if self.try_tool_facts():
                return self.finish()
            self.journal["termination"] = {"reason": reason, "at": time.time(),
                "unexecuted_action_ids": [entry["action_id"] for entry in self.journal["calls"] if "result" not in entry]}
            return self.finish(reason, terminal=True)
        finally:
            BUSINESS_IO.reset(token)

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
        steps = self.journal["model_steps"]
        step = steps[-1] if steps and steps[-1].get("status") == "reserved" else None
        if step is None:
            if self.journal["model_calls"] >= self.config.max_model_calls:
                raise ModelRequestFailed("model_call_limit", "model decision budget exhausted")
            now = time.time()
            step = {"id": f"{message_key(self.message)}:model:{self.journal['model_calls'] + 1}",
                    "started_at": now, "deadline_at": min(now + self.config.model_step_seconds, now + self.deadline - time.monotonic()),
                    "status": "reserved", "attempts": []}
            steps.append(step)
            self.journal["model_calls"] += 1
            self.persist()
        step_deadline = time.monotonic() + step["deadline_at"] - time.time()
        cached = "response" in step
        if step_deadline <= time.monotonic() and not cached:
            raise ModelRequestFailed("model_step_deadline")
        projection_started = time.monotonic()
        native = project_messages(request.messages)
        self.journal["messages"] = messages_to_dict(native)
        history, summary = conversation.history(self.store, self.user_id, self.message["chat_id"], bounded=True, config=self.config, before=self.human_id)
        state = state_view(self)
        projected, measurement = select_messages(history, summary, native, SYSTEM, state,
            [convert_to_openai_tool(tool) for tool in request.tools], self.config, self.config.model)
        request = request.override(messages=[SystemMessage(content="可信会话事实：" + json.dumps(state, ensure_ascii=False)), *projected], system_message=SystemMessage(content=SYSTEM + "\n"))
        saved_request = next((item for item in self.journal["model_requests"] if item.get("step_id") == step["id"]), None)
        if saved_request is None:
            current_start = next((i for i, item in reversed(list(enumerate(request.messages))) if isinstance(item, HumanMessage) and item.id == self.human_id), 0)
            tools = [dict(name=tool.name, description=tool.description, schema=tool.get_input_schema().model_json_schema(), risk=tool.metadata["risk"]) for tool in request.tools]
            tool_digest = hashlib.sha256(json.dumps(tools, sort_keys=True).encode()).hexdigest()
            known_tools = any(item.get("tools_digest") == tool_digest for item in self.journal["model_requests"])
            saved_request = {"step_id": step["id"], "messages": _audit_messages(messages_to_dict(request.messages[current_start:])),
                             "history_message_count": current_start, "tools": [tool["name"] for tool in tools] if known_tools else tools,
                             "tools_digest": tool_digest, "budget": measurement,
                             "projection_ms": round((time.monotonic() - projection_started) * 1000, 3)}
            self.journal["model_requests"].append(saved_request)
        measurement = saved_request["budget"]
        self.persist()
        self.check_deadline()
        if time.monotonic() >= step_deadline and not cached:
            raise ModelRequestFailed("model_step_deadline")
        if isinstance(self.model, DeepSeekModel):
            self.model.request_timeout = self.config.model_step_seconds
        def checkpoint():
            try:
                self.persist()
            except Exception as exc:
                raise StateCheckpointFailed("model step checkpoint requires recovery") from exc
        scope = REQUEST_BUDGET.set({"config": self.config, "purpose": "main", "measurement": measurement,
                                   "step": step, "step_deadline": step_deadline, "checkpoint": checkpoint})
        try:
            response = handler(request)
            if not cached and time.monotonic() >= step_deadline:
                raise ModelRequestFailed("model_step_deadline", "late model result discarded")
            self.check_deadline()
        except (InputBudgetExceeded, ModelRequestFailed, TruncatedGeneration, StateCheckpointFailed):
            raise
        except Exception as exc:
            raise ModelRequestFailed("model_error", type(exc).__name__ + ": model step failed") from exc
        finally:
            REQUEST_BUDGET.reset(scope)
        ai = response.result[0]
        self.journal["messages"] = messages_to_dict([*native, ai])
        for call in ai.tool_calls:
            if any(entry["call_id"] == call["id"] for entry in self.journal["calls"]):
                raise ModelRequestFailed("model_response_invalid", "model reused a native call ID")
            index = len(self.journal["calls"]) + 1
            args = deepcopy(call["args"])
            finish = args.pop("finish_after_tools", False)
            self.journal["calls"].append({"call_id": call["id"], "tool": call["name"], "args": args,
                "finish_after_tools": finish is True, "batch_id": step["id"], "index": index,
                "action_id": f"{message_key(self.message)}:agent:{index}"})
        step["status"] = "response_saved"
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
            from .storage.telemetry import span
            with span("tool_commit", tool=entry["tool"], action_id=entry["action_id"]):
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

    def finish(self, error="", *, terminal=False):
        if terminal:
            proposal = self.session.get("proposal")
            confirmation = self.journal.get("confirmation") or {}
            if proposal and (proposal.get("response_id") == self.message["message_id"] or proposal.get("proposal_id") == confirmation.get("proposal_id")):
                self.session.pop("proposal", None)
            self.journal["confirmation"] = None
        facts = [e["result"] for e in self.journal["calls"] if "result" in e]
        if not error and any("result" not in e for e in self.journal["calls"]):
            error = "tool_limit_reached"
        messages = messages_from_dict(self.journal["messages"])
        direct = self.journal.get("direct_reply") if not error else None
        text = "" if direct else messages[-1].content if messages and isinstance(messages[-1], AIMessage) and not messages[-1].tool_calls else ""
        reply, adjustments = (direct, []) if direct else render(str(text), facts)
        if (not text and not direct) or error:
            reply = progress_reply(facts)
            if error:
                if terminal and error == "input_budget_exceeded":
                    histories = [fact["data"] for fact in facts if fact.get("tool") == "query_history" and fact.get("status") == "ok"]
                    if histories:
                        data = histories[-1]
                        reply = f"{data['start_date']}至{data['end_date']}（不含尾日）共{data['count']}项，合计{data['total']['kcal']:g}千卡。完整明细超过输入范围。"
                if terminal:
                    suffix = "本次输入超过模型可处理范围，请缩小范围后重新发送。" if error == "input_budget_exceeded" else "本次等待已到期，请重新发送。" if error == "deadline_reached" else "本次模型处理未能完成，请重新发送。"
                    reply += "\n" + suffix + "未执行的动作已终止，已完成的部分保留。"
                else:
                    reply += "\n本轮未完成，已保留执行进度；结果仍需核对。"
        proposal = self.session.get("proposal")
        if proposal:
            adjustments.append({"reason":"proposal_confirmation_added","proposal_id":proposal["proposal_id"]})
            confirmation = proposal_prompt(proposal)
            reply = reply.replace(confirmation, "").rstrip() + "\n" + confirmation
        # Keep native paired history, and save the actual delivered reply for future references.
        fact_scope = "business" if any(f["tool"] != "query_knowledge" for f in facts) else ("knowledge" if facts else "dialogue")
        if messages and isinstance(messages[-1], AIMessage) and not messages[-1].tool_calls:
            messages[-1] = messages[-1].model_copy(update={"content":reply, "additional_kwargs": {**messages[-1].additional_kwargs, "fact_scope": fact_scope}})
        elif not error or terminal:
            messages.append(AIMessage(content=reply, additional_kwargs={"fact_scope": fact_scope}))
        self.session["messages"] = messages_to_dict(messages)
        self.journal["messages"] = messages_to_dict(messages)
        self.session["last_message_id"] = self.message["message_id"]
        self.session["last_execution_complete"] = not bool(error) or terminal
        result = {"status": "error" if error else "ok", "reply": reply, "reply_source": "tool_facts" if direct else "fallback" if error or not text else ("agent_adjusted" if adjustments else "agent"),
            "storage_revision": self.store.journal.revision if hasattr(self.store, "journal") else None,
            "tool_results": facts, "native_calls": [{k:v for k,v in e.items() if k not in {"prepared", "result"}} for e in self.journal["calls"]],
            "model_requests": self.journal["model_requests"], "model_calls": self.journal["model_calls"],
            "model_reply": str(text), "response_adjustments": adjustments,
            "model_steps": [{key:value for key,value in step.items() if key not in {"request_body", "response"}} for step in self.journal["model_steps"]],
            "completion": self.journal.get("completion"), "deadline_at": self.journal["deadline_at"], "legacy_budget": self.journal["legacy_budget"],
            "termination_reason": error if terminal else None,
            "completion_status": "terminated" if terminal else "needs_reconciliation" if error else "awaiting_user" if any(fact.get("status") == "pending" or fact.get("data", {}).get("unfinished") for fact in facts) else "completed",
            "business_status": "committed" if any(fact.get("tool") in {"record_meal", "revise_intake", "update_user_settings"} and fact.get("status") == "ok" for fact in facts) else "none",
            "proposal_id": proposal.get("proposal_id") if proposal else None,
            "dialogue_turn_id": self.human_id, "terminal": terminal,
            "guard_result": {"checked_scope": "committed_tool_facts_and_batch_completion" if direct else "structured_tools_and_source_references", "fallback_used": bool(error or (not text and not direct)), "violations": adjustments},
            "latency_events": [e for e in (EVENTS.get() or []) if e.get("message_id")==self.message["message_id"]],
            "errors": [error] if error else [], "latency_ms": int((time.monotonic()-self.started)*1000), "trace_id": "native_"+uuid.uuid4().hex[:12]}
        if not error or terminal:
            self.journal["result"] = {k:v for k,v in result.items() if k not in {"model_requests", "native_calls", "tool_results"}}
        self.persist(completed=not error or terminal)
        if self.preset.allow_write and (not error or terminal):
            _stage_saved_dialogue(self.store, self.message, self.journal)
        if self.preset.allow_write and not self.session_superseded:
            clean, summary = conversation.history(self.store, self.user_id, self.message["chat_id"], bounded=True, config=self.config)
            from .context import turns
            self.session["messages"] = messages_to_dict([message for group in turns(clean)[-self.config.recent_turns:] for message in group]) if not error or terminal else self.session["messages"]
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
def run(message, *, store=None, config=None, intake_date=None, model=None):
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
            _stage_saved_dialogue(store, message, journal)
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
        deadline_token = PROCESSING_DEADLINE.set(rt.recovery_deadline)
        work_token = WORK_DEADLINE.set(rt.deadline)
        error = ""
        try:
            # Expired requests only reconcile original effects. They never gain
            # a fresh planning window or replay an uncommitted prepared action.
            if time.monotonic() >= rt.deadline:
                return rt.terminate("deadline_reached")
            for entry in rt.journal["calls"]:
                if "result" not in entry and not entry.get("terminated"):
                    rt.execute(entry)
            while True:
                if rt.try_tool_facts():
                    break
                messages = project_messages(messages_from_dict(rt.journal["messages"]))
                if isinstance(messages[-1], AIMessage) and not messages[-1].tool_calls and messages[-1].content:
                    break
                reserved = bool(rt.journal["model_steps"] and rt.journal["model_steps"][-1].get("status") == "reserved")
                remaining = config.max_model_calls - rt.journal["model_calls"] + int(reserved)
                if remaining <= 0:
                    return rt.terminate("model_call_limit")
                agent = create_agent(model=model, tools=[t for n,t in TOOL_REGISTRY.items() if n in preset.allowed_tools],
                    system_prompt=SYSTEM,
                    middleware=[ModelCallLimitMiddleware(run_limit=remaining, exit_behavior="end"),
                        ToolCallLimitMiddleware(run_limit=max(1, config.max_tool_calls-len(rt.journal["calls"])), exit_behavior="end"), rt],
                    interrupt_after=["tools"])
                final = agent.invoke({"messages": messages}, config={"max_concurrency": 1, "recursion_limit": 40})
                rt.journal["messages"] = messages_to_dict(final["messages"])
            result = rt.finish()
        except InputBudgetExceeded as exc:
            for entry in rt.journal["calls"]:
                if "result" not in entry:
                    entry["terminated"] = "input_budget_exceeded"
            rt.journal["budget_exceeded"] = exc.measurement
            result = rt.terminate("input_budget_exceeded")
        except DeadlineExceeded:
            result = rt.terminate("deadline_reached")
        except (ModelRequestFailed, TruncatedGeneration) as exc:
            reason = exc.reason if isinstance(exc, ModelRequestFailed) else "model_response_truncated"
            if rt.journal["model_steps"]:
                rt.journal["model_steps"][-1].update(status="failed", failure_reason=reason)
            result = rt.terminate(reason)
        except Exception as exc:
            if "result" in rt.journal: raise
            if isinstance(exc, (ValueError, KeyError, PermissionError)):
                result = rt.terminate("business_request_rejected")
            elif message.get("recovery_attempt", 0) >= 3 or time.monotonic() >= rt.deadline:
                result = rt.terminate("storage_recovery_required")
            else:
                result = rt.finish(f"{type(exc).__name__}:{str(exc)[:200]}")
        finally:
            WORK_DEADLINE.reset(work_token)
            PROCESSING_DEADLINE.reset(deadline_token)
            CURRENT_RUNTIME.reset(token)
        return result

__all__ = ["run", "session_lock"]


def _stage_saved_dialogue(store, message, journal):
    """Publish only the immutable final result; replay repairs a missed stage."""
    result = journal["result"]
    ident = result.get("dialogue_turn_id")
    if not ident:
        return
    messages = messages_from_dict(journal["messages"])
    if result.get("terminal") and result.get("termination_reason") == "input_budget_exceeded":
        messages = [HumanMessage(content="上一条请求因输入超限已终止，原文保留在收件及执行审计中。", id=ident), AIMessage(content="请缩小范围后重新发送。", additional_kwargs={"fact_scope": "dialogue"})]
    elif result.get("terminal"):
        # Keep locating references, but do not turn a terminal business receipt
        # into remembered ledger facts or an unanswered request to execute later.
        if messages and isinstance(messages[-1], AIMessage) and not messages[-1].tool_calls:
            messages.pop()
        messages.append(AIMessage(content="本次请求已结束，未执行的动作已终止；如需继续，请提出新的请求。",
                                  additional_kwargs={"fact_scope": "dialogue", "execution_terminated": True}))
    conversation.stage_turn(store, message["user_id"], message["chat_id"], ident, messages)


def mark_reply_presented(store, user_id, chat_id, result):
    """Called by the entry only after successful handoff of the saved reply."""
    if result.get("status") == "error" and not result.get("terminal"):
        return
    user_id = store.load_user_aliases().get(user_id, user_id)
    conversation.confirm_turn(store, user_id, chat_id, result.get("dialogue_turn_id"))
    if not result.get("proposal_id"):
        return
    session = store.load_session(user_id, chat_id)
    proposal = session.get("proposal")
    if proposal and proposal.get("proposal_id") == result["proposal_id"]:
        proposal["presented"] = True
        store.save_session(user_id, chat_id, session)
