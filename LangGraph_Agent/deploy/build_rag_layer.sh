#!/usr/bin/env bash
# 构建 RAG 云函数 Layer（依赖 + 索引产物），供知识问答检索使用。
# 产出：deploy/rag-layer.zip（腾讯 SCF 层格式：PYTHONPATH=/var/user:/opt，
# 所以依赖包必须放在 zip 根目录——不是 AWS 式 python/ 子目录；rag_index/ 挂 /opt/rag_index）。
#
# 前置：deploy/rag_index/ 已由 deploy/ingest_knowledge.py 生成。
# 依赖按 cp310 + manylinux2014 下载（与 SCF 运行时一致），并做体积精简：
#   删 pandas/xlsxwriter（仅 Excel 导出用）、google-genai（仅 gemini.py 用）、
#   lightrag/api（服务器）、__pycache__、dist-info；保留 parser 内部依赖。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LAYER_ROOT="$ROOT/deploy/rag-layer"
PIP="$ROOT/.venv/bin/pip"
INDEX_SOURCE="${RAG_INDEX_SOURCE:-$ROOT/deploy/rag_index}"

rm -rf "$LAYER_ROOT"
mkdir -p "$LAYER_ROOT"

echo "[1/6] 下载 cp310 manylinux 依赖（lightrag-hku 全树，装到 zip 根目录）..."
"$PIP" install -q --target "$LAYER_ROOT" \
  --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux_2_28_x86_64 \
  --implementation cp --python-version 3.10 --only-binary :all: \
  lightrag-hku nano-vectordb networkx tiktoken regex httpx pydantic pypinyin json_repair \
  python-dotenv configparser ascii_colors tenacity aiohttp

echo "[2/6] 精简..."
cd "$LAYER_ROOT"
# 注意：lightrag 包内部硬引用 lightrag.parser（__init__ 链路），不能删；
# lightrag/api 与 pandas/google 等按需再补
rm -rf pandas* xlsxwriter* google* google_auth* cryptography* pyasn1* proto* \
       lightrag/api lightrag/__pycache__ \
       __pycache__ *.dist-info bin include 2>/dev/null || true
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true

echo "[3/6] tiktoken BPE 预热（防 SCF 冷启动联网下载）..."
cd "$ROOT"  # 用仓库 venv 的 mac 版 tiktoken 生成缓存，不能在 layer 目录里跑（那是 cp310 linux 构建）
TIKTOKEN_CACHE_DIR="$LAYER_ROOT/tiktoken_cache" "$ROOT/.venv/bin/python" - <<'PY'
import tiktoken
enc = tiktoken.encoding_for_model("gpt-4o-mini")  # LightRAG TiktokenTokenizer 默认
enc.encode("预热 o200k_base 词表")
print("tiktoken cache ready")
PY

echo "[4/6] 拷贝索引产物..."
if [[ ! -d "$INDEX_SOURCE" ]]; then
  echo "缺少索引目录 $INDEX_SOURCE（先跑 deploy/ingest_knowledge.py）" >&2
  exit 1
fi
cp -r "$INDEX_SOURCE" "$LAYER_ROOT/rag_index"
date -u +%Y%m%d%H%M%S > "$LAYER_ROOT/rag_index/INDEX_VERSION"

echo "[5/6] 打包..."
cd "$LAYER_ROOT"
rm -f "$ROOT/deploy/rag-layer.zip"
zip -qr "$ROOT/deploy/rag-layer.zip" .
cd - >/dev/null
echo "built: deploy/rag-layer.zip ($(du -h "$ROOT/deploy/rag-layer.zip" | cut -f1))"
