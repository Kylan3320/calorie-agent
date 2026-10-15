#!/usr/bin/env python3
"""Deploy V4 via Tencent Cloud API (UpdateFunctionCode), bypassing the console.

Usage:
  TENCENTCLOUD_SECRET_ID=... TENCENTCLOUD_SECRET_KEY=... python3 deploy/api_deploy.py [zip路径]
"""

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

SECRET_ID = os.environ.get("TENCENTCLOUD_SECRET_ID", "")
SECRET_KEY = os.environ.get("TENCENTCLOUD_SECRET_KEY", "")
if not SECRET_ID or not SECRET_KEY:
    sys.exit("需要 TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY 环境变量")

SERVICE = "scf"
HOST = "scf.tencentcloudapi.com"
REGION = "ap-shanghai"
FUNCTION = os.environ.get("SCF_FUNCTION", "calorie-agent-api-v3-test")
NAMESPACE = "default"

zip_path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "calorie-agent-v401.zip")
if not os.path.isabs(zip_path):
    zip_path = os.path.join(os.path.dirname(__file__), zip_path)

with open(zip_path, "rb") as fh:
    zip_b64 = base64.b64encode(fh.read()).decode()
print(f"[deploy] zip={zip_path} ({len(zip_b64) // 1024 // 1024}MB base64={len(zip_b64) // 1024 // 1024}MB)")


def tc3_request(action: str, payload: dict, version: str = "2018-04-16") -> dict:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    ts = int(time.time())
    date = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")

    canonical = (
        "POST\n/\n\n"
        f"content-type:application/json; charset=utf-8\nhost:{HOST}\n"
        f"x-tc-action:{action.lower()}\n"
        "\ncontent-type;host;x-tc-action\n" + hashlib.sha256(body.encode()).hexdigest()
    )
    string_to_sign = (
        f"TC3-HMAC-SHA256\n{ts}\n{date}/{SERVICE}/tc3_request\n"
        + hashlib.sha256(canonical.encode()).hexdigest()
    )

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    k_date = _hmac(("TC3" + SECRET_KEY).encode(), date)
    k_service = _hmac(k_date, SERVICE)
    k_signing = _hmac(k_service, "tc3_request")
    signature = hmac.new(k_signing, string_to_sign.encode(), hashlib.sha256).hexdigest()

    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Host": HOST,
        "X-TC-Action": action,
        "X-TC-Version": version,
        "X-TC-Region": REGION,
        "X-TC-Timestamp": str(ts),
        "Authorization": (
            f"TC3-HMAC-SHA256 Credential={SECRET_ID}/{date}/{SERVICE}/tc3_request, "
            f"SignedHeaders=content-type;host;x-tc-action, Signature={signature}"
        ),
    }
    req = urllib.request.Request(f"https://{HOST}/", data=body.encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=300) as resp:
        return json.loads(resp.read().decode())


print("[deploy] UpdateFunctionCode ...")
result = tc3_request(
    "UpdateFunctionCode",
    {"FunctionName": FUNCTION, "Namespace": NAMESPACE, "ZipFile": zip_b64},
)
print(json.dumps(result, ensure_ascii=False)[:300])
if "Error" in result.get("Response", {}):
    err = result["Response"]["Error"]
    sys.exit(f"部署失败: {err.get('Code')} {err.get('Message')}")

print("[deploy] 已提交，等待状态同步 25s ...")
time.sleep(25)

status = tc3_request("GetFunction", {"FunctionName": FUNCTION, "Namespace": NAMESPACE})
resp = status.get("Response", {})
print("Status:", resp.get("Status"), "| ModTime:", resp.get("ModTime"))
print("验证: curl -s https://1440448581-65q5tm95zk.ap-shanghai.tencentscf.com/health")
