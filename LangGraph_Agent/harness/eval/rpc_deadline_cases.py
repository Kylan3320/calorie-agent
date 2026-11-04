"""Local HTTP proves runtime external calls obey their absolute RPC deadlines."""
from contextlib import contextmanager
from hashlib import md5
from http.server import BaseHTTPRequestHandler, HTTPServer
import base64
import asyncio
import gzip
import json
import logging
import os
import sys
import threading
import time
from types import ModuleType, SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx
import requests
from qcloud_cos import CosConfig
from qcloud_cos.cos_exception import CosClientError

from agent.storage import boohee_client, cos_transport, feishu_client, knowledge_retriever, live_client, model_transport, scf_dispatch
from agent.storage.telemetry import EVENTS
from agent.storage.timeutil import BUSINESS_IO, PROCESSING_DEADLINE, WORK_DEADLINE


@contextmanager
def local_server(behavior):
    released = threading.Event()
    requests_seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def respond(self, body=b"", status=200, headers=None):
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except OSError:
                pass
        def dispatch(self):
            body = self.rfile.read(int(self.headers.get("content-length", 0)))
            requests_seen.append({"method": self.command, "path": self.path, "headers": dict(self.headers), "body": body})
            behavior(self, len(requests_seen), released)
        do_GET = dispatch
        do_POST = dispatch
        do_PUT = dispatch
    server = HTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01))
    serving.start()
    try:
        yield server, requests_seen, released
    finally:
        released.set()
        server.shutdown()
        server.server_close()
        serving.join(2)
        assert not serving.is_alive()


@contextmanager
def sdk_objects(port):
    adapter = cos_transport._DeadlineAdapter()
    session = requests.Session()
    session.trust_env = False
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    config = CosConfig(Region="ap-shanghai", SecretId="offline-test-id", SecretKey="offline-test-secret",
                       Scheme="http", IP="127.0.0.1", Port=port, Timeout=8)
    try:
        with patch("qcloud_cos.CosConfig", return_value=config), patch.object(cos_transport, "_cos_session", return_value=session):
            objects = cos_transport.CosObjects(bucket="offline-production-1234567890", credentials=("offline-test-id", "offline-test-secret", None))
        assert objects.client._retry == 0 and objects.client._session is session
        yield objects
    finally:
        session.close()


def block(handler, released, mode):
    if mode == "headers":
        released.wait(2)
        return
    handler.send_response(200)
    handler.send_header("Content-Length", "100000")
    handler.end_headers()
    try:
        if mode == "body":
            handler.wfile.write(b"{\"code\":0,\"data\":")
            handler.wfile.flush()
            released.wait(2)
        else:
            while not released.is_set():
                handler.wfile.write(b" \n")
                handler.wfile.flush()
                released.wait(.012)
    except OSError:
        pass


def exercise(action, *, worker, expected, seconds=.2, minimum=None):
    outcomes, events = [], []
    def invoke():
        entered = time.monotonic()
        processing = PROCESSING_DEADLINE.set(entered + 3)
        work = WORK_DEADLINE.set(entered + seconds)
        business = BUSINESS_IO.set(True)
        observed = EVENTS.set(events)
        try:
            try:
                action()
            except expected as exc:
                outcomes.append((type(exc).__name__, time.monotonic() - entered))
            else:
                outcomes.append(("accepted", time.monotonic() - entered))
        finally:
            EVENTS.reset(observed)
            BUSINESS_IO.reset(business)
            WORK_DEADLINE.reset(work)
            PROCESSING_DEADLINE.reset(processing)
    if worker:
        thread = threading.Thread(target=invoke)
        thread.start()
        thread.join(2)
        assert not thread.is_alive(), "actual RPC exceeded its absolute deadline"
    else:
        invoke()
    assert outcomes and outcomes[0][0] != "accepted", outcomes
    assert (seconds * .7 if minimum is None else minimum) <= outcomes[0][1] < .7, outcomes
    return events, outcomes[0][1]


def RP01():
    """真实COS SDK保留签名、原body、MD5/禁止覆盖和流读取，XML解压仍兼容。"""
    body = b'{"revision":7}'
    compressed = gzip.compress(body)
    def behavior(handler, number, released):
        if "versioning" in handler.path:
            handler.respond(b'<VersioningConfiguration xmlns="http://s3.amazonaws.com/doc/2006-03-01/"/>')
        elif handler.command == "PUT":
            handler.respond(headers={"ETag": "offline-etag"})
        elif "prefix=" in handler.path:
            xml = b'<ListBucketResult><IsTruncated>false</IsTruncated><Contents><Key>test/state.json</Key></Contents></ListBucketResult>'
            handler.respond(gzip.compress(xml), headers={"Content-Encoding": "gzip"})
        else:
            handler.respond(compressed, headers={"Content-Encoding": "gzip"})
    with local_server(behavior) as (server, seen, _), sdk_objects(server.server_port) as objects:
        assert objects.create("test/state.json", body)
        assert objects.get("test/state.json") == compressed
        assert objects.list("test/") == ["test/state.json"]
    put = next(request for request in seen if request["method"] == "PUT")
    headers = {name.lower(): value for name, value in put["headers"].items()}
    assert put["body"] == body and put["path"] == "/test/state.json"
    assert headers["x-cos-forbid-overwrite"] == "true"
    assert headers["content-md5"] == base64.b64encode(md5(body).digest()).decode()
    assert "q-sign-algorithm=sha1" in headers["authorization"]
    assert "q-ak=offline-test-id" in headers["authorization"]


def RP02():
    """真实Feishu HTTP主线程/工作线程：阻塞头、停滞body及持续保活均按总截止退出。"""
    for worker in (False, True):
        for mode in ("headers", "body", "keepalive"):
            with local_server(lambda handler, number, released: block(handler, released, mode)) as (server, seen, _):
                client = model_transport.create_client()
                try:
                    with patch.object(feishu_client, "_CLIENT", client):
                        events, _ = exercise(lambda: feishu_client.http_json_get(f"http://127.0.0.1:{server.server_port}/test"),
                                             worker=worker, expected=(TimeoutError, RuntimeError))
                    assert len(seen) == 1 and len(events) == 1
                finally:
                    client.close()


def RP03():
    """真实COS SDK主线程/工作线程：阻塞头、停滞body及持续保活均按总截止退出。"""
    logger = logging.getLogger("qcloud_cos.cos_client")
    with patch.object(logger, "disabled", True):
        for worker in (False, True):
            for mode in ("headers", "body", "keepalive"):
                def behavior(handler, number, released):
                    if "versioning" in handler.path:
                        handler.respond(b"<VersioningConfiguration/>")
                    else:
                        block(handler, released, mode)
                with local_server(behavior) as (server, seen, _), sdk_objects(server.server_port) as objects:
                    events, _ = exercise(lambda: objects.get("test/state.json"), worker=worker, expected=CosClientError)
                    assert len(seen) == 2 and any(event["kind"] == "cos_get" and event["outcome"] == "error" for event in events)


def RP04():
    """Feishu两尝试与退避共用一次逻辑RPC截止，退避耗尽不发迟到第二请求。"""
    for worker in (False, True):
        for backoff in (.05, .3):
            def behavior(handler, number, released):
                if number == 1:
                    handler.respond(status=503)
                else:
                    block(handler, released, "keepalive")
            with local_server(behavior) as (server, seen, _):
                client = model_transport.create_client()
                try:
                    with patch.object(feishu_client, "_CLIENT", client), patch.object(feishu_client, "_RETRY_SLEEP_SECONDS", backoff):
                        events, _ = exercise(lambda: feishu_client.http_json_post(f"http://127.0.0.1:{server.server_port}/test", {"fixture": True}),
                                             worker=worker, expected=(TimeoutError, RuntimeError), minimum=0 if backoff >= .2 else None)
                    expected = 2 if backoff < .2 else 1
                    assert len(seen) == len(events) == expected
                finally:
                    client.close()


def RP05():
    """COS写入已发但超时仍抛未知提交故障，随后按原键核对可取原字节。"""
    body, committed = b'{"committed":true}', {}
    def behavior(handler, number, released):
        if "versioning" in handler.path:
            handler.respond(b"<VersioningConfiguration/>")
        elif handler.command == "PUT":
            committed[handler.path] = body
            released.wait(2)
        else:
            handler.respond(committed.get(handler.path, b""))
    logger = logging.getLogger("qcloud_cos.cos_client")
    with local_server(behavior) as (server, seen, released), sdk_objects(server.server_port) as objects, patch.object(logger, "disabled", True):
        exercise(lambda: objects.create("test/unknown.json", body), worker=True, expected=CosClientError)
        assert committed == {"/test/unknown.json": body}
        released.set()
        assert objects.get("test/unknown.json") == body
        assert [request["method"] for request in seen] == ["GET", "PUT", "GET"]


def RP06():
    """薄荷真实保活按逻辑RPC截止退出，退避耗尽不重发，正常JSON及注入接口保持。"""
    for mode in ("keepalive", "retry"):
        def behavior(handler, number, released):
            if mode == "retry":
                handler.respond(status=503)
            else:
                block(handler, released, "keepalive")
        with local_server(behavior) as (server, seen, _), model_transport.create_client() as client, \
                patch.object(boohee_client, "_CLIENT", client):
            exercise(lambda: boohee_client._default_http_get(f"http://127.0.0.1:{server.server_port}/food", {"X-Api-Key": "offline-key"}),
                     worker=True, expected=(TimeoutError, httpx.TimeoutException), minimum=0 if mode == "retry" else None)
            assert len(seen) == 1
            assert seen[0]["headers"]["X-Api-Key"] == "offline-key"
    payload = {"code": 0, "data": {"foods": [{"code": "fixture", "name": "夹具食物", "carbohydrate": 10, "protein": 2, "fat": 1}]}}
    with local_server(lambda handler, number, released: handler.respond(json.dumps(payload).encode())) as (server, _, _), \
            model_transport.create_client() as client, patch.object(boohee_client, "_CLIENT", client):
        assert boohee_client._default_http_get(f"http://127.0.0.1:{server.server_port}/food", {}) == payload
    result = boohee_client.search_food("rpc-fixture-food", transport=lambda url, headers: payload)
    assert result[0]["boohee_id"] == "fixture" and result[0]["protein_per_100g"] == 2


def RP07():
    """SCF保留TC3签名及Event载荷，真实保活继承预算，独立请求不超过2秒。"""
    calls = []
    credentials = {"X-Scf-Secret-Id": "offline-id", "X-Scf-Secret-Key": "offline-secret", "X-Scf-Session-Token": "offline-session"}
    def route(url, **kwargs):
        calls.append((url, kwargs, model_transport.remaining()))
        assert kwargs["timeout"] <= 2 and model_transport.remaining() <= 2
        return client.post(f"http://127.0.0.1:{server.server_port}/invoke", **kwargs)
    with local_server(lambda handler, number, released: block(handler, released, "keepalive")) as (server, seen, _), \
            model_transport.create_client() as client, patch.object(scf_dispatch, "_CLIENT", SimpleNamespace(post=route)):
        exercise(lambda: scf_dispatch.dispatch_event({"message_id": "same-event"}, credentials),
                 worker=True, expected=(TimeoutError, httpx.TimeoutException))
        assert len(seen) == len(calls) == 1
    def accepted(url, **kwargs):
        assert 1.8 < model_transport.remaining() <= 2
        assert kwargs["headers"]["Authorization"].startswith("TC3-HMAC-SHA256 Credential=offline-id/")
        assert kwargs["headers"]["X-TC-Token"] == "offline-session"
        body = json.loads(kwargs["content"])
        assert body["InvocationType"] == "Event" and json.loads(body["ClientContext"])["message_id"] == "same-event"
        return httpx.Response(200, json={"Response": {"RequestId": "offline-request", "Result": {"InvokeResult": 0}}}, request=httpx.Request("POST", url))
    with patch.object(scf_dispatch, "_CLIENT", SimpleNamespace(post=accepted)):
        assert scf_dispatch.dispatch_event({"message_id": "same-event"}, credentials) == "offline-request"


def RP08():
    """真实RAG回调的线程在async取消后仍受物理截止，初始化共用预算且原文输出不变。"""
    lightrag = ModuleType("lightrag")
    utils = ModuleType("lightrag.utils")
    lightrag.QueryParam = lambda **kwargs: SimpleNamespace(**kwargs)
    utils.EmbeddingFunc = lambda **kwargs: SimpleNamespace(**kwargs)
    mode = "keyword"
    cancelled = threading.Event()
    stopped = threading.Event()
    class LightRAG:
        def __init__(self, **kwargs):
            self.options = kwargs
            assert model_transport.remaining() <= 85
        async def initialize_storages(self):
            await asyncio.sleep(.4 if mode == "initialize" else .025)
        async def aquery_data(self, query, param):
            assert query == "怎样搭配" and param.mode == "mix" and param.top_k == 3
            if mode == "healthy":
                return {"status": "success", "data": {"chunks": [{"chunk_id": "original-chunk", "content": "原文：按自己的日程搭配。\n来源：离线资料"}]}}
            callback = self.options["llm_model_func"]("extract") if mode == "keyword" else self.options["embedding_func"].func(["chunk"])
            try:
                # Cancellation happens before the inherited .25s I/O deadline.
                # asyncio.run must still drain the executor without a long tail.
                await asyncio.wait_for(callback, timeout=.08)
            except asyncio.TimeoutError:
                cancelled.set()
                raise
            raise AssertionError("blocking RAG callback returned successfully")
    lightrag.LightRAG = LightRAG
    with patch.dict(sys.modules, {"lightrag": lightrag, "lightrag.utils": utils}), \
            patch.object(knowledge_retriever, "_writable_index_dir", return_value="offline-index"), \
            patch.dict(os.environ, {"DEEPSEEK_API_KEY": "offline-key", "SILICONFLOW_API_KEY": "offline-key", "RAG_STUB_EMBEDDING": "0"}):
        for mode in ("keyword", "embedding"):
            cancelled.clear()
            stopped.clear()
            def route(url, **kwargs):
                try:
                    return client.post(f"http://127.0.0.1:{server.server_port}" + urlsplit(url).path, **kwargs)
                finally:
                    stopped.set()
            with local_server(lambda handler, number, released: block(handler, released, "keepalive")) as (server, seen, _), \
                    model_transport.create_client() as client, \
                    patch.object(knowledge_retriever, "_HTTP_CLIENT", SimpleNamespace(post=route)), \
                    patch.object(knowledge_retriever, "_LIGHTRAG_INSTANCE", None), patch.object(knowledge_retriever, "_LIGHTRAG_READY", False):
                _, elapsed = exercise(lambda: knowledge_retriever.retrieve_lightrag("怎样搭配"), worker=True,
                                      expected=(TimeoutError, asyncio.TimeoutError), seconds=.25)
                assert cancelled.is_set() and stopped.is_set() and elapsed >= .2
                assert len(seen) == 1 and seen[0]["headers"]["Authorization"] == "Bearer offline-key"
                payload = json.loads(seen[0]["body"])
                assert ("messages" in payload) if mode == "keyword" else payload["input"] == ["chunk"]
        mode = "initialize"
        with patch.object(knowledge_retriever, "_LIGHTRAG_INSTANCE", None), patch.object(knowledge_retriever, "_LIGHTRAG_READY", False):
            exercise(lambda: knowledge_retriever.retrieve_lightrag("怎样搭配"), worker=True, expected=(TimeoutError, asyncio.TimeoutError), seconds=.12)
            assert not knowledge_retriever._LIGHTRAG_READY
        mode = "healthy"
        with patch.object(knowledge_retriever, "_LIGHTRAG_INSTANCE", None), patch.object(knowledge_retriever, "_LIGHTRAG_READY", False):
            result = knowledge_retriever.retrieve_lightrag("怎样搭配")
            assert result["status"] == "ok" and result["evidence"] == "原文：按自己的日程搭配。\n来源：离线资料"
            assert result["entries"][0]["source"] == "" and result["entries"][0]["entry_id"] == "original-chunk"


def RP09():
    """LiveClient从受理前计总预算、限制每次请求，超时不重发且显式恢复沿用原键。"""
    message = {"message_id": "original-key", "user_id": "user", "chat_id": "cli:rpc", "text": "原请求"}
    environment = {"CALORIE_SERVICE_TOKEN": "offline-token", "CALORIE_SERVICE_AUTH": "bearer"}
    with local_server(lambda handler, number, released: block(handler, released, "keepalive")) as (server, seen, _), \
            model_transport.create_client() as http, patch.object(live_client, "_CLIENT", http), \
            patch.dict(os.environ, {**environment, "CALORIE_SERVICE_URL": f"http://127.0.0.1:{server.server_port}"}):
        client = live_client.LiveClient()
        def unknown_acceptance():
            try:
                client.submit(message, source="cli", timeout=.2)
            except TimeoutError as exc:
                assert "acceptance is unknown" in str(exc) and "original-key" in str(exc)
                raise
        exercise(unknown_acceptance, worker=True, expected=TimeoutError)
        assert len(seen) == 1 and json.loads(seen[0]["body"])["message"] == message
    def behavior(handler, number, released):
        if handler.command == "POST":
            released.wait(.06)
            handler.respond(b'{"receipt_id":"same-receipt"}')
        else:
            released.wait(.06)
            payload = {"status": "running"} if number == 2 else {
                "result": {"status": "ok", "reply": "已处理原请求。"}, "delivery": {"uuid": "same-uuid"}}
            handler.respond(json.dumps(payload).encode())
    with local_server(behavior) as (server, seen, _), \
            model_transport.create_client() as http, patch.object(live_client, "_CLIENT", http), \
            patch.dict(os.environ, {**environment, "CALORIE_SERVICE_URL": f"http://127.0.0.1:{server.server_port}"}):
        client = live_client.LiveClient()
        def accepted_timeout():
            try:
                client.submit(message, source="cli", timeout=.2)
            except TimeoutError as exc:
                assert "remains accepted" in str(exc) and "original-key" in str(exc)
                raise
        exercise(accepted_timeout, worker=True, expected=TimeoutError)
        assert [request["method"] for request in seen] == ["POST", "GET"]
        resumed = client.submit(message, source="cli", timeout=.5)
        assert resumed["reply"] == "已处理原请求。" and resumed["delivery_reference"]["message"] == message
        assert resumed["delivery_reference"]["receipt_id"] == "same-receipt"
        assert [request["method"] for request in seen] == ["POST", "GET", "POST", "GET"]
        assert [json.loads(request["body"])["message"] for request in seen if request["method"] == "POST"] == [message, message]
        assert all(request["headers"]["Authorization"] == "Bearer offline-token" for request in seen)
    def capped(request):
        assert 9 < model_transport.remaining() <= 10
        assert all(9 < value <= 10 for value in request.extensions["timeout"].values())
        return httpx.Response(200, json={"status": "accepted"})
    with httpx.Client(transport=httpx.MockTransport(capped)) as http, \
            patch.dict(os.environ, {**environment, "CALORIE_SERVICE_URL": "https://offline.invalid"}):
        assert live_client.LiveClient(client=http).submit_reminder("offline-time") == {"status": "accepted"}


CHECKS = {name: value for name, value in globals().copy().items() if name.startswith("RP") and callable(value)}
