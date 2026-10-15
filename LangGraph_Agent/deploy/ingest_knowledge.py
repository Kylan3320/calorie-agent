#!/usr/bin/env python3
"""Build the LightRAG index from the knowledge corpus (RAG M3).

Reads agent/storage/knowledge_seed.py (+ optionally the Bitable knowledge_base
table) and inserts each entry into LightRAG. Output lands in deploy/rag_index/
and ships inside the RAG Layer (see deploy/build_rag_layer.sh).

Requirements (local ingest only): pip install lightrag-hku httpx, and env:
  DEEPSEEK_API_KEY, SILICONFLOW_API_KEY

Usage: .venv/bin/python deploy/ingest_knowledge.py [--from-bitable]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

INDEX_DIR = ROOT / "deploy" / "rag_index"


def _load_env_file() -> None:
    path = Path("/tmp/v4_env.json")
    if path.exists():
        for key, value in json.loads(path.read_text()).items():
            os.environ.setdefault(key, str(value))


def _bitable_knowledge() -> list[dict]:
    """Pull active knowledge entries from Bitable (seed file stays the fallback)."""
    env_path = Path("/tmp/v4_env.json")
    env = json.loads(env_path.read_text()) if env_path.exists() else {}
    app_token, table = env.get("BITABLE_APP_TOKEN", ""), env.get("BITABLE_KNOWLEDGE_TABLE_ID", "")
    if not app_token or not table:
        return []
    def _post(url, payload, token=None):
        req = urllib.request.Request(url, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {token}"} if token else {})})
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    tok = _post("https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
                {"app_id": env["FEISHU_APP_ID"], "app_secret": env["FEISHU_APP_SECRET"]})["tenant_access_token"]
    def txt(f, n):
        v = f.get(n)
        return v[0]["text"] if isinstance(v, list) and v else v
    out = []
    rows, page = [], ""
    while True:
        q = {"page_size": "500"}
        if page:
            q["page_token"] = page
        req = urllib.request.Request(
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}/tables/{table}/records?{urllib.parse.urlencode(q)}",
            headers={"Authorization": f"Bearer {tok}"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read().decode()).get("data") or {}
        rows.extend(data.get("items", []))
        if not data.get("has_more"):
            break
        page = data.get("page_token", "")
    for it in rows:
        f = it.get("fields", {})
        if txt(f, "status") not in ("", "active"):
            continue
        question, answer = txt(f, "question"), txt(f, "answer")
        if question and answer:
            out.append({"question": question, "answer": answer, "source": txt(f, "source") or ""})
    return out


def _documents(from_bitable: bool) -> list[str]:
    from agent.storage.knowledge_seed import KNOWLEDGE_SEED

    entries: list[dict] = list(KNOWLEDGE_SEED)
    if from_bitable:
        live = _bitable_knowledge()
        if live:
            entries = live
            print(f"[ingest] using {len(live)} entries from Bitable knowledge_base")
    docs = []
    for entry in entries:
        source = entry.get("source") or "膳食指南"
        docs.append(f"问题：{entry['question']}\n回答：{entry['answer']}\n来源：{source}")
    return docs


async def _run(from_bitable: bool) -> None:
    from lightrag import LightRAG
    from lightrag.utils import EmbeddingFunc
    import httpx

    sys.path.insert(0, str(ROOT))
    os.environ["RAG_INDEX_DIR"] = str(INDEX_DIR)
    from agent.storage.knowledge_retriever import _build_lightrag  # reuse llm/embed wiring

    rag = _build_lightrag()
    _ = LightRAG  # keep the import explicit for readers
    await rag.initialize_storages()

    docs = _documents(from_bitable)
    print(f"[ingest] inserting {len(docs)} documents into {INDEX_DIR}")
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    try:
        await rag.ainsert(docs)
    finally:
        await rag.finalize_storages()
    print("[ingest] done. Index files:", sorted(p.name for p in INDEX_DIR.iterdir()))


def main() -> int:
    parser = argparse.ArgumentParser(description="Build LightRAG knowledge index")
    parser.add_argument("--from-bitable", action="store_true", help="use live Bitable rows instead of seed file")
    args = parser.parse_args()
    _load_env_file()
    asyncio.run(_run(args.from_bitable))
    return 0


if __name__ == "__main__":
    sys.exit(main())
