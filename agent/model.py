"""DeepSeek's native tool calling with one persisted budget per model step."""
from datetime import timezone
from email.utils import parsedate_to_datetime
import json
import math
import os
import random
import re
import time

import httpx
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatResult, ChatGeneration
from langchain_core.utils.function_calling import convert_to_openai_tool

from .storage.telemetry import span
from .storage.timeutil import PROCESSING_DEADLINE, WORK_DEADLINE, business_scope
from .storage.model_transport import create_client, deadline_scope
from .context import REQUEST_BUDGET, request_body, check_request, TruncatedGeneration, ModelRequestFailed

_CLIENT = create_client()
_RETRYABLE_TRANSPORT = (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError, TimeoutError)


def _remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:
        raise ModelRequestFailed("model_step_deadline", "model step wall-clock deadline reached")
    return value


def _retry_after(value, now):
    if value is None:
        return None
    value = value.strip()
    if re.fullmatch(r"\d+", value):
        try:
            seconds = float(value)
        except ValueError:
            return None
    else:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=timezone.utc)
            seconds = max(0, date.timestamp() - now)
        except (TypeError, ValueError, OverflowError):
            return None
    return seconds if math.isfinite(seconds) else None


def _diagnostic(exc):
    # Exception strings and provider error messages may contain URLs, credentials
    # or request content. Persist only a bounded error class and HTTP status.
    if isinstance(exc, httpx.HTTPStatusError):
        return "HTTPStatusError: status=" + str(exc.response.status_code)
    if isinstance(exc, TruncatedGeneration):
        return str(exc)
    if isinstance(exc, ModelRequestFailed):
        return exc.reason
    return type(exc).__name__ + ": model transport failed"


def _retryable(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code == 429 or 500 <= exc.response.status_code <= 599
    return isinstance(exc, _RETRYABLE_TRANSPORT)


def _parse_result(data, measurement, model, *, cached=False):
    try:
        choice = data["choices"][0]
        finish_reason = choice.get("finish_reason")
        if finish_reason in {"length", "content_filter", "insufficient_system_resource"}:
            raise TruncatedGeneration("incomplete model response: " + finish_reason)
        raw = choice["message"]
        calls = [{"id": call["id"], "name": call["function"]["name"],
                  "args": json.loads(call["function"]["arguments"]), "type": "tool_call"}
                 for call in raw.get("tool_calls", [])]
        usage = data.get("usage", {})
        return ChatResult(generations=[ChatGeneration(message=AIMessage(
            content=raw.get("content") or "", tool_calls=calls,
            additional_kwargs={"reasoning_content": raw["reasoning_content"]} if "reasoning_content" in raw else {},
            usage_metadata={"input_tokens": usage.get("prompt_tokens", 0), "output_tokens": usage.get("completion_tokens", 0), "total_tokens": usage.get("total_tokens", 0)},
            response_metadata={"model_name": data.get("model"), "model_provider": model._get_ls_params()["ls_provider"],
                               "usage": usage, "finish_reason": finish_reason, "budget": measurement,
                               "cached_response": cached}))])
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        raise ModelRequestFailed("model_response_invalid", "model response or tool arguments were rejected") from exc


class DeepSeekModel(BaseChatModel):
    model_name: str = "deepseek-v4-flash"
    request_timeout: float = 40
    purpose: str = "main"

    @property
    def _llm_type(self):
        return "deepseek_native"

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        if tool_choice is not None:
            kwargs["tool_choice"] = tool_choice
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    def _attempt(self, url, wire, headers, deadline, attempt, measurement):
        started = time.monotonic()
        attempt.update(started_at=time.time(), state="running")
        with span("model_http", model=self.model_name, **{key: value for key, value in measurement.items() if key != "attempts"}) as timing:
            try:
                with deadline_scope(deadline, attempt):
                    with _CLIENT.stream("POST", url, content=wire, headers=headers, timeout=_remaining(deadline)) as response:
                        attempt.update(response_headers_ms=round((time.monotonic() - started) * 1000, 3),
                                       status_code=response.status_code)
                        for name in ("x-request-id", "request-id", "x-ds-request-id", "x-deepseek-request-id"):
                            if response.headers.get(name):
                                attempt["provider_request_id"] = response.headers[name][:200]
                                break
                        if response.headers.get("retry-after") is not None:
                            attempt["retry_after_seconds"] = _retry_after(response.headers["retry-after"], time.time())
                        _remaining(deadline)
                        chunks = []
                        for chunk in response.iter_bytes():
                            _remaining(deadline)
                            if chunk.strip() and attempt["first_nonempty_response_byte_ms"] is None:
                                attempt["first_nonempty_response_byte_ms"] = round((time.monotonic() - started) * 1000, 3)
                            chunks.append(chunk)
                        _remaining(deadline)
                        # Preserve provider usage even if a rejected response
                        # happens to report billing. Never persist its error body.
                        try:
                            data = json.loads(b"".join(chunks))
                        except ValueError:
                            data = None
                        if isinstance(data, dict):
                            attempt["usage"] = data.get("usage", {})
                        response.raise_for_status()
                        try:
                            choice = data["choices"][0]
                            usage, finish = data.get("usage", {}), choice.get("finish_reason")
                        except (KeyError, TypeError, IndexError) as exc:
                            raise ModelRequestFailed("model_response_invalid", "model response was rejected") from exc
                        attempt.update(usage=usage, finish_reason=finish)
                        measurement.update(usage=usage, finish_reason=finish)
                        result = _parse_result(data, measurement, self)
                        _remaining(deadline)
                        attempt["state"] = "succeeded"
                        return data, result
            except Exception as exc:
                attempt.update(state="failed", error=_diagnostic(exc), retryable=_retryable(exc))
                raise
            finally:
                attempt.update(ended_at=time.time(), completed_ms=round((time.monotonic() - started) * 1000, 3))
                timing.update(attempt)

    @business_scope
    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        from .config import load_config
        scope = REQUEST_BUDGET.get() or {}
        config = scope.get("config") or load_config()
        purpose = scope.get("purpose", self.purpose)
        checkpoint = scope.get("checkpoint") or (lambda: None)
        entered, epoch = time.monotonic(), time.time()
        step = scope.get("step")
        if step is None:
            step = {"deadline_at": epoch + min(self.request_timeout, 20 if purpose in {"summary", "experience"} else 40), "attempts": []}
        deadline = scope.get("step_deadline")
        if deadline is None:
            deadline = entered + min(self.request_timeout, 20 if purpose in {"summary", "experience"} else 40,
                                     float(step["deadline_at"]) - epoch)
        deadline = min(deadline, PROCESSING_DEADLINE.get() or float("inf"), WORK_DEADLINE.get() or float("inf"))
        # A fully received, timely response was saved with its attempt. Parsing it
        # on recovery is not a new HTTP request and does not renew the step.
        cached = step.get("response")
        if cached is not None:
            if step.get("response_completed_at", float("inf")) > float(step["deadline_at"]):
                raise ModelRequestFailed("model_step_deadline", "saved model response arrived after its deadline")
            measurement = dict(scope.get("measurement") or {})
            measurement.update(purpose=purpose, usage=cached.get("usage", {}),
                               finish_reason=cached["choices"][0].get("finish_reason"), attempts=step.get("attempts", []))
            if scope.get("measurement") is not None:
                scope["measurement"].update(measurement)
            return _parse_result(cached, measurement, self, cached=True)
        _remaining(deadline)
        if "request_body" not in step:
            step["request_body"] = request_body(self.model_name, messages, kwargs.get("tools"),
                max_tokens=scope.get("max_tokens", config.summary_max_tokens if purpose in {"summary", "experience"} else config.output_tokens),
                tool_choice=kwargs.get("tool_choice"), response_format=kwargs.get("response_format"))
        body = step["request_body"]
        measurement = check_request(body, config, purpose)
        attempts = step.setdefault("attempts", [])
        measurement["attempts"] = attempts
        if scope.get("measurement") is not None:
            scope["measurement"].update(measurement)
        _remaining(deadline)
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not configured")
        url = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com").rstrip("/") + "/chat/completions"
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
        wire = json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        maximum = 1 if purpose in {"summary", "experience"} else 2
        while len(attempts) < maximum:
            _remaining(deadline)
            if attempts:
                previous = attempts[-1]
                if previous.get("retryable") is False:
                    raise ModelRequestFailed("model_http_rejected", previous.get("error", "model request rejected"))
                if "retry_not_before" not in previous:
                    delay = previous.get("retry_after_seconds")
                    delay = random.uniform(.5, 1) if delay is None else delay
                    previous.update(retry_delay_seconds=delay,
                                    retry_not_before=float(previous.get("ended_at", epoch)) + delay)
                    checkpoint()
                wait = max(0, previous["retry_not_before"] - time.time())
                if wait >= _remaining(deadline):
                    raise ModelRequestFailed("model_step_deadline", "model retry wait would exhaust its fixed deadline")
                if wait:
                    time.sleep(wait)
                _remaining(deadline)
            attempt = {"attempt": len(attempts) + 1, "purpose": purpose, "state": "reserved", "reserved_at": time.time(),
                       "response_headers_ms": None, "first_nonempty_response_byte_ms": None,
                       "completed_ms": None, "status_code": None, "provider_request_id": None, "usage": {}}
            attempts.append(attempt)
            # The decision, exact request and transport attempt share one
            # durable boundary. A process death still consumes the reservation.
            checkpoint()
            try:
                _remaining(deadline)  # A slow checkpoint cannot authorize a late POST.
                data, result = self._attempt(url, wire, headers, deadline, attempt, measurement)
                _remaining(deadline)
                step.update(response=data, response_completed_at=attempt["ended_at"])
            except Exception as exc:
                attempt.update(error=_diagnostic(exc), state="failed", retryable=_retryable(exc))
                attempt.setdefault("ended_at", time.time())
                checkpoint()
                if isinstance(exc, TruncatedGeneration):
                    raise
                if isinstance(exc, ModelRequestFailed):
                    raise
                if time.monotonic() >= deadline:
                    raise ModelRequestFailed("model_step_deadline", "model step wall-clock deadline reached") from exc
                if not _retryable(exc):
                    raise ModelRequestFailed("model_http_rejected", _diagnostic(exc)) from exc
                if len(attempts) >= maximum:
                    raise ModelRequestFailed("model_retry_exhausted", _diagnostic(exc)) from exc
            else:
                # The graph saves the raw reply and parsed calls together
                # before tools run. Standalone callers own their checkpoint.
                if not scope.get("checkpoint_success_in_graph"):
                    checkpoint()
                if scope.get("measurement") is not None:
                    scope["measurement"].update(measurement)
                return result
        raise ModelRequestFailed("model_retry_exhausted", "model HTTP attempt reservations exhausted")
