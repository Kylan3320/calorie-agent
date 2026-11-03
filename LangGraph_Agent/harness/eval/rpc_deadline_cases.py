"""Real local HTTP proves Feishu and the production COS SDK obey RPC deadlines."""
from contextlib import contextmanager
from hashlib import md5
from http.server import BaseHTTPRequestHandler, HTTPServer
import base64
import gzip
import json
import logging
import threading
import time
from unittest.mock import patch

import requests
from qcloud_cos import CosConfig
from qcloud_cos.cos_exception import CosClientError

from agent.storage import cos_transport, feishu_client, model_transport
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


CHECKS = {name: value for name, value in globals().copy().items() if name.startswith("RP") and callable(value)}
