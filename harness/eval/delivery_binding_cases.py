"""Offline wire checks for recovery delivery and content-bound presentation."""
from copy import deepcopy
import hashlib
import json
import os
from unittest.mock import patch

import httpx

from agent.storage import feishu_client
from agent.storage.live_client import LiveClient


def DB01():
    """文本更新沿用已投 message_id，HTTP 重试及再次覆盖均发送相同正文。"""
    requests = []

    def handle(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, json={"code": 1})
        return httpx.Response(200, json={"code": 0, "data": {"message_id": "om-existing"}})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
            patch.object(feishu_client, "_CLIENT", client), \
            patch.object(feishu_client, "get_tenant_access_token", return_value="offline-token"), \
            patch.object(feishu_client, "feishu_api_base", return_value="https://offline.invalid/open-apis"), \
            patch.object(feishu_client.time, "sleep"):
        for _ in range(2):
            assert feishu_client.update_text("om-existing", "已查明：这餐120千卡。 ")["code"] == 0

    assert len(requests) == 3
    assert {request.method for request in requests} == {"PUT"}
    assert {str(request.url) for request in requests} == {
        "https://offline.invalid/open-apis/im/v1/messages/om-existing"}
    assert len({request.content for request in requests}) == 1
    for request in requests:
        assert request.headers["Authorization"] == "Bearer offline-token"
        body = json.loads(request.content)
        assert set(body) == {"msg_type", "content"} and body["msg_type"] == "text"
        assert isinstance(body["content"], str)
        assert json.loads(body["content"]) == {"text": "已查明：这餐120千卡。 "}


def DB02():
    """应用错误、HTTP 错误和网络重试耗尽均抛错，不返回成功更新。"""
    for failure, attempts in (("application", 1), ("http", 1), ("network", 2)):
        requests = []

        def handle(request):
            requests.append(request)
            if failure == "network":
                raise httpx.ReadTimeout("injected update timeout", request=request)
            if failure == "http":
                return httpx.Response(403, json={"code": 99991672})
            return httpx.Response(200, json={"code": 230001, "msg": "injected update rejection"})

        with httpx.Client(transport=httpx.MockTransport(handle)) as client, \
                patch.object(feishu_client, "_CLIENT", client), \
                patch.object(feishu_client, "get_tenant_access_token", return_value="offline-token"), \
                patch.object(feishu_client, "feishu_api_base", return_value="https://offline.invalid/open-apis"), \
                patch.object(feishu_client.time, "sleep"):
            try:
                feishu_client.update_text("om-existing", "原任务最终正文")
            except RuntimeError as exc:
                assert "Feishu" in str(exc)
                if failure == "application":
                    assert "230001" in str(exc)
                elif failure == "http":
                    assert "403" in str(exc)
                else:
                    assert "ReadTimeout" in str(exc)
            else:
                raise AssertionError(f"{failure} update failure reported success")
        assert len(requests) == attempts
        assert {request.method for request in requests} == {"PUT"}


def DB03():
    """同任务进度与最终正文透传各自摘要，展示事件分离且不提交业务收件。"""
    message = {"message_id": "same-task", "user_id": "user", "chat_id": "dsh:test", "text": "记录米饭"}
    replies = ("仍在核对原提交。", "已记录，这餐120千卡。")
    digests = [hashlib.sha256(reply.encode("utf-8")).hexdigest() for reply in replies]
    states = [{"result": {"status": "ok", "reply": reply},
               "delivery": {"uuid": "same-reply-uuid", "result_digest": digest}}
              for reply, digest in zip(replies, digests)]
    requests = []

    def handle(request):
        body = json.loads(request.content) if request.content else None
        requests.append((request.method, request.url.path, body))
        if request.url.path == "/internal/intake":
            assert body == {"message": message, "source": "dsh"}
            return httpx.Response(200, json={"receipt_id": "same-receipt"})
        if request.url.path == "/internal/result":
            assert request.url.params["receipt_id"] == "same-receipt"
            return httpx.Response(200, json=states.pop(0))
        assert request.url.path == "/internal/presented"
        return httpx.Response(200, json={"status": "accepted"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http, patch.dict(os.environ, {
            "CALORIE_SERVICE_URL": "https://offline.invalid", "CALORIE_SERVICE_TOKEN": "offline-token",
            "CALORIE_SERVICE_AUTH": "bearer"}):
        client = LiveClient(client=http)
        results = [client.submit(deepcopy(message), source="dsh") for _ in replies]
        before = len(requests)
        for result, digest in zip(results, digests):
            reference = result["delivery_reference"]
            assert reference["result_digest"] == digest
            assert reference["message"] == message
            assert client.presented(reference) == {"status": "accepted"}

    presentation = requests[before:]
    assert len(presentation) == 2
    assert all(method == "POST" and path == "/internal/presented" for method, path, _ in presentation)
    assert [body["message"]["message_id"] for _, _, body in presentation] == [
        "same-task:presented:" + digest[:16] for digest in digests]
    for (_, _, body), digest in zip(presentation, digests):
        assert body["source"] == "dsh" and body["message"]["text"] == ""
        assert body["reference"]["result_digest"] == digest
        assert body["reference"]["receipt_id"] == "same-receipt"
        assert body["reference"]["uuid"] == "same-reply-uuid"


def DB04():
    """旧结果及展示参考缺摘要时沿用原展示事件 ID，不臆造正文摘要。"""
    message = {"message_id": "legacy-task", "user_id": "user", "chat_id": "cli:test", "text": "旧请求"}
    requests = []

    def handle(request):
        body = json.loads(request.content) if request.content else None
        requests.append((request.url.path, body))
        if request.url.path == "/internal/intake":
            return httpx.Response(200, json={"receipt_id": "legacy-receipt"})
        if request.url.path == "/internal/result":
            return httpx.Response(200, json={"result": {"status": "ok", "reply": "旧正文"},
                                             "delivery": {"uuid": "legacy-uuid"}})
        assert request.url.path == "/internal/presented"
        return httpx.Response(200, json={"status": "accepted"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as http, patch.dict(os.environ, {
            "CALORIE_SERVICE_URL": "https://offline.invalid", "CALORIE_SERVICE_TOKEN": "offline-token",
            "CALORIE_SERVICE_AUTH": "bearer"}):
        client = LiveClient(client=http)
        result = client.submit(message, source="cli")
        assert "result_digest" not in result["delivery_reference"]
        client.presented(result["delivery_reference"])

    body = requests[-1][1]
    assert body["message"]["message_id"] == "legacy-task:presented"
    assert "result_digest" not in body["reference"]


CHECKS = {"DB01": DB01, "DB02": DB02, "DB03": DB03, "DB04": DB04}
