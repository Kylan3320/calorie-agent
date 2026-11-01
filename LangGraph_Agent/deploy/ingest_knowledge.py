#!/usr/bin/env python3
"""Build a complete LightRAG snapshot in a new directory; Bitable is the default source."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _documents(source: str) -> list[str]:
    if source == "seed":
        from agent.storage.knowledge_seed import KNOWLEDGE_SEED
        entries = KNOWLEDGE_SEED
    else:
        from agent.storage import BitableStore
        entries = BitableStore().load_knowledge()
    if not entries:
        raise RuntimeError(f"No active knowledge entries from {source}; existing index is unchanged")
    return [f"问题：{entry['question']}\n回答：{entry['answer']}\n来源：{entry.get('source') or ''}" for entry in entries]


async def _run(source: str, output: Path) -> None:
    docs = _documents(source)
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"Output directory must be empty: {output}")
    if os.getenv("RAG_STUB_EMBEDDING") == "1" and source == "bitable":
        raise ValueError("Production knowledge snapshots require real embeddings")
    output.mkdir(parents=True, exist_ok=True)
    os.environ["RAG_INDEX_DIR"] = str(output)
    from agent.storage.knowledge_retriever import _build_lightrag
    rag = _build_lightrag()
    await rag.initialize_storages()
    try:
        await rag.ainsert(docs)
    finally:
        await rag.finalize_storages()
    print(f"Built {len(docs)} documents at {output}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a new LightRAG snapshot")
    parser.add_argument("--source", choices=("bitable", "seed"), default="bitable")
    parser.add_argument("--output", type=Path, default=ROOT / "deploy" / "rag_index")
    args = parser.parse_args(argv)
    asyncio.run(_run(args.source, args.output.expanduser().resolve()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
