#!/usr/bin/env python3
"""Build a complete LightRAG snapshot in a new directory; Bitable is the default source."""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import math
import os
import struct
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
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


def verify_index(index: Path, documents: list[str] | None = None) -> dict:
    """Check complete documents, usable vectors and graph/chunk provenance."""
    def load(name):
        return json.loads((index / name).read_text(encoding="utf-8"))
    full, chunks, statuses = map(load, ("kv_store_full_docs.json", "kv_store_text_chunks.json", "kv_store_doc_status.json"))
    contents = [row["content"] for row in full.values()]
    if not full or (documents is not None and sorted(contents) != sorted(documents)):
        raise ValueError("Index documents do not match the complete source")
    if set(statuses) != set(full) or any(row["status"] != "processed" for row in statuses.values()):
        raise ValueError("Index contains unfinished documents")
    if not chunks or any(row["full_doc_id"] not in full for row in chunks.values()):
        raise ValueError("Chunk references an absent document")
    vectors = {}
    for filename in ("vdb_chunks.json", "vdb_entities.json", "vdb_relationships.json"):
        data = load(filename)
        rows, dimension = data["data"], data["embedding_dim"]
        matrix = base64.b64decode(data["matrix"], validate=True)
        if dimension <= 0 or len(matrix) != len(rows) * dimension * 4:
            raise ValueError(f"Invalid vector matrix: {filename}")
        for offset in range(0, len(matrix), dimension * 4):
            values = [v[0] for v in struct.iter_unpack("<f", matrix[offset:offset + dimension * 4])]
            if not all(math.isfinite(v) for v in values) or not any(values):
                raise ValueError(f"Non-finite or zero embedding: {filename}")
        if filename == "vdb_chunks.json":
            if len(rows) != len(chunks) or {row["__id__"] for row in rows} != set(chunks):
                raise ValueError("Chunk vectors do not match the text chunks")
        elif any(not set(row["source_id"].split("<SEP>")) <= set(chunks) for row in rows):
            raise ValueError(f"Unresolved vector provenance: {filename}")
        vectors[filename] = {"rows": len(rows), "dimension": dimension}
    graph = ET.parse(index / "graph_chunk_entity_relation.graphml").getroot()
    ns = {"g": "http://graphml.graphdrawing.org/xmlns"}
    source_keys = {key.attrib["id"] for key in graph.findall("g:key", ns) if key.attrib.get("attr.name") == "source_id"}
    if len({value["dimension"] for value in vectors.values()}) != 1:
        raise ValueError("Vector dimensions differ")
    for item in graph.findall(".//g:node", ns) + graph.findall(".//g:edge", ns):
        sources = [data.text or "" for data in item.findall("g:data", ns) if data.attrib.get("key") in source_keys]
        if not sources or any(not set(value.split("<SEP>")) <= set(chunks) for value in sources):
            raise ValueError("Unresolved graph provenance")
    return {"document_count": len(full), "chunk_count": len(chunks), "vectors": vectors}


def _base_archive(path: Path, expected_sha256: str):
    if hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256:
        raise ValueError("Base layer SHA256 does not match")
    return zipfile.ZipFile(path)


def _dependency(name):
    parts = Path(name).parts
    return not name.endswith("/") and parts[0] != "rag_index" and "__pycache__" not in parts and not name.endswith(".pyc")


def package_layer(index: Path, base: Path, expected_sha256: str, target: Path) -> dict:
    verification = verify_index(index)
    version = (index / "INDEX_VERSION").read_text(encoding="utf-8").strip()
    if not version:
        raise ValueError("INDEX_VERSION must not be empty")
    dependencies = hashlib.sha256()
    with _base_archive(base, expected_sha256) as original:
        with zipfile.ZipFile(target, "x", zipfile.ZIP_DEFLATED) as output:
            for name in (n for n in original.namelist() if _dependency(n)):
                body = original.read(name)
                output.writestr(original.getinfo(name), body, compress_type=zipfile.ZIP_DEFLATED)
                dependencies.update(name.encode())
                dependencies.update(body)
            for path in sorted(index.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                    info = zipfile.ZipInfo("rag_index/" + path.relative_to(index).as_posix())
                    info.create_system = 3
                    info.external_attr = 0o100644 << 16
                    output.writestr(info, path.read_bytes(), compress_type=zipfile.ZIP_DEFLATED)
    return {**verification, "artifact": str(target), "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
            "bytes": target.stat().st_size, "index_version": version,
            "base_layer_sha256": expected_sha256, "dependency_content_sha256": dependencies.hexdigest()}


async def check_query(index: Path, query: str, expected: list[str], rejected: list[str]) -> dict:
    from lightrag import QueryParam
    from agent.storage.knowledge_retriever import _build_lightrag
    os.environ["RAG_INDEX_DIR"] = str(index)
    rag = _build_lightrag()
    await rag.initialize_storages()
    try:
        result = await rag.aquery_data(query, param=QueryParam(mode="mix", top_k=3))
    finally:
        await rag.finalize_storages()
    chunks = result.get("data", result).get("chunks", [])[:3]
    evidence = "\n".join(chunk.get("content", "") for chunk in chunks)
    if any(text not in evidence for text in expected) or any(text in evidence for text in rejected):
        raise ValueError("Retrieval does not meet the supplied source expectations")
    return {"query": query, "retrieved_chunks": chunks}


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
    verification = verify_index(output, docs)
    version = "knowledge-" + hashlib.sha256("\n".join(docs).encode()).hexdigest()[:16]
    (output / "INDEX_VERSION").write_text(version, encoding="utf-8")
    print(json.dumps({**verification, "output": str(output), "index_version": version}, ensure_ascii=False))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build a new LightRAG snapshot")
    parser.add_argument("--source", choices=("bitable", "seed"), default="bitable")
    parser.add_argument("--output", type=Path, default=ROOT / "deploy" / "rag_index")
    parser.add_argument("--base-layer", type=Path, help="verified Python 3.10 layer ZIP supplying the exact dependencies")
    parser.add_argument("--base-sha256", help="expected SHA256 of --base-layer")
    parser.add_argument("--check-only", action="store_true")
    parser.add_argument("--package", type=Path, help="package the existing index with the base layer dependencies")
    parser.add_argument("--query", help="explicit real-provider retrieval check")
    parser.add_argument("--expect-text", action="append", default=[])
    parser.add_argument("--reject-text", action="append", default=[])
    args = parser.parse_args(argv)
    index = args.output.expanduser().resolve()
    if bool(args.base_layer) != bool(args.base_sha256):
        parser.error("--base-layer and --base-sha256 are required together")
    if (args.expect_text or args.reject_text) and not args.query:
        parser.error("text expectations require --query")
    if args.package:
        if not args.base_layer or args.query or args.check_only:
            parser.error("--package requires the base layer and cannot be combined with --query/--check-only")
        print(json.dumps(package_layer(index, args.base_layer, args.base_sha256, args.package), ensure_ascii=False))
        return 0
    if args.check_only:
        print(json.dumps(verify_index(index), ensure_ascii=False))
        if not args.query:
            return 0
    if not args.base_layer:
        parser.error("building or querying requires --base-layer and --base-sha256")
    if sys.platform != "linux" or sys.version_info[:2] != (3, 10):
        parser.error("the frozen layer dependencies require Linux Python 3.10")
    with tempfile.TemporaryDirectory(prefix="calorie-rag-") as temporary:
        dependencies = Path(temporary)
        with _base_archive(args.base_layer, args.base_sha256) as archive:
            for name in archive.namelist():
                if _dependency(name):
                    archive.extract(name, dependencies)
        sys.path.insert(0, str(dependencies))
        os.environ["TIKTOKEN_CACHE_DIR"] = str(dependencies / "tiktoken_cache")
        if not args.check_only:
            asyncio.run(_run(args.source, index))
        if args.query:
            print(json.dumps(asyncio.run(check_query(index, args.query, args.expect_text, args.reject_text)), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
