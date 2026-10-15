#!/usr/bin/env python3
"""Publish the RAG layer and attach it to the cloud function (RAG M3).

1. CreateLayerVersion with rag-layer.zip (ZipFile base64)
2. UpdateFunctionConfiguration Layers=[...] (+ RAG_INDEX_DIR=/opt/rag_index)

Usage: TENCENTCLOUD_SECRET_ID=... TENCENTCLOUD_SECRET_KEY=... \
       .venv/bin/python deploy/deploy_rag_layer.py [deploy/rag-layer.zip]
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SECRET_ID = os.environ.get("TENCENTCLOUD_SECRET_ID", "")
SECRET_KEY = os.environ.get("TENCENTCLOUD_SECRET_KEY", "")
HOST, SERVICE, REGION = "scf.tencentcloudapi.com", "scf", "ap-shanghai"
FUNCTION = os.environ.get("SCF_FUNCTION", "calorie-agent-api-v3-test")


def tc3(action: str, payload: dict) -> dict:
    body = json.dumps(payload, separators=(",", ":"))
    ts = int(time.time())
    date = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    canonical = (
        f"POST\n/\n\ncontent-type:application/json; charset=utf-8\nhost:{HOST}\n"
        f"x-tc-action:{action.lower()}\n\ncontent-type;host;x-tc-action\n"
        + hashlib.sha256(body.encode()).hexdigest()
    )
    string_to_sign = (
        f"TC3-HMAC-SHA256\n{ts}\n{date}/{SERVICE}/tc3_request\n"
        + hashlib.sha256(canonical.encode()).hexdigest()
    )

    def _hmac(key: bytes, msg: str) -> bytes:
        return hmac.new(key, msg.encode(), hashlib.sha256).digest()

    key = _hmac(_hmac(_hmac(("TC3" + SECRET_KEY).encode(), date), SERVICE), "tc3_request")
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "Host": HOST,
        "X-TC-Action": action,
        "X-TC-Version": "2018-04-16",
        "X-TC-Region": REGION,
        "X-TC-Timestamp": str(ts),
        "Authorization": (
            f"TC3-HMAC-SHA256 Credential={SECRET_ID}/{date}/{SERVICE}/tc3_request, "
            f"SignedHeaders=content-type;host;x-tc-action, Signature={signature}"
        ),
    }
    request = urllib.request.Request(f"https://{HOST}/", data=body.encode(), headers=headers, method="POST")
    with urllib.request.urlopen(request, timeout=300) as response:
        return json.loads(response.read().decode())


def main() -> int:
    if not SECRET_ID or not SECRET_KEY:
        sys.exit("需要 TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY")
    attach_only = "--attach-only" in sys.argv
    if attach_only:
        idx = sys.argv.index("--attach-only")
        layer_version = int(sys.argv[idx + 1])
    else:
        zip_path = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "deploy" / "rag-layer.zip")
        zip_b64 = base64.b64encode(zip_path.read_bytes()).decode()
        print(f"[layer] zip={zip_path} base64={len(zip_b64) // 1024 // 1024}MB")

        print("[layer] PublishLayerVersion ...")
        created = tc3(
            "PublishLayerVersion",
            {
                "LayerName": "calorie-rag",
                "CompatibleRuntimes": ["Python3.10"],
                "Description": "LightRAG deps + knowledge index (calorie agent RAG M3)",
                "Content": {"ZipFile": zip_b64},
            },
        ).get("Response", {})
        if "Error" in created:
            sys.exit(f"PublishLayerVersion 失败: {created['Error']}")
        layer_version = created.get("LayerVersion")
    print(f"[layer] version={layer_version}")

    print("[fn] UpdateFunctionConfiguration Layers ...")
    updated = tc3(
        "UpdateFunctionConfiguration",
        {
            "FunctionName": FUNCTION,
            "Namespace": "default",
            "Layers": [{"LayerName": "calorie-rag", "LayerVersion": layer_version}],
            "Environment": {
                "Variables": _merged_env({"RAG_INDEX_DIR": "/opt/rag_index", "KNOWLEDGE_RETRIEVER": "lightrag"})
            },
        },
    ).get("Response", {})
    if "Error" in updated:
        sys.exit(f"UpdateFunctionConfiguration 失败: {updated['Error']}")
    print("[fn] submitted. 注意：等 30s 后重放一条消息验证；旧实例惰性回收见 README。")
    return 0


def _merged_env(extra: dict) -> list:
    env_path = Path("/tmp/v4_env.json")
    variables = json.loads(env_path.read_text()) if env_path.exists() else {}
    variables.update(extra)
    variables.setdefault("TIKTOKEN_CACHE_DIR", "/opt/tiktoken_cache")
    return [{"Key": k, "Value": v} for k, v in variables.items()]


if __name__ == "__main__":
    sys.exit(main())
