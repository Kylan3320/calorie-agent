"""DeepSeek's native OpenAI-compatible tool-calling transport; no plan fallback."""
import json
import os
import httpx
from .storage.telemetry import span
from .storage.timeutil import remaining_timeout, business_scope
from typing import Any
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatResult, ChatGeneration
from langchain_core.utils.function_calling import convert_to_openai_tool


_CLIENT = httpx.Client()

class DeepSeekModel(BaseChatModel):
    model_name: str = "deepseek-v4-flash"
    request_timeout: float = 40

    @property
    def _llm_type(self):
        return "deepseek_native"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    @business_scope
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
            if m.type == "ai" and m.additional_kwargs.get("reasoning_content") is not None:
                row["reasoning_content"] = m.additional_kwargs["reasoning_content"]
            if m.type == "ai" and m.tool_calls:
                row["tool_calls"] = [{"id": c["id"], "type": "function", "function": {
                    "name": c["name"], "arguments": json.dumps(c["args"], ensure_ascii=False)}} for c in m.tool_calls]
            wire.append(row)
        body = {"model": self.model_name, "messages": wire, "temperature": 0,
                **{k: v for k,v in kwargs.items() if k in {"tools", "tool_choice"}}}
        if body.get("tools"):
            body["parallel_tool_calls"] = False
        url = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com").rstrip("/") + "/chat/completions"
        with span("model_http", model=self.model_name, purpose=("main" if body.get("tools") else "summary")) as timing:
            resp = _CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {api_key}"},
                                timeout=remaining_timeout(self.request_timeout))
            resp.raise_for_status()
            data = resp.json()
            timing["usage"] = data.get("usage",{})
        raw = data["choices"][0]["message"]
        calls = [{"id": c["id"], "name": c["function"]["name"],
                  "args": json.loads(c["function"]["arguments"]), "type": "tool_call"}
                 for c in raw.get("tool_calls", [])]
        usage = data.get("usage", {})
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content=raw.get("content") or "", tool_calls=calls,
            additional_kwargs={"reasoning_content": raw["reasoning_content"]} if "reasoning_content" in raw else {},
            usage_metadata={"input_tokens": usage.get("prompt_tokens",0), "output_tokens": usage.get("completion_tokens",0), "total_tokens": usage.get("total_tokens",0)},
            response_metadata={"model_name": data.get("model"), "model_provider": self._get_ls_params()["ls_provider"], "usage": usage}))])
