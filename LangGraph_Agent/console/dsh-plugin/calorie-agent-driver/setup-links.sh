#!/usr/bin/env bash
# 把插件依赖符号链接到本地 DSH 仓库的同一构建产物，保证宿主与插件共享同一个
# 模块实例（类同一性）。DSH_REPO 可用环境变量覆盖。
set -euo pipefail

DSH_REPO="${DSH_REPO:-/Users/boohee/Documents/DSH}"
DIR="$(cd "$(dirname "$0")" && pwd)"
TARGET="$DIR/node_modules/@deepseek-ai"
mkdir -p "$TARGET"

link() {
  local src="$1" dst="$2"
  if [ ! -e "$dst" ]; then
    ln -s "$src" "$dst"
    echo "linked $(basename "$dst")"
  fi
}

# 注册表依赖：指向 apps/cli 的 node_modules（宿主 dsh CLI 运行时使用的同一份）
link "$DSH_REPO/apps/cli/node_modules/@deepseek-ai/cordis"      "$TARGET/cordis"
link "$DSH_REPO/apps/cli/node_modules/@deepseek-ai/schemastery" "$TARGET/schemastery"
# 工作区包：指向 packages 下的同一目录（exports -> lib/index.js，宿主加载同一文件）
link "$DSH_REPO/packages/core/agent"      "$TARGET/dsh-agent"
link "$DSH_REPO/packages/core/session"    "$TARGET/dsh-session"
link "$DSH_REPO/packages/llm/llm"         "$TARGET/dsh-llm"
link "$DSH_REPO/packages/core/scope"      "$TARGET/dsh-scope"
link "$DSH_REPO/packages/core/agent-loop" "$TARGET/dsh-agent-loop"

echo "node_modules links ready at $TARGET"
