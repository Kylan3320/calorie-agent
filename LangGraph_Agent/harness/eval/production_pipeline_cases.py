"""Production ingress and adapters with injected object/HTTP transports."""
from dataclasses import replace
import io
import json
import os
import time
from unittest.mock import patch
from agent.storage import feishu_client


def forbidden_bitable(*args, **kwargs):
    raise AssertionError("synchronous Bitable HTTP on the message path")


class Objects:
    def __init__(self):
        self.data = {}
    def get(self, key):
        return self.data.get(key)
    def create(self, key, body):
        if key in self.data:
            return False
        self.data[key] = body
        return True
    def list(self, prefix, marker='', limit=None):
        keys = sorted(key for key in self.data if key.startswith(prefix) and key > marker)
        return keys if limit is None else keys[:limit]


def fixture():
    from agent.storage.cos_store import CosStore
    from agent.storage.execution_pipeline import Pipeline
    from harness.eval.personal_cases import A, B, CONFIG
    objects = Objects()
    store = CosStore(objects, "candidate/pipeline/")
    store.cache_food(A)
    store.cache_food(B)
    return objects, Pipeline(objects, "candidate/pipeline/"), replace(CONFIG, preset="live")


def event(receipt):
    return {"receipt_id": receipt["receipt_id"], "ingress_revision": receipt["ingress_revision"],
            **{k: receipt["message"][k] for k in ("message_id", "user_id", "chat_id")}}


def message(mid, text="今天多少热量？", chat="test"):
    return {"message_id": mid, "user_id": "user", "chat_id": chat, "text": text}


def assert_cam_request(request):
    """Independent wire verification of function-URL TC3 request inputs."""
    import hashlib
    import hmac
    from datetime import datetime, timezone
    authorization = request.headers["Authorization"]
    assert authorization.startswith("TC3-HMAC-SHA256 Credential=function-role/")
    assert request.headers["X-Calorie-Service-Token"] == "fixture-service-token"
    assert request.headers["X-Scf-Cam-Token"] == "function-session"
    assert request.headers["X-Scf-Cam-Uin"] == "100000000001"
    timestamp = int(request.headers["X-Scf-Cam-Timestamp"])
    day = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
    scope = day + "/scf/tc3_request"
    path = request.url.raw_path.split(b"?", 1)[0].decode("ascii")
    query = "&".join(sorted(request.url.query.decode("ascii").split("&")))
    canonical = "\n".join([request.method, path, query,
        "content-type:" + request.headers["Content-Type"] + "\nhost:" + request.headers["Host"] + "\n",
        "content-type;host", hashlib.sha256(request.content).hexdigest()])
    to_sign = "TC3-HMAC-SHA256\n" + str(timestamp) + "\n" + scope + "\n" + hashlib.sha256(canonical.encode()).hexdigest()
    key = ("TC3" + "function-key").encode()
    for part in (day, "scf", "tc3_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    expected = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    assert authorization == f"TC3-HMAC-SHA256 Credential=function-role/{scope}, SignedHeaders=content-type;host, Signature={expected}"


@patch.multiple(feishu_client, http_json_get=forbidden_bitable, http_json_post=forbidden_bitable, http_json_put=forbidden_bitable)
def P01():
    """生产受理序号、原日期、迟到重试、持久尝试和交付丢状态恢复。"""
    from agent import run
    from agent.storage.cos_store import CosStore
    from harness.eval.personal_cases import NativeFake, call
    from langchain_core.messages import AIMessage
    objects, pipeline, config = fixture()
    first = pipeline.accept(message("order-first", "记录夹具A200克"), received_at=time.time()-20)
    second = pipeline.accept(message("order-second"), received_at=time.time()-10)
    assert pipeline.accept(message("order-first", "改变的文本"))["sequence"] == first["sequence"]
    assert pipeline.accept(message("order-first"))["received_at"] == first["received_at"]
    executed, sent = [], []
    def execute(msg, **kwargs):
        executed.append(msg["message_id"])
        steps = [call("record_meal", {"items": [{"food_query": "夹具A", "grams": 200}]}), AIMessage(content="夹具A已记录，这餐120千卡。 ")] if msg["message_id"] == "order-first" else [call("query_history", {"start_date": first["intake_date"], "end_date": "2026-12-01"}), AIMessage(content="合计120千卡。")]
        return run(msg, model=NativeFake(responses=steps), **kwargs)
    def send(chat, text, uuid_key):
        sent.append((chat, text, uuid_key))
        return {"data": {"message_id": "sent-" + str(len(sent))}}
    with patch("agent.config.load_config", return_value=config):
        assert pipeline.worker(event(second), execute=execute, send=send)["status"] == "completed"
        assert executed == ["order-first", "order-second"]
        assert pipeline.worker(event(first), execute=execute, send=send)["duplicate"]
        assert len(sent) == 2
        # The original receipt date and fixed deadline survive out-of-order wakes.
        store = CosStore(objects, pipeline.prefix, readonly=True)
        rows = store.intake_items("user")
        assert len(rows) == 1 and rows[0]["date"] == first["intake_date"] and rows[0]["nutrition"]["kcal"] == 120
        third = pipeline.accept(message("after-delivery-state-loss"))
        writer = CosStore(objects, pipeline.prefix)
        save = writer.save_document
        class Death(BaseException):
            pass
        def lost_state(kind, uid, scope, payload):
            if kind == "receipt_state" and scope == third["receipt_id"] and payload.get("status") == "completed":
                raise Death()
            return save(kind, uid, scope, payload)
        def controlled(msg, **kwargs):
            return run(msg, model=NativeFake(responses=[call("query_today", {}), AIMessage(content="今天合计0千卡。")]), **kwargs)
        with patch.object(pipeline, "store_factory", lambda: writer), patch.object(writer, "save_document", lost_state):
            try:
                pipeline.worker(event(third), execute=controlled, send=send)
            except Death:
                pass
            else:
                raise AssertionError("delivery interruption was not injected")
        original = sent[-1]
        assert pipeline.worker(event(third), execute=lambda *a, **k: (_ for _ in ()).throw(AssertionError("saved reply must replay")), send=send)["status"] == "completed"
        assert sent[-1] == original
        assert pipeline.result(third["receipt_id"])["attempts"] == 2
        # Completed legacy ingress state migrates before replaying its original
        # Feishu ID. No execution, delivery or automatic retry budget is used.
        from agent.storage.execution_pipeline import Pipeline
        from agent.storage.bitable_adapter import stable_uuid
        migrated = CosStore(objects, "candidate/migrated/")
        old_message = message("legacy-delivered")
        task = {"pending_id": stable_uuid("legacy-pending"), "intent": "message_received",
                "status": "completed", "source_message_id": old_message["message_id"],
                "user_id": "user", "chat_id": "test", "payload": {"message": old_message, "delivery": {"presented": True}}}
        migrated.import_baseline({"baseline_id": "migration-fixture", "pending": [task]})
        after_migration = Pipeline(objects, "candidate/migrated/")
        old_receipt = after_migration.accept(old_message)
        before_sends, before_runs = len(sent), len(executed)
        state = after_migration.worker(event(old_receipt), execute=execute, send=send)
        assert state["duplicate"] and state["migrated"] and state["attempts"] == 0
        assert len(sent) == before_sends and len(executed) == before_runs
        legacy_only = CosStore(objects, "candidate/deleted-only/")
        legacy_only.import_baseline({"baseline_id": "deleted-without-receipt", "intake": [{
            "record_id": "original-feishu-deleted", "user_id": "user", "date": "2026-10-31",
            "message_id": "legacy-deleted:agent:1", "raw_text": "记录夹具A", "meal_type": "lunch",
            "food_id": "A", "food_name": "夹具A", "grams": 100,
            "nutrition": {"carbs": 10, "protein": 2, "fat": 1, "kcal": 60},
            "snapshot": {}, "status": "deleted", "created_at": 1000}]})
        deleted_pipeline = Pipeline(objects, "candidate/deleted-only/")
        deleted_receipt = deleted_pipeline.accept(message("legacy-deleted", "记录夹具A100克"))
        deleted_state = deleted_pipeline.worker(event(deleted_receipt), execute=execute, send=send)
        assert deleted_state["duplicate"] and deleted_state["migrated"] and deleted_state["attempts"] == 0
        assert len(sent) == before_sends and len(executed) == before_runs
        assert not legacy_only.intake_items("user")
        assert deleted_pipeline.result(deleted_receipt["receipt_id"])["status"] == "completed"
        saved_deleted = CosStore(objects, deleted_pipeline.prefix, readonly=True).intake_items("user", include_deleted=True)
        assert len(saved_deleted) == 1 and saved_deleted[0]["record_id"] == "original-feishu-deleted" and saved_deleted[0]["status"] == "deleted"
        unfinished = pipeline.accept(message("partial-manual", "记录并设置偏好"))
        partial = {"status": "error", "reply": "夹具A已记录，偏好尚未完成。", "errors": ["ConnectionError:temporary"], "tool_results": [], "native_calls": []}
        def partial_execute(msg, store, **kwargs):
            store.write_intake({**msg, "message_id": msg["message_id"] + ":agent:1"}, [{"matched_name": "夹具A", "grams": 200, "nutrition": {"carbs": 20, "protein": 4, "fat": 2, "kcal": 120}}])
            return partial
        for attempt in (1, 2):
            try:
                pipeline.worker(event(unfinished), execute=partial_execute, send=send)
            except RuntimeError:
                pass
            else:
                raise AssertionError("temporary failure did not retain the original attempt")
            assert pipeline.result(unfinished["receipt_id"])["attempts"] == attempt
        pipeline.worker(event(unfinished), execute=partial_execute, send=send)
        delivered = pipeline.result(unfinished["receipt_id"])
        sent_count = len(sent)
        def recover_partial(msg, store, **kwargs):
            assert msg["message_id"] == "partial-manual"
            store.save_document("user_settings", "user", "", {"preferences": {"cooking": "少油"}})
            return {"status": "ok", "reply": "偏好已保存。", "errors": []}
        edits=[]
        recovered = pipeline.worker({**event(unfinished), "manual_recovery": True}, execute=recover_partial, send=send, update_send=lambda mid,text: edits.append((mid,text)) or {"code":0})
        assert recovered["work_status"] == "completed" and recovered["recovery_result"]["status"] == "ok"
        assert recovered["result"]["reply"] == "偏好已保存。"
        assert recovered["delivery"]["uuid"] == delivered["delivery"]["uuid"]
        assert len(edits)==1 and edits[0][1]=="偏好已保存。"
        assert len(sent) == sent_count
        # Timer execution is a durable intake too. Maintenance can wake a failed
        # tick days later, preserving its original local minute and delivery ID.
        from agent.storage.reminders import reminder_intake
        writer = CosStore(objects, pipeline.prefix)
        writer.save_document("user_settings", "user", "", {"feishu_chat_id": "reminder-chat", "reminders": {
            "lunch-rule": {"enabled": True, "time": "12:00", "meal_type": "lunch", "weekdays": list(range(7))}}})
        timer_message, reference = reminder_intake("2026-10-31T04:00:00Z")
        tick = pipeline.accept(timer_message, source="timer", kind="reminder", reference=reference)
        assert tick["intake_date"] == "2026-10-31"
        timer_sends = []
        def timer_send(chat, text, uuid_key):
            timer_sends.append((chat, text, uuid_key))
            if len(timer_sends) == 1:
                raise ConnectionError("temporary reminder send failure")
            return {"data": {"message_id": "timer-delivered"}}
        try:
            pipeline.worker(event(tick), execute=lambda *a, **k: (_ for _ in ()).throw(AssertionError("timer must not call model")), send=timer_send)
        except ConnectionError:
            pass
        else:
            raise AssertionError("reminder delivery failure was not injected")
        woken = []
        pipeline.recover(wake=lambda receipt: woken.append(event(receipt)))
        assert any(item["receipt_id"] == tick["receipt_id"] for item in woken)
        timer_state = pipeline.worker(event(tick), send=timer_send)
        assert timer_state["attempts"] == 2 and timer_state["result"]["scheduled_minute"].startswith("2026-10-31T12:00")
        assert timer_sends[0] == timer_sends[1] and timer_sends[1][0] == "reminder-chat"
        assert pipeline.worker(event(tick), send=timer_send)["duplicate"] and len(timer_sends) == 2
        failed_message, failed_reference = reminder_intake("2026-11-01T04:00:00Z")
        failed_tick = pipeline.accept(failed_message, source="timer", kind="reminder", reference=failed_reference)
        failed_sends = []
        def always_unavailable(chat, text, uuid_key):
            failed_sends.append(uuid_key)
            raise ConnectionError("persistent reminder send failure")
        for _ in range(2):
            try:
                pipeline.worker(event(failed_tick), send=always_unavailable)
            except ConnectionError:
                pass
            else:
                raise AssertionError("retryable reminder failure was not retained")
        exhausted = pipeline.worker(event(failed_tick), send=always_unavailable)
        assert exhausted["status"] == "completed" and exhausted["work_status"] == "needs_attention" and exhausted["attempts"] == 3
        assert len(failed_sends) == 3 and len(set(failed_sends)) == 1
        # The user closes the failed 12:00 rule and has an unrelated 12:01 rule.
        # The old tick must not hold all subsequent rules/dates behind it.
        close_rule = pipeline.accept(message("close-failed-reminder", "关闭午餐提醒，12:01提醒晚餐。"))
        def close_rule_execute(msg, **kwargs):
            return run(msg, model=NativeFake(responses=[
                call("update_user_settings", {"kind": "reminder", "values": {"rule_id": "lunch-rule", "enabled": False}}),
                call("update_user_settings", {"kind": "reminder", "values": {"rule_id": "later-rule", "enabled": True, "time": "12:01", "meal_type": "dinner"}}, "c2"),
                AIMessage(content="已关闭午餐提醒，并设置晚餐提醒。")]), **kwargs)
        pipeline.worker(event(close_rule), execute=close_rule_execute, send=send)
        later_message, later_reference = reminder_intake("2026-11-01T04:01:00Z")
        later_tick = pipeline.accept(later_message, source="timer", kind="reminder", reference=later_reference)
        later_sends = []
        def later_send(chat, text, uuid_key):
            later_sends.append((chat, text, uuid_key))
            return {"data": {"message_id": "later-rule-delivered"}}
        later_state = pipeline.worker(event(later_tick), send=later_send)
        assert later_state["status"] == "completed" and later_state["result"]["sent"] == 1 and len(later_sends) == 1
        assert pipeline.result(failed_tick["receipt_id"])["attempts"] == 3
        closed_recovery = pipeline.worker({**event(failed_tick), "manual_recovery": True}, send=later_send)
        assert closed_recovery["work_status"] == "completed" and closed_recovery["recovery_result"]["sent"] == 0
        assert closed_recovery["attempts"] == 3 and len(later_sends) == 1


def P02():
    """真实HTTP入口仅在持久收件与异步承接后ACK，失败可补查且身份绑定。"""
    import app
    import httpx
    from agent.storage import scf_dispatch
    objects, pipeline, config = fixture()
    queued, responses, denied = [], [], [False]
    def invoke(request):
        body = json.loads(request.content)
        assert body["InvocationType"] == "Event"
        assert "fixture-key" not in request.content.decode()
        if denied[0]:
            return httpx.Response(200, json={"Response": {"Error": {"Code": "Denied"}}})
        queued.append(json.loads(body["ClientContext"]))
        return httpx.Response(200, json={"Response": {"RequestId": "queued"}})
    def receive(mid):
        payload = {"event": {"sender": {"sender_id": {"open_id": "user"}}, "message": {
            "message_id": mid, "chat_id": "test", "message_type": "text", "content": json.dumps({"text": "今日热量"})}}}
        raw = json.dumps(payload).encode()
        handler = object.__new__(app.Handler)
        handler.path = "/webhook/feishu/events"
        handler.rfile = io.BytesIO(raw)
        handler.headers = {"Content-Length": str(len(raw)), "X-Scf-Secret-Id": "fixture-id", "X-Scf-Secret-Key": "fixture-key", "X-Scf-Session-Token": "fixture-token"}
        handler.do_POST()
    def ack(self, body, status=200):
        if status == 200 and body.get("status") == "accepted":
            assert queued and pipeline.inbox(readonly=True).get("receipt:" + body["receipt_id"])
        responses.append((body, status))
    with httpx.Client(transport=httpx.MockTransport(invoke)) as client, patch.object(scf_dispatch, "_CLIENT", client), patch.multiple(app, configured_pipeline=lambda headers=None: pipeline, load_config=lambda: config, _verify_rollover_style_token=lambda _: True), patch.object(app.Handler, "_send_json", ack):
        health = app.ingress_handler({"httpMethod": "GET", "path": "/health"})
        assert health["statusCode"] == 200 and json.loads(health["body"])["status"] == "ok"
        challenge = app.ingress_handler({"httpMethod": "POST", "path": "/webhook/feishu/events", "body": json.dumps({"challenge": "fixture-challenge"})})
        assert challenge["statusCode"] == 200 and json.loads(challenge["body"]) == {"challenge": "fixture-challenge"}
        receive("http-ok")
        assert responses[-1][1] == 200 and not pipeline.result(queued[-1]["receipt_id"])
        denied[0] = True
        receive("http-rejected-handoff")
        assert responses[-1][1] == 503
        denied[0] = False
        recovery = pipeline.recover(wake=lambda receipt: queued.append(event(receipt)))
        assert recovery["woken"] == 2
        try:
            pipeline.worker({**queued[0], "user_id": "other"})
        except ValueError:
            pass
        else:
            raise AssertionError("wrong receipt identity was accepted")
        # The platform timer only submits through HTTP and returns after the
        # durable handoff; it cannot wait for its own serial message consumer.
        from agent.storage import reminders, live_client
        def timer_http(request):
            if os.getenv("CALORIE_SERVICE_AUTH") == "cam":
                assert_cam_request(request)
            response = app.ingress_handler({"httpMethod": request.method, "path": request.url.path,
                "body": request.content.decode(), "headers": {**dict(request.headers),
                    "X-Scf-Secret-Id": "fixture-id", "X-Scf-Secret-Key": "fixture-key", "X-Scf-Session-Token": "fixture-token"}})
            return httpx.Response(response["statusCode"], json=json.loads(response["body"]))
        with patch.dict("os.environ", {"CALORIE_SERVICE_URL": "https://fixture", "CALORIE_SERVICE_TOKEN": "fixture-service-token", "CALORIE_SERVICE_AUTH": "bearer"}), patch.object(reminders, "load_config", lambda: config), httpx.Client(transport=httpx.MockTransport(timer_http)) as timer_transport:
            adapter = live_client.LiveClient(client=timer_transport)
            with patch.object(live_client, "LiveClient", lambda: adapter):
                accepted_tick = reminders.handler({"Time": "2026-10-31T04:00:00Z"})
            assert accepted_tick["status"] == "accepted"
            tick = pipeline.inbox(readonly=True).get("receipt:" + accepted_tick["receipt_id"])
            assert tick["source"] == "timer" and tick["kind"] == "reminder"
            assert tick["reference"]["scheduled_time"] == "2026-10-31T04:00:00+00:00"
            assert tick["message"]["user_id"] == "system" and tick["message"]["chat_id"] == "system:reminders"
            assert not pipeline.result(tick["receipt_id"])
            assert queued[-1]["receipt_id"] == tick["receipt_id"]
            cam_env = {"CALORIE_SERVICE_AUTH": "cam", "CALORIE_CLOUD_UIN": "100000000001",
                       "TENCENTCLOUD_SECRET_ID": "function-role", "TENCENTCLOUD_SECRET_KEY": "function-key",
                       "TENCENTCLOUD_SESSION_TOKEN": "function-session"}
            with patch.dict("os.environ", cam_env), patch.object(live_client, "LiveClient", lambda: adapter):
                cam_tick = reminders.handler({"Time": "2026-10-31T04:00:00Z"})
            assert cam_tick["status"] == "accepted" and cam_tick["receipt_id"] == accepted_tick["receipt_id"]
    # A warm SCF instance must replace its COS client when temporary role
    # credentials rotate, for both injected HTTP headers and Event environment.
    from agent.storage import execution_pipeline as pipeline_module, cos_transport
    created = []
    def transport_objects(*, credentials):
        created.append(credentials)
        return Objects()
    group_a = ("role-a", "key-a", "token-a")
    group_b = ("role-b", "key-b", "token-b")
    headers_a = dict(zip(("X-Scf-Secret-Id", "X-Scf-Secret-Key", "X-Scf-Session-Token"), group_a))
    headers_b = dict(zip(("X-Scf-Secret-Id", "X-Scf-Secret-Key", "X-Scf-Session-Token"), group_b))
    credential_env = {"COS_PREFIX": "candidate/credential-refresh/",
                      "TENCENTCLOUD_SECRET_ID": group_a[0], "TENCENTCLOUD_SECRET_KEY": group_a[1],
                      "TENCENTCLOUD_SESSION_TOKEN": group_a[2]}
    with patch.dict("os.environ", credential_env), patch.object(cos_transport, "CosObjects", transport_objects), patch.multiple(pipeline_module, _PIPELINE=None, _PIPELINE_KEY=None):
        initial = pipeline_module.configured_pipeline(headers_a)
        assert pipeline_module.configured_pipeline(headers_a) is initial
        assert pipeline_module.configured_pipeline() is initial
        refreshed = pipeline_module.configured_pipeline(headers_b)
        assert refreshed is not initial and created == [group_a, group_b]
        with patch.dict("os.environ", {"TENCENTCLOUD_SESSION_TOKEN": "token-a-rotated"}):
            rotated = pipeline_module.configured_pipeline()
            assert rotated is not refreshed and created[-1] == (*group_a[:2], "token-a-rotated")
        assert pipeline_module.configured_pipeline(headers_a) is not initial
        assert len(created) == 4 and pipeline_module._PIPELINE_KEY[0] == group_a


def P03():
    """CLI/DSH live经真实HTTP客户端统一提交，展示回报后提案才可确认。"""
    import cli
    import console.server as console
    import httpx
    from agent import run
    from agent.storage.live_client import LiveClient
    from agent.storage.cos_store import CosStore
    from harness.eval.personal_cases import NativeFake, call
    from langchain_core.messages import AIMessage
    objects, pipeline, config = fixture()
    sources = []
    def execute(msg, **kwargs):
        confirm = msg["text"] == "好的"
        args = {"kind": "targets", "mode": "confirm" if confirm else "propose", "values": {"kcal": 1900}}
        return run(msg, model=NativeFake(responses=[call("update_user_settings", args), AIMessage(content="这个目标可以先试试。")]), **kwargs)
    def http(request):
        if os.getenv("CALORIE_SERVICE_AUTH") == "cam":
            assert_cam_request(request)
        path = request.url.path
        if path == "/internal/intake":
            body = json.loads(request.content)
            sources.append(body["source"])
            accepted = pipeline.accept(body["message"], source=body["source"])
            pipeline.worker(event(accepted), execute=execute)
            return httpx.Response(200, json={"receipt_id": accepted["receipt_id"]})
        if path == "/internal/result":
            return httpx.Response(200, json=pipeline.result(request.url.params["receipt_id"]))
        if path == "/internal/presented":
            body = json.loads(request.content)
            accepted = pipeline.accept(body["message"], source=body["source"], kind="presented", reference=body["reference"])
            pipeline.worker(event(accepted), execute=execute)
            return httpx.Response(200, json={"status": "accepted"})
        raise AssertionError(path)
    env = {"CALORIE_SERVICE_URL": "https://fixture", "CALORIE_SERVICE_TOKEN": "fixture-token", "CALORIE_SERVICE_AUTH": "bearer", "CLI_USER_ID": "user", "CONSOLE_USER_ID": "user"}
    with patch.dict("os.environ", env), patch("agent.config.load_config", return_value=config), httpx.Client(transport=httpx.MockTransport(http)) as transport:
        client = LiveClient(client=transport)
        with patch.multiple(cli, LiveClient=lambda: client, load_config=lambda: config, _interactive=lambda: iter(["建议1900", "好的"])):
            assert cli.main(["--preset", "live"]) == 0
        assert CosStore(objects, pipeline.prefix, readonly=True).get_standard("user")["kcal"] == 1900
        with patch.multiple(console, LiveClient=lambda: client, load_config=lambda: config):
            result = console.api_agent({"text": "建议1900", "session_id": "production", "message_id": "p1", "preset": "live"})
            proposal = CosStore(objects, pipeline.prefix, readonly=True).load_session("user", "dsh:production")["proposal"]
            assert not proposal["presented"]
            console.api_agent_presented({"delivery_reference": result["delivery_reference"]})
            assert CosStore(objects, pipeline.prefix, readonly=True).load_session("user", "dsh:production")["proposal"]["presented"]
            result = console.api_agent({"text": "好的", "session_id": "production", "message_id": "p2", "preset": "live"})
            assert result["status"] == "ok" and sources == ["cli", "cli", "dsh", "dsh"]
        cam_env = {"CALORIE_SERVICE_AUTH": "cam", "CALORIE_CLOUD_UIN": "100000000001",
                   "CALORIE_SERVICE_TOKEN": "fixture-service-token", "TENCENTCLOUD_SECRET_ID": "function-role",
                   "TENCENTCLOUD_SECRET_KEY": "function-key", "TENCENTCLOUD_SESSION_TOKEN": "function-session"}
        with patch.dict("os.environ", cam_env):
            cam_client = LiveClient(client=transport)
            cam_result = cam_client.submit(message("cam-proposal", "建议1900", "cam-session"), source="dsh")
            cam_client.presented(cam_result["delivery_reference"])
            state = cam_client._request("GET", "/internal/result", params={"receipt_id": cam_result["delivery_reference"]["receipt_id"]})
            assert state["status"] == "completed"


def P04():
    """三类候选逐样本保留；只缺暖时可追加一次，质量失败不能洗白。"""
    from copy import deepcopy
    from harness.eval import production_live as live

    worker, ingress = 'a'*32, 'b'*32
    samples = []
    platform = {'messages':{}}
    for index, step in enumerate(live.STEPS):
        mid, rid = 'fresh-'+step, 'receipt-'+step
        received = 1000+10*index
        service = {'record':4535, 'revise':4076, 'today':3677}[step]
        events = [{'kind':'cos_create', 'id':'ingress-'+step, 'instance_id':ingress,
                   'message_id':mid, 'started_epoch_ms':received*1000+10, 'duration_ms':20}]
        events.extend({'kind':'model_http', 'purpose':'main', 'id':mid+'-model-'+str(call),
                       'instance_id':worker, 'message_id':mid, 'outcome':'ok',
                       'started_epoch_ms':received*1000+1000+call*300, 'duration_ms':100}
                      for call in range(1))
        samples.append({'step':step, 'message_id':mid, 'receipt_id':rid, 'replay':False,
            'entry_mode':'synthetic_webhook', 'ack_elapsed_ms':900, 't_service_ms':service,
            'original_received_at':received, 'auto_quality_passed':True,
            'receipt_state':{'status':'completed', 'receipt_id':rid, 'completed_at':received+service/1000+.1,
                             'result':{'latency_events':events, 'model_calls':1, 'reply_source':'tool_facts', 'model_reply':''}}})
        evidence = {'cold_start':None, 'build_id':'candidate-build', 'semantic_review_passed':True,
                    'worker_instance_id':worker, 'reference':'actual-platform-log-reference'}
        if index:
            evidence.update(process_reused=True, warm_basis='worker_process_reuse', reference={
                'current':f'/requests/{index}/receipt_state/result/latency_events',
                'prior':'/requests/0/receipt_state/result/latency_events'})
        else:
            evidence['cold_start'] = False
        platform['messages'][mid] = evidence
    base = {'requests':samples, 'archive':{'status':'passed'}, 'cleanup':{'status':'passed'}}
    plan = {'platform_evidence_file':__file__, 'build_id':'candidate-build'}

    def checked(report=None, evidence=None):
        report = deepcopy(base if report is None else report)
        with patch.object(live, '_read_json', return_value=deepcopy(platform if evidence is None else evidence)):
            live.finish_gates(plan, report)
        return report

    passed = checked()
    assert passed['release_gate_passed']
    old_loop = deepcopy(base)
    old_loop['requests'][0]['receipt_state']['result']['model_calls'] = 2
    assert not checked(old_loop)['release_gate_passed']
    assert tuple(passed['gates']) == ('record', 'revise', 'today')
    assert passed['gates']['record']['samples'][0]['warm_basis'] == 'platform_cold_start_false'
    for step in ('revise','today'):
        gate = passed['gates'][step]['samples'][0]
        assert gate['platform_cold_start'] is None and gate['warm_basis']=='worker_process_reuse'
        assert gate['warm_speed_passed'] and not passed['gates'][step]['warm_sample_needed']
        assert gate['worker_reuse_proof']['worker_instance_id']==worker

    def rejected(report, evidence=platform):
        result = checked(report, evidence)
        gate = next(iter(result['gates']['revise']['samples']), {})
        assert not result['release_gate_passed'] and not gate.get('warm_speed_passed')
        assert not gate.get('warm_basis') and not gate.get('worker_reuse_proof')
        return result

    replay = deepcopy(base)
    replay['requests'][1]['replay'] = True
    rejected(replay)
    prior_replay = deepcopy(base)
    prior_replay['requests'][0]['replay'] = True
    rejected(prior_replay)
    ingress_only = deepcopy(base)
    for sample in ingress_only['requests']:
        sample['receipt_state']['result']['latency_events'] = sample['receipt_state']['result']['latency_events'][:1]
    copied_ingress = deepcopy(platform)
    copied_ingress['messages']['fresh-revise']['worker_instance_id'] = ingress
    rejected(ingress_only, copied_ingress)
    other_worker = deepcopy(base)
    for event in other_worker['requests'][1]['receipt_state']['result']['latency_events'][1:]:
        event['instance_id'] = 'c'*32
    other_evidence = deepcopy(platform)
    other_evidence['messages']['fresh-revise']['worker_instance_id'] = 'c'*32
    rejected(other_worker, other_evidence)
    mixed_worker = deepcopy(base)
    mixed_worker['requests'][1]['receipt_state']['result']['latency_events'][-1]['instance_id'] = 'c'*32
    rejected(mixed_worker)
    unfinished = deepcopy(base)
    unfinished['requests'][0]['receipt_state']['status'] = 'processing'
    rejected(unfinished)
    late = deepcopy(base)
    late['requests'][0]['receipt_state']['completed_at'] = late['requests'][1]['original_received_at']+1
    rejected(late)
    copied_result = deepcopy(base)
    copied_result['requests'][1]['receipt_state']['result'] = deepcopy(copied_result['requests'][0]['receipt_state']['result'])
    rejected(copied_result)
    explicit_cold = deepcopy(platform)
    explicit_cold['messages']['fresh-revise']['cold_start'] = True
    conflict = rejected(base, explicit_cold)
    assert conflict['gates']['revise']['samples'][0]['platform_cold_start'] is True
    manual_warm = deepcopy(platform)
    evidence = manual_warm['messages']['fresh-revise']
    evidence['warm'] = True
    evidence.pop('process_reused')
    rejected(base, manual_warm)
    for field,value in [('worker_instance_id','c'*32), ('warm_basis','operator_says_warm'),
                        ('reference',{'current':'/requests/1/receipt_state/result/latency_events','prior':'/requests/2/receipt_state/result/latency_events'})]:
        mismatch = deepcopy(platform)
        mismatch['messages']['fresh-revise'][field] = value
        rejected(base, mismatch)

    explicit_warm = deepcopy(platform)
    explicit_warm['messages']['fresh-revise'] = {'cold_start':False, 'build_id':'candidate-build',
        'semantic_review_passed':True, 'reference':'actual-platform-warm-log'}
    marked = checked(base, explicit_warm)['gates']['revise']['samples'][0]
    assert marked['warm_basis']=='platform_cold_start_false' and marked['warm_speed_passed'] and marked['worker_reuse_proof'] is None
    slow = deepcopy(base)
    slow['requests'][1]['t_service_ms'] = 5000.1
    slow_report = checked(slow)
    slow_gate = slow_report['gates']['revise']['samples'][0]
    assert not slow_gate['warm_speed_passed'] and not slow_report['gates']['revise']['warm_sample_needed']

    # A cold original may be supplemented; it remains visible beside the new warm result.
    cold = deepcopy(base)
    cold['requests'][0]['t_service_ms'] = 6395
    cold['requests'][0]['receipt_state']['completed_at'] = 1006.5
    cold_evidence = deepcopy(platform)
    cold_evidence['messages']['fresh-record']['cold_start'] = True
    cold_report = checked(cold, cold_evidence)
    assert not cold_report['release_gate_passed'] and cold_report['missing_warm_categories'] == ['record']
    assert not cold_report['unresolved_quality_failures'] and not cold_report['evidence_gaps']
    warm = deepcopy(samples[0])
    warm.update(message_id='new-warm-record', receipt_id='new-warm-receipt', original_received_at=1040)
    warm['receipt_state'].update(receipt_id=warm['receipt_id'], completed_at=1045)
    for event in warm['receipt_state']['result']['latency_events']:
        event.update(message_id=warm['message_id'], id='new-'+event['id'], started_epoch_ms=event['started_epoch_ms']+40000)
    appended = deepcopy(cold_report)
    appended['requests'].append(warm)
    cold_evidence['messages'][warm['message_id']] = deepcopy(platform['messages']['fresh-record'])
    appended = checked(appended, cold_evidence)
    assert appended['release_gate_passed'] and len(appended['gates']['record']['samples']) == 2
    assert appended['requests'][:3] == cold['requests']
    replayed = deepcopy(appended)
    replayed['requests'][-1]['replay'] = True
    assert not checked(replayed, cold_evidence)['release_gate_passed']
    # Later passing samples and edited evidence cannot erase an observed quality failure.
    poisoned = deepcopy(appended)
    poisoned['requests'][0]['ack_elapsed_ms'] = 3001
    poisoned = checked(poisoned, cold_evidence)
    assert not poisoned['release_gate_passed'] and poisoned['unresolved_quality_failures']
    poisoned['requests'][0]['ack_elapsed_ms'] = 900
    assert not checked(poisoned, cold_evidence)['release_gate_passed']

    # Final report status is a CLI gate; generating a rejected report is not success.
    import tempfile
    from pathlib import Path
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        platform_path, plan_path = root / 'platform.json', root / 'plan.json'
        cli_plan = {**plan, 'run_id':'report-status', 'platform_evidence_file':str(platform_path)}
        plan_path.write_text(json.dumps(cli_plan), encoding='utf-8')
        report_path = root / 'reports/production-candidate/report-status.json'
        report_path.parent.mkdir(parents=True)
        saved = {**deepcopy(base), 'plan_sha256':live._digest(cli_plan), 'stage_attempts':[]}
        report_path.write_text(json.dumps(saved), encoding='utf-8')
        platform_path.write_text('{}', encoding='utf-8')
        with patch.object(live, 'ROOT', root), patch.object(live, 'validate_plan', lambda value:value), patch.object(live, 'Driver') as driver:
            assert live.main(['--plan', str(plan_path), '--stage', 'report']) == 1
            assert json.loads(report_path.read_text(encoding='utf-8'))['release_gate_passed'] is False
            platform_path.write_text(json.dumps(platform), encoding='utf-8')
            assert live.main(['--plan', str(plan_path), '--stage', 'report']) == 0
            final = json.loads(report_path.read_text(encoding='utf-8'))
            assert final['release_gate_passed'] is True and final['requests'] == base['requests']
            driver.assert_not_called()
            # A successful intermediate operation may still have incomplete final evidence.
            platform_path.write_text('{}', encoding='utf-8')
            assert live.main(['--plan', str(plan_path), '--stage', 'archive']) == 0
            driver.return_value.archive.assert_called_once_with()

        # No new stage/controller: business appends the single fixed missing-warm plan.
        rows = [{'food_name':name, 'date':'2026-11-03', 'meal_type':'dinner', 'grams':100,
                 'nutrition':{'kcal':100, 'carbs':10, 'protein':5, 'fat':2}, 'estimated':False}
                for name in ('known-A', 'known-B')]
        message = {'step':'record', 'message_id':warm['message_id'], 'text':'记录已知A和B各100克',
                   'expected_items':rows, 'expected_total_kcal':200}
        fixed = json.loads((live.ROOT / 'harness/eval/production-live-plan.template.json').read_text(encoding='utf-8'))
        fixed.update(run_id='minimum-plan', build_id='candidate-build', bucket='approved',
                     prefix='cos-probe/production-candidate/minimum-plan/', user_id='candidate', sender_open_id='sender',
                     chat_id='chat', webhook_url='https://approved.example/webhook/feishu/events', webhook_auth='feishu_token',
                     intake_table_id='candidate-table', bitable_app_token='existing-app', production_intake_table_id='production-table',
                     candidate_table_verified=True, source_evidence=[{'source':'independent','excerpt':'100 kcal/100g'}])
        fixed['messages'] = [{**deepcopy(message), 'step':step, 'message_id':'planned-'+step} for step in live.STEPS]
        assert live.validate_plan(fixed) is fixed
        assert not any(key in fixed for key in ('pause_confirmation_file','resume_confirmation_file','knowledge_evidence'))
        driver = live.Driver.__new__(live.Driver)
        driver.plan, driver.report, driver.save = fixed, {}, lambda:None
        with patch.object(driver, 'run_message') as run:
            driver.business()
            assert [call.args[0]['step'] for call in run.call_args_list] == list(live.STEPS)
            assert 'driver_create_time_ms' not in fixed['messages'][0]
        supplement_path = root / 'supplement.json'
        addition_plan = {**plan, 'platform_evidence_file':str(platform_path),
                         'warm_supplement_plan_file':str(supplement_path), 'entry_mode':'synthetic_webhook'}
        addition = {'base_plan_sha256':live._digest(addition_plan), 'source_evidence':[{'source':'independent','excerpt':'100 kcal/100g'}],
                    'message':message}
        supplement_path.write_text(json.dumps(addition), encoding='utf-8')
        platform_path.write_text(json.dumps(cold_evidence), encoding='utf-8')
        driver = live.Driver.__new__(live.Driver)
        driver.plan = addition_plan
        driver.report = {**deepcopy(cold_report), 'plan_sha256':live._digest(addition_plan), 'business_stage':'auto_checks_passed'}
        driver.report.pop('cleanup')
        original_samples = deepcopy(driver.report['requests'])
        driver.save = lambda:None
        def run_message(planned):
            assert planned == message
            sample = deepcopy(warm)
            driver.report['requests'].append(sample)
            return sample
        with patch.object(driver, 'run_message', side_effect=run_message) as run:
            driver.business()
            assert driver.report['requests'][:3] == original_samples
            assert driver.report['warm_supplements'][0]['plan'] == addition
            assert driver.report['prior_archives'] == [{'status':'passed'}]
            try:
                driver.business()
            except ValueError:
                pass
            else:
                raise AssertionError('same warm supplement ran twice')
            run.assert_called_once()
        failed_driver = live.Driver.__new__(live.Driver)
        failed_driver.plan, failed_driver.save = addition_plan, lambda:None
        failed_driver.report = deepcopy(cold_report)
        failed_driver.report.pop('cleanup')
        failed_driver.report.update(business_stage='auto_checks_passed', plan_sha256=live._digest(addition_plan))
        failed_driver.report['requests'][0]['auto_quality_passed'] = False
        with patch.object(failed_driver, 'run_message') as forbidden:
            try:
                failed_driver.business()
            except ValueError:
                pass
            else:
                raise AssertionError('quality failure was replaced by a warm supplement')
            forbidden.assert_not_called()

        # With the scheduler running, an earlier confirmed revision is not the final readback.
        from agent.storage import bitable_adapter as bitable
        target_rows = [{**deepcopy(row), 'record_id':'fixed-'+str(index), 'user_id':'candidate',
                        'created_at':1000000, 'agent_revision':2} for index, row in enumerate(rows)]
        target = {'items':target_rows, 'commit_key':'exact-final-commit'}
        archive_driver = live.Driver.__new__(live.Driver)
        archive_driver.plan = {'bitable_app_token':'existing-app', 'intake_table_id':'candidate-table'}
        archive_driver.report = {'business_stage':'auto_checks_passed', 'requests':[{
            'step':'record', 'replay':False, 'auto_quality_passed':True, 'original_received_at':1000,
            'planned_message':message, 'raw_cos_audit':target}]}
        archive_driver.save = lambda:None
        class Sync:
            reads = 0
            def snapshot(self):
                self.reads += 1
                return self.reads, {'item:archive:intake:'+row['record_id']:'ref' for row in target_rows}
            def document(self, index, key):
                return {'status':'confirmed', 'ack_revision':min(self.reads, 2), 'confirmed_at':1005}
        archive_driver.sync = Sync()
        actual_rows = [('feishu-'+row['record_id'], {'agent_item_id':row['record_id'], 'agent_revision':2,
                        bitable._intake_field('date'):row['date']}) for row in target_rows]
        with patch.object(bitable, '_list_records', return_value=actual_rows), \
                patch.object(bitable, 'BitableStore') as store, \
                patch.object(live.time, 'time', return_value=1005), patch.object(live.time, 'sleep') as sleep:
            store.return_value._intake_summary_item.side_effect = lambda identity, fields:deepcopy(next(row for row in target_rows if row['record_id']==identity))
            archive_driver.archive()
            sleep.assert_called_once_with(2)
            assert archive_driver.report['archive']['status'] == 'passed'
            assert archive_driver.report['archive']['fixed_cleanup_record_ids'] == [rid for rid, _ in actual_rows]


CHECKS = {"P01": P01, "P02": P02, "P03": P03, "P04": P04}
