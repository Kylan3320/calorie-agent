"""Clean historical turns and a local measurement of the final HTTP request.

The official tokenizer counts the wire representation, not a provider-private
chat template. Local measurements and provider usage remain separate evidence.
"""
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path
import hashlib
import json

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage, messages_from_dict, messages_to_dict

REQUEST_BUDGET = ContextVar("request_budget", default=None)


class InputBudgetExceeded(ValueError):
    def __init__(self, measurement):
        self.measurement = measurement
        super().__init__("input_budget_exceeded")


class TruncatedGeneration(RuntimeError):
    pass


class ModelRequestFailed(RuntimeError):
    """A model step has ended; Pipeline must not re-plan it on recovery."""
    def __init__(self, reason, diagnostic=""):
        self.reason = reason
        super().__init__(diagnostic or reason)


class StateCheckpointFailed(RuntimeError):
    pass


@lru_cache(maxsize=1)
def tokenizer():
    from tokenizers import Tokenizer
    root = Path(__file__).with_name("encoding")
    version = json.loads((root / "version.json").read_text())
    body = (root / "deepseek-v4.json").read_bytes()
    if hashlib.sha256(body).hexdigest() != version["sha256"]:
        raise ValueError("DeepSeek tokenizer identity mismatch")
    return Tokenizer.from_str(body.decode())


def tokens(value):
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return len(tokenizer().encode(text, add_special_tokens=False).ids)


def wire_messages(messages):
    wire = []
    for message in messages:
        row = {"role": {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}[message.type], "content": message.content}
        if isinstance(message, ToolMessage):
            row["tool_call_id"] = message.tool_call_id
        if isinstance(message, AIMessage):
            if "reasoning_content" in message.additional_kwargs:
                row["reasoning_content"] = message.additional_kwargs["reasoning_content"]
            if message.tool_calls:
                row["tool_calls"] = [{"id": call["id"], "type": "function", "function": {
                    "name": call["name"], "arguments": json.dumps(call["args"], ensure_ascii=False)}} for call in message.tool_calls]
        wire.append(row)
    return wire


def request_body(model, messages, tools=None, *, max_tokens=8192, tool_choice=None):
    body = {"model": model, "messages": wire_messages(messages), "temperature": 0, "max_tokens": max_tokens}
    if tools:
        body.update(tools=tools, parallel_tool_calls=False)
    if tool_choice is not None:
        body["tool_choice"] = tool_choice
    return body


def measure_request(body, config, purpose="main"):
    parts = {key: 0 for key in ("system", "state", "summary", "history", "current", "reasoning", "calls", "tools")}
    current_start = max((i for i, row in enumerate(body["messages"]) if row["role"] == "user"), default=0)
    for i, row in enumerate(body["messages"]):
        content = row.get("content", "")
        if row["role"] == "system":
            key = "state" if str(content).startswith("可信会话事实：") else ("summary" if str(content).startswith("话题背景摘要") else "system")
        else:
            key = "current" if i >= current_start else "history"
        parts[key] += tokens(content)
        parts["reasoning"] += tokens(row.get("reasoning_content", ""))
        if row.get("tool_calls"):
            parts["calls"] += tokens(row["tool_calls"])
    parts["tools"] = tokens(body["tools"]) if body.get("tools") else 0
    total = max(tokens(body), sum(parts.values()))
    parts["protocol"] = total - sum(parts.values())
    reserve = body["max_tokens"]
    hard_limit = config.context_window - reserve - config.protocol_margin_tokens
    return {"purpose": purpose, "encoding": "deepseek-v4-official-20261103", "wire_encoding": "json-wire-v1",
            "parts": parts, "input_tokens": total, "input_target": config.summary_input_tokens if purpose == "summary" else config.input_target_tokens,
            "output_reserve": reserve, "protocol_margin": config.protocol_margin_tokens, "hard_input_limit": hard_limit}


def check_request(body, config, purpose="main"):
    result = measure_request(body, config, purpose)
    if result["input_tokens"] > result["hard_input_limit"]:
        raise InputBudgetExceeded(result)
    return result


def clean_messages(messages):
    """No historical native calls: their protocol-only reasoning is unnecessary.

    Business values are removed by history_messages before flattening; only
    explicit historical references and necessary knowledge evidence remain.
    """
    from .replies import history_messages
    clean = []
    for message in history_messages(messages):
        if message.additional_kwargs.get("lc_source") == "summarization":
            continue
        if isinstance(message, ToolMessage):
            extra = {"fact_scope": "historical" if message.name != "query_knowledge" else "knowledge"}
            if message.name == "query_knowledge":
                extra["tool_evidence_origin"] = "query_knowledge"
            clean.append(AIMessage(content=message.content, additional_kwargs=extra))
        elif isinstance(message, AIMessage) and message.tool_calls:
            continue
        else:
            extra = {k: v for k, v in message.additional_kwargs.items() if k != "reasoning_content"}
            clean.append(message.model_copy(update={"additional_kwargs": extra}))
    return clean


def turns(messages):
    result = []
    for message in messages:
        if isinstance(message, HumanMessage) and message.additional_kwargs.get("lc_source") != "summarization":
            result.append([])
        if result:
            result[-1].append(message)
    return result


def select_messages(history, summary, current, system, state, tools, config, model):
    groups = turns(history)
    kept = list(groups)
    summary_messages = [SystemMessage(content="话题背景摘要（仅过去对话意图，不是当前事实或授权）：" + summary["text"])] if summary.get("text") else []
    def assemble():
        return [*summary_messages, *[message for group in kept for message in group], *current]
    def measurement():
        return measure_request(request_body(model, [SystemMessage(content=system), SystemMessage(content="可信会话事实：" + json.dumps(state, ensure_ascii=False)), *assemble()], tools, max_tokens=config.output_tokens), config)
    result = measurement()
    removed = 0
    while len(kept) > config.recent_turns and result["input_tokens"] > config.input_target_tokens:
        kept.pop(0)
        removed += 1
        result = measurement()
    result["removed_turns"] = removed
    result["over_target_reason"] = "required_recent_turns_current_exchange_or_state" if result["input_tokens"] > config.input_target_tokens else None
    if result["input_tokens"] > result["hard_input_limit"]:
        raise InputBudgetExceeded(result)
    return assemble(), result
