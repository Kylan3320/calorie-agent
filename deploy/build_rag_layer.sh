#!/usr/bin/env bash
# Reuse a verified layer's dependencies; package only the replacement index.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
PYTHON="${PYTHON:-python3}"
: "${RAG_BASE_LAYER:?Set RAG_BASE_LAYER to the verified layer ZIP}"
: "${RAG_BASE_LAYER_SHA256:?Set RAG_BASE_LAYER_SHA256 to its expected hash}"
"$PYTHON" "$ROOT/deploy/ingest_knowledge.py" \
  --output "${RAG_INDEX_SOURCE:-$ROOT/deploy/rag_index}" \
  --base-layer "$RAG_BASE_LAYER" --base-sha256 "$RAG_BASE_LAYER_SHA256" \
  --package "${RAG_LAYER_OUTPUT:-$ROOT/deploy/rag-layer.zip}"
