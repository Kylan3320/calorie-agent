"""Bind citations to retrieved text, and collect only evidence in a model request."""
from copy import deepcopy
import hashlib
import json
import re

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


def _bind(entry, namespace, legacy=None):
    value = deepcopy(entry)
    identity = json.dumps([namespace, value.get("entry_id"), value.get("answer"), value.get("source")], ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(identity.encode()).hexdigest()
    value.update(source_id="K" + str(int(digest[:24], 16)), evidence_id=digest)
    if legacy:
        value["legacy_source_id"] = legacy
    return value


def assign_evidence(rt, entries):
    """A saved native action replay has exactly the same citation identities."""
    namespace = [rt.user_id, rt.message["chat_id"], rt.action_id]
    index = rt.current["index"]
    return [_bind(entry, [*namespace, n], entry.get("legacy_source_id", entry.get("source_id", f"K{index * 100 + n}")))
            for n, entry in enumerate(entries, 1)]


def _knowledge_fact(message):
    # Extra metadata is not transmitted by wire_messages; never treat it as
    # visible evidence. Clean history carries the same JSON in assistant content.
    if not isinstance(message, (ToolMessage, AIMessage)) or not isinstance(message.content, str):
        return None
    if isinstance(message, AIMessage) and message.additional_kwargs.get("tool_evidence_origin") != "query_knowledge":
        return None
    try:
        value = json.loads(message.content)
    except (ValueError, TypeError):
        return None
    return value if isinstance(value, dict) and value.get("tool", getattr(message, "name", None)) == "query_knowledge" else None


def bind_legacy_messages(messages):
    """Rebind old local IDs from their owning result before exposing history.

    This reads only the provided messages, never a hidden execution or Store.
    An old assistant reference is rewritten only within an unambiguous turn.
    """
    result, aliases, turn = [], {}, "legacy"
    for message in messages:
        if isinstance(message, HumanMessage):
            turn = message.id or hashlib.sha256(str(message.content).encode()).hexdigest()
            aliases = {}
        fact = _knowledge_fact(message)
        if fact is not None:
            fact = deepcopy(fact)
            entries = fact.get("data", {}).get("entries", [])
            bound = []
            for n, entry in enumerate(entries, 1):
                value = entry
                old = entry.get("source_id")
                if not entry.get("evidence_id"):
                    value = _bind(entry, [turn, fact.get("action_id", fact.get("call_id", getattr(message, "tool_call_id", ""))), n], old)
                if old:
                    aliases.setdefault(old, set()).add(value["source_id"])
                bound.append(value)
            fact.setdefault("data", {})["entries"] = bound
            message = message.model_copy(update={"content": json.dumps(fact, ensure_ascii=False)})
        elif isinstance(message, AIMessage) and not message.tool_calls and isinstance(message.content, str):
            gaps = []
            def clause(match):
                text = match[0]
                missing = [ref for ref in re.findall(r"\[(K\d+)\]", text) if len(aliases.get(ref, set())) != 1]
                if missing:
                    gaps.extend(missing)
                    return ""
                return re.sub(r"\[(K\d+)\]", lambda ref: "[" + next(iter(aliases[ref[1]])) + "]", text)
            content = re.sub(r"(?:[^。！？!?\n]|[。！？!?](?=[”’」』\"']*\[K\d+\]))+[。！？!?\n]?", clause, message.content)
            if gaps:
                content += "\n旧引用" + "、".join(dict.fromkeys(gaps)) + "无法从所属可见工具结果唯一定位，相关结论需重新检索。"
            message = message.model_copy(update={"content": content})
        result.append(message)
    return result


def collect_visible_evidence(messages):
    """Return the exact original text/source pairs actually present in messages."""
    entries, seen = [], set()
    for message in messages:
        fact = _knowledge_fact(message)
        if fact is None:
            continue
        for entry in fact.get("data", {}).get("entries", []):
            if not all(isinstance(entry.get(key), str) and entry[key].strip() for key in ("source_id", "answer", "source")):
                continue
            key = (entry["source_id"], entry["answer"], entry["source"])
            if key not in seen:
                seen.add(key)
                entries.append(deepcopy(entry))
    return entries
