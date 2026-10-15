#!/usr/bin/env bash
# 打包腾讯云函数部署 zip。
# zip 根目录直接包含 app.py / agent / config.yaml / scf_bootstrap / requirements.txt / deps。
#
# SCF 运行时是 Python 3.10（Linux x86_64），pydantic-core 等含编译扩展的依赖必须用
# 对应平台的 wheel。本机无 Docker 时，脚本按 cp310 + manylinux2014_x86_64 下载 wheel，
# PyYAML（无 manylinux wheel）以 sdist 内的纯 Python 包 vendor 进 deps。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BUILD="$ROOT/deploy/build"
ZIP="$ROOT/deploy/calorie-agent-v4.zip"

rm -rf "$BUILD"
mkdir -p "$BUILD"

copy_sources() {
  cp -r "$ROOT/agent" "$BUILD/agent"
  cp "$ROOT/app.py" "$ROOT/config.yaml" "$ROOT/scf_bootstrap" "$ROOT/requirements.txt" "$BUILD/"
  find "$BUILD" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
  chmod +x "$BUILD/scf_bootstrap"
}

install_deps() {
  local dest="$BUILD/deps"
  local wheels="/tmp/v4wheels"
  mkdir -p "$dest" "$wheels"
  if [[ "${USE_DOCKER:-0}" == "1" ]] && command -v docker >/dev/null 2>&1; then
    docker run --rm -v "$ROOT":/src -w /src python:3.10-slim \
      pip install -r requirements.txt --target /src/deploy/build/deps -q
    return
  fi
  # 无 Docker：langgraph 依赖树按 SCF 平台下载 wheel；PyYAML 无 manylinux wheel，
  # 但其 Linux 形态就是纯 Python，直接从 sdist vendor 出 yaml/ 包。
  # 注意：--platform 下载需要较新 pip（系统自带 pip 21 不认新 manylinux 标签），优先用 .venv 的 pip。
  local pip_bin="python3 -m pip"
  if [[ -x "$ROOT/.venv/bin/pip" ]]; then
    pip_bin="$ROOT/.venv/bin/pip"
  elif python3 -m pip --version 2>/dev/null | grep -qE "pip 2[2-9]|pip [3-9]"; then
    :
  else
    echo "[error] 需要 pip>=22 执行跨平台 wheel 下载；请先创建 .venv 或升级 pip" >&2
    return 1
  fi
  echo "[info] 按 SCF 平台 cp310 + manylinux2014_x86_64 下载依赖 wheel ($pip_bin)"
  $pip_bin download "langgraph>=0.2,<0.6" -q -d "$wheels" \
    --only-binary=:all: \
    --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux_2_28_x86_64 \
    --implementation cp --python-version 3.10
  # install --target 也要带平台参数，否则按宿主机解释器校验 wheel 标签会拒绝 cp310
  $pip_bin install -q --no-index --find-links "$wheels" "langgraph>=0.2,<0.6" --target "$dest" \
    --only-binary=:all: \
    --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux_2_28_x86_64 \
    --implementation cp --python-version 3.10

  echo "[info] vendor 纯 Python PyYAML"
  $pip_bin download "PyYAML>=6.0,<7" --no-deps -q --no-binary :all: -d "$wheels"
  python3 - "$wheels" "$dest" <<'PY'
import glob
import os
import shutil
import sys
import tarfile

wheels_dir, dest = sys.argv[1], sys.argv[2]
sdists = [p for p in glob.glob(f"{wheels_dir}/*.tar.gz") if "yaml" in os.path.basename(p).lower()]
sdist = sdists[0]
with tarfile.open(sdist) as tar:
    members = [m for m in tar.getmembers() if "/lib/yaml/" in m.name and m.name.endswith(".py")]
    for member in members:
        rel = member.name.split("/lib/")[-1]
        target = os.path.join(dest, rel)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with tar.extractfile(member) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
print(f"vendored {len(members)} yaml modules")
PY
}

copy_sources
install_deps
(
  cd "$BUILD"
  zip -qr "$ZIP" .
)
echo "built: $ZIP ($(du -h "$ZIP" | cut -f1))"
