#!/usr/bin/env python3
"""Import knowledge seed corpus into the Bitable knowledge_base table.

Idempotent: entry_id uses the contract formula and each batch carries a
client_token derived from it, so re-running never duplicates rows.

Usage: .venv/bin/python deploy/seed_knowledge.py   # env via /tmp/v4_env.json or environ
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from agent.storage.bitable_adapter import stable_uuid  # noqa: E402
from agent.storage.knowledge_retriever import normalize_text  # noqa: E402
from agent.storage.knowledge_seed import KNOWLEDGE_SEED  # noqa: E402


def _env(key: str) -> str:
    return os.getenv(key, "")


def _load_env_file() -> None:
    for key, value in json.loads(Path("/tmp/v4_env.json").read_text()).items():
        os.environ.setdefault(key, str(value))


def _post(url: str, payload: dict, token: str | None = None) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode(),
        method="POST",
        headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read().decode())


def main() -> int:
    _load_env_file()
    app_id, app_secret = _env("FEISHU_APP_ID"), _env("FEISHU_APP_SECRET")
    app_token, table = _env("BITABLE_APP_TOKEN"), _env("BITABLE_KNOWLEDGE_TABLE_ID")
    if not all([app_id, app_secret, app_token, table]):
        print("missing FEISHU_APP_ID/SECRET, BITABLE_APP_TOKEN or BITABLE_KNOWLEDGE_TABLE_ID")
        return 1
    token = _post(
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        {"app_id": app_id, "app_secret": app_secret},
    )["tenant_access_token"]

    records = []
    for item in KNOWLEDGE_SEED:
        entry_id = stable_uuid(f"calorie-agent:knowledge:{normalize_text(item['question'])}")
        records.append(
            {
                "fields": {
                    "entry_id": entry_id,
                    "question": item["question"],
                    "answer": item["answer"],
                    "aliases": item.get("aliases", ""),
                    "tags": item.get("tags", ""),
                    "source": item.get("source", ""),
                    "status": "active",
                }
            }
        )
    client_token = stable_uuid("calorie-agent:knowledge-import:" + stable_uuid("batch:" + str(len(records))))
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table}/records/batch_create?client_token={client_token}"
    result = _post(url, {"records": records}, token)
    print(f"import {len(records)} entries -> code {result.get('code')} {result.get('msg')}")
    return 0 if result.get("code") == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
