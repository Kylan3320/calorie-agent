#!/usr/bin/env python3
"""Publish/attach the RAG layer while preserving function environment and other layers."""

from __future__ import annotations

import argparse
import base64
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from deploy.api_deploy import NAMESPACE, tc3_request


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish and attach the RAG layer")
    parser.add_argument("--function", required=True, help="SCF function that executes knowledge retrieval")
    parser.add_argument("zip", nargs="?", type=Path, default=ROOT / "deploy" / "rag-layer.zip")
    parser.add_argument("--attach-only", type=int, metavar="VERSION")
    args = parser.parse_args(argv)
    target = {"FunctionName": args.function, "Namespace": NAMESPACE}
    current = tc3_request("GetFunction", target)
    version = args.attach_only
    if version is None:
        created = tc3_request("PublishLayerVersion", {
            "LayerName": "calorie-rag", "CompatibleRuntimes": ["Python3.10"],
            "Description": "LightRAG dependencies and knowledge snapshot",
            "Content": {"ZipFile": base64.b64encode(args.zip.expanduser().resolve().read_bytes()).decode()},
        })
        version = created["LayerVersion"]
    variables = {item["Key"]: item["Value"] for item in (current.get("Environment") or {}).get("Variables", [])}
    variables.update(RAG_INDEX_DIR="/opt/rag_index", KNOWLEDGE_RETRIEVER="lightrag",
                     TIKTOKEN_CACHE_DIR="/opt/tiktoken_cache")
    layers = [{"LayerName": item["LayerName"], "LayerVersion": version if item["LayerName"] == "calorie-rag" else item["LayerVersion"]}
              for item in current.get("Layers") or []]
    if not any(item["LayerName"] == "calorie-rag" for item in layers):
        layers.append({"LayerName": "calorie-rag", "LayerVersion": version})
    tc3_request("UpdateFunctionConfiguration", {
        **target, "Layers": layers,
        "Environment": {"Variables": [{"Key": key, "Value": value} for key, value in variables.items()]},
    })
    print(f"已提交 {args.function}: calorie-rag v{version}，请核验生效配置。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
