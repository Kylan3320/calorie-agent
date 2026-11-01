"""DeepSeek's native OpenAI-compatible tool-calling transport; no plan fallback."""
import json
import os
import urllib.request
from typing import Any
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatResult, ChatGeneration
from langchain_core.utils.function_calling import convert_to_openai_tool


class DeepSeekModel(BaseChatModel):
    model_name: str = "deepseek-v4-flash"
    request_timeout: float = 40

    @property
    def _llm_type(self):
        return "deepseek_native"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        wire = []
        for m in messages:
            role = {"human": "user", "ai": "assistant", "system": "system", "tool": "tool"}[m.type]
            row: dict[str, Any] = {"role": role, "content": m.content}
            if m.type == "tool":
                row["tool_call_id"] = m.tool_call_id
            if m.type == "ai" and m.tool_calls:
                row["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
                    "name": c["name"], "arguments": json.dumps(c["args"], ensure_ascii=False)}} for c in m.tool_calls]
            wire.append(row)
        body = {"model": self.model_name, "messages": wire, "temperature": 0,
                **{k: v for k,v in kwargs.items() if k in {"tools", "tool_choice"}}}
        if body.get("tools"):
            body["parallel_tool_calls"] = False
        url = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com").rstrip("/") + "/chat/completions"
        req = urllib.request.Request(url, json.dumps(body, ensure_ascii=False).encode(), {
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=self.request_timeout) as resp:
            data = json.load(resp)
        raw = data["choices"][0]["message"]
        calls = [{"id": c["id"], "name": c["function"]["name"],
                  "args": json.loads(c["function"]["arguments"]), "type": "tool_call"}
                 for c in raw.get("tool_calls", [])]
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content=raw.get("content") or "", tool_calls=calls,
            response_metadata={"model_name": data.get("model"), "usage": data.get("usage", {})}))])
