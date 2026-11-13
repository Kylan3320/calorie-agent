"""Model HTTP attempts use one persisted step and a real wall-clock deadline."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import format_datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import signal
import threading
import time
from unittest.mock import patch

import httpx
from langchain_core.messages import HumanMessage

from agent.config import Config
from agent.context import REQUEST_BUDGET, ModelRequestFailed, TruncatedGeneration
import agent.model as model_module
from agent.storage import model_transport
from agent.storage.telemetry import EVENTS

CONFIG = Config(preset="shadow_fakewrite", planner="deepseek", model="deepseek-v4-flash", allowed_tools=(), timezone_offset_hours=8)
PAYLOAD = {"model": "deepseek-v4-flash", "choices": [{"finish_reason": "stop", "message": {"content": "fixture"}}],
           "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15,
                     "prompt_cache_hit_tokens": 8, "prompt_cache_miss_tokens": 4,
                     "completion_tokens_details": {"reasoning_tokens": 2}}}


class Clock:
    def __init__(self):
        self.now = 100.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def time(self):
        return 1_800_000_000 + self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


@contextmanager
def scenario(handler, *, seconds=5, purpose="main", checkpoint=None, step=None):
    clock = Clock()
    step = step if step is not None else {"deadline_at": clock.time() + seconds, "attempts": []}
    saved, events, requests, measurement = [], [], [], {}
    def save():
        saved.append(deepcopy(step))
        if checkpoint:
            checkpoint(clock, step)
    def handle(request):
        requests.append(request.content)
        return handler(request, clock, len(requests))
    scope = {"config": CONFIG, "purpose": purpose, "measurement": measurement,
             "step": step, "step_deadline": clock.monotonic() + seconds, "checkpoint": save}
    token = REQUEST_BUDGET.set(scope)
    observed = EVENTS.set(events)
    try:
        with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
             patch.object(model_module, "_CLIENT", client), \
             patch.object(model_module, "check_request", side_effect=lambda body, config, purpose: {"purpose": purpose, "input_tokens": 1}), \
             patch.dict("os.environ", {"DEEPSEEK_API_KEY": "fixture"}), \
             patch.object(model_module.time, "monotonic", clock.monotonic), \
             patch.object(model_module.time, "time", clock.time), \
             patch.object(model_module.time, "sleep", clock.sleep), \
             patch.object(model_module.random, "uniform", return_value=.75):
            yield model_module.DeepSeekModel(), step, requests, events, saved, clock, measurement
    finally:
        EVENTS.reset(observed)
        REQUEST_BUDGET.reset(token)


def failure(model, reason):
    try:
        model.invoke([HumanMessage(content="fixture")])
    except ModelRequestFailed as exc:
        assert exc.reason == reason, (exc.reason, str(exc))
    else:
        raise AssertionError("expected terminal model failure: " + reason)


def MT01():
    """503后成功为同一步两次HTTP，完整请求复用且逐次usage/请求ID不丢。"""
    def reply(request, clock, number):
        clock.now += .01
        return httpx.Response(503 if number == 1 else 200, json=PAYLOAD,
                              headers={"x-request-id": "request-" + str(number)})
    with scenario(reply) as (model, step, requests, events, saved, clock, measurement):
        response = model.invoke([HumanMessage(content="original")])
        assert response.content == "fixture"
        assert len(requests) == len(step["attempts"]) == len(events) == 2
        assert requests[0] == requests[1]
        assert len(clock.sleeps) == 1 and .74 < clock.sleeps[0] < .76
        assert saved[0]["request_body"]["messages"][-1]["content"] == "original"
        assert [attempt["state"] for attempt in saved[0]["attempts"]] == ["reserved"]
        assert [event["status_code"] for event in events] == [503, 200]
        assert [event["provider_request_id"] for event in events] == ["request-1", "request-2"]
        assert all(event["kind"] == "model_http" and event["purpose"] == "main" for event in events)
        assert all(attempt["usage"] == PAYLOAD["usage"] for attempt in step["attempts"])
        assert all(attempt["response_headers_ms"] is not None and attempt["first_nonempty_response_byte_ms"] is not None for attempt in step["attempts"])
        assert len(measurement["attempts"]) == 2
        # A successful HTTP checkpoint survives death before graph saves its AIMessage.
        clock.now += 10
        cached = model.invoke([HumanMessage(content="different projection must not be sent")])
        assert cached.response_metadata["cached_response"] is True and len(requests) == 2
        assert step["response_completed_at"] <= step["deadline_at"]


def MT02():
    """400/401/402/422、截断及参数JSON拒绝不自动重试；摘要也无内部重试。"""
    for status in (400, 401, 402, 403, 404, 422):
        with scenario(lambda request, clock, number: httpx.Response(status, json={"error": {"message": "private fixture"}})) as (model, step, requests, events, *_):
            failure(model, "model_http_rejected")
            assert len(requests) == len(step["attempts"]) == len(events) == 1
            assert "private fixture" not in json.dumps(step["attempts"])
    for finish in ("length", "content_filter", "insufficient_system_resource"):
        payload = deepcopy(PAYLOAD)
        payload["choices"][0]["finish_reason"] = finish
        with scenario(lambda request, clock, number: httpx.Response(200, json=payload)) as (model, step, requests, events, *_):
            try:
                model.invoke([HumanMessage(content="fixture")])
            except TruncatedGeneration:
                pass
            else:
                raise AssertionError("truncation accepted")
            assert len(requests) == len(events) == 1 and "response" not in step
            assert step["attempts"][0]["usage"] == PAYLOAD["usage"]
    payload = deepcopy(PAYLOAD)
    payload["choices"][0]["message"]["tool_calls"] = [{"id": "call", "function": {"name": "query_today", "arguments": "{"}}]
    with scenario(lambda request, clock, number: httpx.Response(200, json=payload)) as (model, step, requests, events, *_):
        failure(model, "model_response_invalid")
        assert len(requests) == len(events) == 1 and events[0]["state"] == "failed"
    with scenario(lambda request, clock, number: httpx.Response(503), purpose="summary") as (model, step, requests, events, *_):
        failure(model, "model_retry_exhausted")
        assert len(requests) == len(events) == 1 and events[0]["purpose"] == "summary"


def MT03():
    """合法Retry-After优先，耗尽原截止则不发第二次；无效值使用原退避。"""
    for retry_after in ("100", format_datetime(datetime.fromtimestamp(1_800_000_110, timezone.utc), usegmt=True)):
        with scenario(lambda request, clock, number: httpx.Response(429, headers={"retry-after": retry_after}), seconds=2) as (model, step, requests, events, saved, clock, _):
            failure(model, "model_step_deadline")
            assert len(requests) == len(events) == 1 and not clock.sleeps
            assert step["attempts"][0]["retry_delay_seconds"] > 2
    for retry_after, expected in (("0", 0), ("1", 1), ("invalid", .75)):
        def reply(request, clock, number):
            return httpx.Response(503, headers={"retry-after": retry_after}) if number == 1 else httpx.Response(200, json=PAYLOAD)
        with scenario(reply) as (model, step, requests, events, saved, clock, _):
            model.invoke([HumanMessage(content="fixture")])
            assert len(requests) == 2 and step["attempts"][0]["retry_delay_seconds"] == expected
            assert sum(clock.sleeps) == expected


def MT04():
    """迟到响应、请求准备及前置checkpoint超截止均拒绝，不能发起迟到HTTP。"""
    def late(request, clock, number):
        clock.now += 3
        return httpx.Response(200, json=PAYLOAD)
    with scenario(late, seconds=2) as (model, step, requests, events, *_):
        failure(model, "model_step_deadline")
        assert len(requests) == len(events) == 1 and "response" not in step
        assert events[0]["first_nonempty_response_byte_ms"] is None
    def expire(clock, step):
        if step["attempts"][0]["state"] == "reserved":
            assert step["request_body"]
            clock.now += 3
    with scenario(lambda request, clock, number: httpx.Response(200, json=PAYLOAD), seconds=2, checkpoint=expire) as (model, step, requests, events, *_):
        failure(model, "model_step_deadline")
        assert not requests and not events
        assert step["attempts"][0]["state"] == "failed"


def MT05():
    """连接/超时最多两HTTP，崩溃预留计入次数，恢复复用已保存body。"""
    for exception in (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
        def broken(request, clock, number):
            raise exception("private fixture credential")
        with scenario(broken) as (model, step, requests, events, *_):
            failure(model, "model_retry_exhausted")
            assert len(requests) == len(events) == len(step["attempts"]) == 2
            assert "private fixture credential" not in json.dumps(step["attempts"])
    step = {"deadline_at": 1_800_000_105, "attempts": [{"attempt": 1, "state": "reserved"}],
            "request_body": {"model": "deepseek-v4-flash", "messages": [{"role": "user", "content": "saved wire"}], "temperature": 0, "max_tokens": 8192}}
    with scenario(lambda request, clock, number: httpx.Response(200, json=PAYLOAD), step=step) as (model, step, requests, events, *_):
        model.invoke([HumanMessage(content="must not replace saved wire")])
        assert len(requests) == 1 and json.loads(requests[0])["messages"][0]["content"] == "saved wire"
        assert len(step["attempts"]) == 2
    exhausted = {"deadline_at": 1_800_000_105, "attempts": [{"state": "reserved"}, {"state": "reserved"}]}
    with scenario(lambda request, clock, number: httpx.Response(200, json=PAYLOAD), step=exhausted) as (model, step, requests, events, *_):
        failure(model, "model_retry_exhausted")
        assert not requests and not events


def _real_blocking(mode, *, worker):
    release = threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", 0)))
            if mode == "headers":
                release.wait(2)
                return
            self.send_response(200)
            self.send_header("Content-Length", "100000")
            self.end_headers()
            while not release.is_set():
                try:
                    self.wfile.write(b" \n")
                    self.wfile.flush()
                except OSError:
                    return
                release.wait(.015)
    server = HTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01))
    serving.start()
    events, outcomes, steps = [], [], []
    client = model_transport.create_client()
    resolver = client._transport.backend.resolver
    def invoke():
        entered = time.monotonic()
        step = {"deadline_at": time.time() + .3, "attempts": []}
        token = REQUEST_BUDGET.set({"config": CONFIG, "purpose": "main", "measurement": {}, "step": step,
                                    "step_deadline": entered + .3, "checkpoint": lambda: None})
        event_token = EVENTS.set(events)
        try:
            try:
                model_module.DeepSeekModel().invoke([HumanMessage(content="fixture")])
            except ModelRequestFailed as exc:
                outcomes.append((exc.reason, time.monotonic() - entered))
            else:
                outcomes.append(("accepted", time.monotonic() - entered))
        finally:
            steps.append(step)
            EVENTS.reset(event_token)
            REQUEST_BUDGET.reset(token)
    thread = None
    try:
        with patch.object(model_module, "_CLIENT", client), \
             patch.object(model_module, "check_request", return_value={"purpose": "main", "input_tokens": 1}), \
             patch.dict("os.environ", {"DEEPSEEK_API_KEY": "fixture", "DEEPSEEK_API_BASE": f"http://localhost:{server.server_port}"}):
            if worker:
                thread = threading.Thread(target=invoke)
                thread.start()
                thread.join(2)
                assert not thread.is_alive(), "absolute model deadline failed to interrupt transport"
            else:
                invoke()
        assert outcomes and outcomes[0][0] == "model_step_deadline", outcomes
        assert .2 <= outcomes[0][1] < .8, outcomes
        assert len(events) == len(steps[0]["attempts"]) == 1
        assert steps[0]["attempts"][0]["first_nonempty_response_byte_ms"] is None
        assert "response" not in steps[0]
        if mode == "keepalive":
            assert steps[0]["attempts"][0]["response_headers_ms"] is not None
        if worker or not hasattr(signal, "setitimer"):
            assert steps[0]["attempts"][0]["dns_helper_startup_ms"] > 0
    finally:
        release.set()
        client.close()
        server.shutdown()
        server.server_close()
        serving.join(2)
        if thread is not None:
            thread.join(2)
        assert not serving.is_alive()
        assert resolver.process is None


def MT06():
    """真实HTTP工作线程：持续空白保活和无响应头均被同一总截止中断。"""
    _real_blocking("keepalive", worker=True)
    _real_blocking("headers", worker=True)


def MT07():
    """真实HTTP主线程短截止（Linux含SIGALRM）：保活及阻塞响应头不延长预算。"""
    _real_blocking("keepalive", worker=False)
    _real_blocking("headers", worker=False)


def MT08():
    """线程DNS辅助按需复用，阻塞查询被原截止kill/wait，退出不遗留进程。"""
    resolver = model_transport._Resolver()
    observation = {}
    try:
        with model_transport.deadline_scope(time.monotonic() + 2, observation):
            assert resolver.resolve("localhost", 443)
            process = resolver.process
            assert resolver.resolve("localhost", 443) and resolver.process is process
        assert observation["dns_helper_startup_ms"] > 0
    finally:
        resolver.close()
    assert process.poll() is not None and resolver.process is None
    blocked = model_transport._RESOLVER_CODE.replace("host, port = json.loads(line)", "host, port = json.loads(line); __import__('time').sleep(5)")
    with patch.object(model_transport, "_RESOLVER_CODE", blocked):
        entered = time.monotonic()
        try:
            with model_transport.deadline_scope(entered + .2):
                resolver.resolve("localhost", 443)
        except (TimeoutError, httpx.TimeoutException):
            pass
        else:
            raise AssertionError("blocked DNS ignored absolute deadline")
        assert time.monotonic() - entered < .8
        assert resolver.process is None


CHECKS = {name: value for name, value in globals().copy().items() if name.startswith("MT") and callable(value)}
