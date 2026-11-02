#!/usr/bin/env python3
"""Upload the function zip; shared Tencent SCF request signing for deployment scripts."""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVICE = "scf"
HOST = "scf.tencentcloudapi.com"
REGION = os.getenv("SCF_REGION", "ap-shanghai")
NAMESPACE = "default"


def tc3_request(action: str, payload: dict, version: str = "2018-04-16") -> dict:
    secret_id = os.environ.get("TENCENTCLOUD_SECRET_ID", "")
    secret_key = os.environ.get("TENCENTCLOUD_SECRET_KEY", "")
    if not secret_id or not secret_key:
        raise RuntimeError("需要 TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY")
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

    k_date = _hmac(("TC3" + secret_key).encode(), date)
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
            f"TC3-HMAC-SHA256 Credential={secret_id}/{date}/{SERVICE}/tc3_request, "
            f"SignedHeaders=content-type;host;x-tc-action, Signature={signature}"
        ),
    }
    token = os.getenv("TENCENTCLOUD_SESSION_TOKEN") or os.getenv("TENCENTCLOUD_SECURITY_TOKEN") or os.getenv("TENCENTCLOUD_SESSIONTOKEN")
    if token:
        headers["X-TC-Token"] = token
    req = urllib.request.Request(f"https://{HOST}/", data=body.encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=300) as resp:
        result = json.loads(resp.read().decode())["Response"]
    if "Error" in result:
        error = result["Error"]
        raise RuntimeError(f"{action}: {error.get('Code')} {error.get('Message')}")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Upload the SCF function zip")
    parser.add_argument("--function", required=True, help="SCF function affected by this change")
    parser.add_argument("zip", nargs="?", type=Path, default=ROOT / "deploy" / "calorie-agent-v4.zip")
    args = parser.parse_args(argv)
    zip_path = args.zip.expanduser().resolve()
    zip_b64 = base64.b64encode(zip_path.read_bytes()).decode()
    target = {"FunctionName": args.function, "Namespace": NAMESPACE}
    tc3_request("UpdateFunctionCode", {**target, "ZipFile": zip_b64})
    status = tc3_request("GetFunction", target)
    print(f"已提交 {args.function}/{zip_path.name}; Status={status.get('Status')} ModTime={status.get('ModTime')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
