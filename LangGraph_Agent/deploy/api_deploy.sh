#!/usr/bin/env bash
# 通过腾讯云 API 部署 V4（不经控制台，可重复执行）。
# 用法：
#   export TENCENTCLOUD_SECRET_ID=xxx  TENCENTCLOUD_SECRET_KEY=yyy
#   ./deploy/api_deploy.sh
# 首次使用：.venv/bin/tccli configure set --secretId ... --secretKey ... --region ap-shanghai
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ZIP="$ROOT/deploy/calorie-agent-v401.zip"
REGION="ap-shanghai"
FUNCTION="calorie-agent-api-v3-test"
NAMESPACE="default"

export TENCENTCLOUD_SECRET_ID="${TENCENTCLOUD_SECRET_ID:?需要 TENCENTCLOUD_SECRET_ID}"
export TENCENTCLOUD_SECRET_KEY="${TENCENTCLOUD_SECRET_KEY:?需要 TENCENTCLOUD_SECRET_KEY}"

TC="$ROOT/.venv/bin/tccli"
[[ -x "$TC" ]] || TC="tccli"

# 1) 上传代码：UpdateFunctionCode 支持 ZipFile（base64）。17MB -> base64 ~23MB，在 API 限制内。
echo "[1/2] UpdateFunctionCode ($(du -h "$ZIP" | cut -f1))..."
B64=$(base64 -i "$ZIP" | tr -d '\n')
"$TC" scf UpdateFunctionCode \
  --region "$REGION" \
  --FunctionName "$FUNCTION" \
  --Namespace "$NAMESPACE" \
  --ZipFile "$B64" \
  --handler ignored >/dev/null

echo "[2/2] 等待函数状态更新..."
sleep 20
"$TC" scf GetFunction --region "$REGION" --FunctionName "$FUNCTION" --Namespace "$NAMESPACE" \
  --output json | python3 -c "import json,sys; d=json.load(sys.stdin); print('status:', d.get('Status'), '| update:', d.get('StatusReasons') or 'ok')"

echo "部署命令已提交。验证：curl -s https://1440448581-65q5tm95zk.ap-shanghai.tencentscf.com/health"
