"""Authenticated async handoff to the existing, serial Event function."""
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
import httpx
from .telemetry import span, EVENTS
from .timeutil import remaining_timeout

_CLIENT = httpx.Client()
WORKER = "calorie-agent-reminder"
REGION = "ap-shanghai"
HOST = "scf.tencentcloudapi.com"


def function_url_headers(request):
    """Sign the exact HTTPX request using the current SCF role environment.

    Uses the verified function-URL TC3 algorithm, independently of application
    service-token authorization. No request credentials enter persisted events.
    https://cloud.tencent.com/document/product/583/96100
    """
    secret_id = os.getenv("TENCENTCLOUD_SECRET_ID") or os.getenv("TENCENTCLOUD_SECRETID", "")
    secret_key = os.getenv("TENCENTCLOUD_SECRET_KEY") or os.getenv("TENCENTCLOUD_SECRETKEY", "")
    token = os.getenv("TENCENTCLOUD_SESSION_TOKEN") or os.getenv("TENCENTCLOUD_SESSIONTOKEN", "")
    uin = os.getenv("CALORIE_CLOUD_UIN", "")
    if not all((secret_id, secret_key, token, uin)):
        raise RuntimeError("function URL CAM authentication requires current role credentials and CALORIE_CLOUD_UIN")
    if request.url.scheme != "https":
        raise ValueError("function URL CAM authentication requires HTTPS")
    timestamp = int(time.time())
    day = datetime.fromtimestamp(timestamp, timezone.utc).strftime("%Y-%m-%d")
    content_type = request.headers.get("content-type", "application/json")
    host = request.headers["host"]
    raw_path = request.url.raw_path.split(b"?", 1)[0].decode("ascii")
    query = "&".join(sorted(request.url.query.decode("ascii").split("&")))
    signed = "content-type;host"
    canonical = "\n".join((request.method.upper(), raw_path, query,
                            f"content-type:{content_type}\nhost:{host}\n", signed,
                            hashlib.sha256(request.content).hexdigest()))
    scope = day + "/scf/tc3_request"
    to_sign = f"TC3-HMAC-SHA256\n{timestamp}\n{scope}\n" + hashlib.sha256(canonical.encode()).hexdigest()
    def sign(key, value):
        return hmac.new(key, value.encode(), hashlib.sha256).digest()
    key = sign(sign(sign(("TC3" + secret_key).encode(), day), "scf"), "tc3_request")
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {"Content-Type": content_type, "Authorization":
            f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, SignedHeaders={signed}, Signature={signature}",
            "X-Scf-Cam-Uin": uin, "X-Scf-Cam-Timestamp": str(timestamp), "X-Scf-Cam-Token": token}


def dispatch_event(event, headers=None):
    # SCF injects role credentials into HTTP headers; timer/CLI uses environment.
    headers = headers or {}
    secret_id = headers.get("X-Scf-Secret-Id") or os.getenv("TENCENTCLOUD_SECRET_ID") or os.getenv("TENCENTCLOUD_SECRETID", "")
    secret_key = headers.get("X-Scf-Secret-Key") or os.getenv("TENCENTCLOUD_SECRET_KEY") or os.getenv("TENCENTCLOUD_SECRETKEY", "")
    token = headers.get("X-Scf-Session-Token") or os.getenv("TENCENTCLOUD_SESSION_TOKEN") or os.getenv("TENCENTCLOUD_SESSIONTOKEN", "")
    if not secret_id or not secret_key:
        raise RuntimeError("SCF invocation role credentials missing")
    worker = os.getenv("CALORIE_MESSAGE_FUNCTION", WORKER)
    event = {**event, "handoff_started_epoch_ms": time.time()*1000,
             "ingress_events": list(EVENTS.get() or [])}
    body=json.dumps({"FunctionName":worker,"Namespace":"default","InvocationType":"Event",
                     "ClientContext":json.dumps(event,ensure_ascii=False,separators=(",",":"))},ensure_ascii=False,separators=(",",":")).encode()
    timestamp=int(time.time());day=datetime.fromtimestamp(timestamp,timezone.utc).strftime("%Y-%m-%d")
    canonical="POST\n/\n\ncontent-type:application/json; charset=utf-8\nhost:"+HOST+"\nx-tc-action:invoke\n\ncontent-type;host;x-tc-action\n"+hashlib.sha256(body).hexdigest()
    scope=day+"/scf/tc3_request"
    string_to_sign=f"TC3-HMAC-SHA256\n{timestamp}\n{scope}\n"+hashlib.sha256(canonical.encode()).hexdigest()
    def sign(key,text):return hmac.new(key,text.encode(),hashlib.sha256).digest()
    key=sign(sign(sign(("TC3"+secret_key).encode(),day),"scf"),"tc3_request")
    signature=hmac.new(key,string_to_sign.encode(),hashlib.sha256).hexdigest()
    auth=f"TC3-HMAC-SHA256 Credential={secret_id}/{scope}, SignedHeaders=content-type;host;x-tc-action, Signature={signature}"
    with span("scf_handoff",target=worker):
        response=_CLIENT.post("https://"+HOST+"/",content=body,headers={"Content-Type":"application/json; charset=utf-8","Host":HOST,"X-TC-Action":"Invoke","X-TC-Version":"2018-04-16","X-TC-Region":REGION,"X-TC-Timestamp":str(timestamp),"X-TC-Token":token,"Authorization":auth},timeout=remaining_timeout(2))
        response.raise_for_status()
        result=response.json()["Response"]
        if "Error" in result:
            raise RuntimeError("SCF handoff failed: "+result["Error"].get("Code","unknown"))
        if not result.get("RequestId") or result.get("Result",{}).get("InvokeResult",0)!=0:
            raise RuntimeError("SCF handoff not accepted")
        return result["RequestId"]

