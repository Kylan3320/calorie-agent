"""knowledge_retriever seam (contracts/knowledge_schema.md).

config.yaml `knowledge_retriever:` selects the implementation:
- `faq`: active Bitable entries (seeded in FakeStore).
- `lightrag`: a published knowledge snapshot, queried via aquery_data.

Unknown kinds raise — a config error must surface, never silently degrade.
"""

from __future__ import annotations

import os
import re
import threading
import unicodedata
from collections.abc import Callable
from typing import Any

KNOWLEDGE_RETRIEVERS = ("faq", "lightrag")
TOP_K = 2
MIN_SCORE = 0.5
TAG_BONUS = 0.3

KnowledgeRetriever = Callable[..., dict[str, Any]]


def build_knowledge_retriever(kind: str) -> KnowledgeRetriever:
    if kind == "faq":
        return retrieve_faq
    if kind == "lightrag":
        return retrieve_lightrag
    raise ValueError(f"unknown knowledge_retriever kind: {kind} (expected one of {KNOWLEDGE_RETRIEVERS})")


def retrieve_faq(query: str, entries: list[dict[str, Any]]) -> dict[str, Any]:
    normalized_query = normalize_text(query)
    if not normalized_query or not entries:
        return _not_found()
    scored = sorted((_score_entry(normalized_query, entry) for entry in entries), key=lambda item: -item[1])
    hits = [item for item in scored[:TOP_K] if item[1] >= MIN_SCORE]
    if not hits:
        return _not_found()
    matched_entries = [item[0] for item in hits]
    evidence = "\n".join(f"[{entry.get('source', '')}] Q:{entry.get('question', '')} A:{entry.get('answer', '')}" for entry in matched_entries)
    return {
        "status": "ok",
        "entries": [
            {"entry_id": entry.get("entry_id", ""), "question": entry.get("question", ""), "answer": entry.get("answer", ""), "source": entry.get("source", ""), "score": round(score, 3)}
            for entry, score in hits
        ],
        "evidence": evidence,
    }


def _score_entry(query: str, entry: dict[str, Any]) -> tuple[dict[str, Any], float]:
    keys = [normalize_text(entry.get("question", ""))]
    keys.extend(normalize_text(alias) for alias in _split_aliases(entry.get("aliases")))
    best = 0.0
    for key in (key for key in keys if key):
        best = max(best, _score_key(query, key))
    if best >= MIN_SCORE and any(tag and tag in query for tag in _split_aliases(entry.get("tags"))):
        best += TAG_BONUS
    return entry, best


def _score_key(query: str, key: str) -> float:
    if query == key:
        return 1.0
    if key in query and len(key) >= 4:
        return 0.9
    if query in key and len(query) >= 4:
        return 0.85
    overlap = _char_overlap(query, key)
    if overlap >= 0.6:
        # 0.6 是刻意门槛：中文常用单字（影响/减脂/期间）重叠 0.5 上下非常容易误命中
        return 0.5 + (overlap - 0.6) * 0.75
    return 0.0


def _char_overlap(query: str, key: str) -> float:
    query_chars = set(re.sub(r"[的吗呢？?怎么如何应该能可以适合吗]", "", query))
    key_chars = set(key)
    if not query_chars or not key_chars:
        return 0.0
    return len(query_chars & key_chars) / len(query_chars)


def _split_aliases(value: Any) -> list[str]:
    if not value:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in re.split("[、,，;；]", str(value)) if part.strip()]


def normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    return re.sub(r"[\s,，、。；;:：|/\\()（）\[\]{}？?！!]+", "", text)


def _not_found() -> dict[str, Any]:
    return {"status": "not_found", "entries": [], "evidence": ""}


# --- lightrag implementation (M3, contracts/knowledge_schema.md) ---------------

_LIGHTRAG_INSTANCE: Any = None
_LIGHTRAG_READY = False
_INDEX_MIRROR_LOCK = threading.Lock()


def _writable_index_dir() -> str:
    """SCF Layer 挂载的 /opt 只读，而 LightRAG 查询后会 flush 缓存文件——
    把索引镜像到 /tmp 再用；Layer 更新（INDEX_VERSION 变化）时自动重拷。
    请求可能并发到达同一实例：版本检查+拷贝必须持锁且可重入。"""
    src = os.getenv("RAG_INDEX_DIR", "/opt/rag_index")
    if not os.path.isdir(src):
        raise RuntimeError(f"RAG index directory is missing: {src}")
    probe = os.path.join(src, ".write_probe")
    try:
        with open(probe, "w") as handle:
            handle.write("1")
        os.remove(probe)
        return src
    except OSError:
        pass
    with _INDEX_MIRROR_LOCK:
        dst = "/tmp/rag_index"
        src_version = _read_version(src)
        dst_version = _read_version(dst)
        if src_version and src_version == dst_version and os.path.isdir(dst):
            return dst  # 另一个线程已完成镜像
        import shutil

        if os.path.exists(dst):
            shutil.rmtree(dst)
        shutil.copytree(src, dst, dirs_exist_ok=True)
        with open(os.path.join(dst, "INDEX_VERSION"), "w") as handle:
            handle.write(src_version or "")
        return dst


def _read_version(index_dir: str) -> str:
    path = os.path.join(index_dir, "INDEX_VERSION")
    try:
        with open(path) as handle:
            return handle.read().strip()
    except OSError:
        return ""


def retrieve_lightrag(query: str, entries: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """LightRAG retrieval via aquery_data: chunks原文进 evidence，不用其 LLM 生成答案。

    Lazy singleton: import + storage init cost is only paid by knowledge queries.
    entries 参数与 faq 签名对齐，lightrag 不消费它（索引自带语料）。
    """
    global _LIGHTRAG_INSTANCE
    import asyncio

    rag = _LIGHTRAG_INSTANCE
    if rag is None:
        rag = _build_lightrag()
        _LIGHTRAG_INSTANCE = rag

    async def _run() -> dict[str, Any]:
        global _LIGHTRAG_READY
        from lightrag import QueryParam

        if not _LIGHTRAG_READY:
            await rag.initialize_storages()
            _LIGHTRAG_READY = True
        # singleton 常驻：不再 finalize（进程退出由运行时回收）
        from .timeutil import remaining_timeout
        data = await asyncio.wait_for(rag.aquery_data(query, param=QueryParam(mode="mix", top_k=3)), timeout=remaining_timeout(85))
        # aquery_data wraps the payload: {status, message, data: {chunks, entities, ...}}
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            return data["data"]
        return data if isinstance(data, dict) else {}

    data = asyncio.run(_run())
    chunks = data.get("chunks") if isinstance(data, dict) else None
    hits: list[dict[str, Any]] = []
    for chunk in (chunks or [])[:TOP_K]:
        if not isinstance(chunk, dict):
            continue
        text = str(chunk.get("content") or "").strip()
        if not text:
            continue
        hits.append(
            {
                "entry_id": str(chunk.get("chunk_id") or ""),
                "question": str(chunk.get("file_path") or "知识片段"),
                "answer": text,
                # 原文自带"来源：…"行；置空避免 composer 重复追加
                "source": "",
                "score": 1.0,
            }
        )
    if not hits:
        return _not_found()
    evidence = "\n".join(h["answer"] for h in hits)
    return {"status": "ok", "entries": hits, "evidence": evidence}


def _build_lightrag() -> Any:
    """Construct the LightRAG singleton; DeepSeek as LLM, SiliconFlow bge-m3 as embedder."""
    from lightrag import LightRAG

    working_dir = _writable_index_dir()

    async def deepseek_llm(prompt: str, system_prompt: str | None = None, history_messages: list = [], **kwargs: Any) -> str:
        api_key = os.getenv("DEEPSEEK_API_KEY", "")
        base = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com").rstrip("/")
        messages = ([] if not system_prompt else [{"role": "system", "content": system_prompt}])
        messages += list(history_messages or []) + [{"role": "user", "content": prompt}]
        body = {
            "model": os.getenv("DEEPSEEK_MODEL", "deepseek-v4-flash"),
            "messages": messages,
            "temperature": 0,
            "stream": False,
        }
        import httpx

        # 抽取提示词长、DeepSeek 生成慢：120s（LightRAG 自带 240s 任务级超时）
        async with httpx.AsyncClient(timeout=120) as client:
            resp = await client.post(
                f"{base}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
            payload = resp.json()
        return payload["choices"][0]["message"]["content"]

    async def siliconflow_embed(texts: list[str]) -> list[list[float]]:
        if os.getenv("RAG_STUB_EMBEDDING") == "1":
            # 管线验证用：确定性伪向量（部署/质量验证必须用真 bge-m3，见 D15/D24）
            import hashlib

            import numpy as np

            dim = int(os.getenv("RAG_EMBEDDING_DIM", "1024"))
            vectors = []
            for text in texts:
                seed = int(hashlib.md5(text.encode("utf-8")).hexdigest()[:12], 16)
                vectors.append([((seed >> (i % 24)) & 0xFF) / 255.0 - 0.5 for i in range(dim)])
            return np.array(vectors, dtype=np.float32)
        api_key = os.getenv("SILICONFLOW_API_KEY", "")
        if not api_key:
            raise RuntimeError("SILICONFLOW_API_KEY is not configured (RAG M3 embedding).")
        import httpx

        async with httpx.AsyncClient(timeout=60) as client:
            resp = await client.post(
                "https://api.siliconflow.cn/v1/embeddings",
                json={"model": os.getenv("SILICONFLOW_EMBEDDING_MODEL", "BAAI/bge-m3"), "input": texts},
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.raise_for_status()
            payload = resp.json()
        ordered = sorted(payload["data"], key=lambda item: item["index"])
        # EmbeddingFunc.__call__ requires a single ndarray (it reads result.size)
        import numpy as np

        return np.array([item["embedding"] for item in ordered], dtype=np.float32)

    embed_func = __import__("lightrag.utils", fromlist=["EmbeddingFunc"]).EmbeddingFunc(
        embedding_dim=int(os.getenv("RAG_EMBEDDING_DIM", "1024")),
        max_token_size=8192,
        func=siliconflow_embed,
    )
    rag = LightRAG(
        working_dir=working_dir,
        llm_model_func=deepseek_llm,
        embedding_func=embed_func,
    )
    return rag


__all__ = ["KNOWLEDGE_RETRIEVERS", "build_knowledge_retriever", "retrieve_faq", "retrieve_lightrag"]
