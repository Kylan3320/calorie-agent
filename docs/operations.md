# 运行与部署

以下命令均从仓库根目录运行。本地控制台使用 Python 3.12 或更新版本，云函数使用 Linux Python 3.10。

## 本地控制台与 DSH

```powershell
.\.venv\Scripts\python.exe -m pip install -r console/requirements.txt
$env:CONSOLE_USER_ID = "本人飞书open_id"
.\.venv\Scripts\python.exe -m console.server
```

后端监听 http://127.0.0.1:8765。live 经统一云端受理，需要 CALORIE_SERVICE_URL、CALORIE_SERVICE_TOKEN 和 CONSOLE_USER_ID；别名由已发布 COS 快照归一。
DSH 请求携带 session_id、message_id；追问按会话隔离，消息 ID 用于防重。

DSH 是外部依赖，本仓库不包含其构建和 profile 数据。准备方式：

1. 在已安装并构建的 DSH 仓库中确认 agent-loop 导出 ReactLoopAgent 和 turnBoundaryProjectionDefinition；当前驱动依赖这两个接口。
2. 设置 DSH_REPO 为该仓库绝对路径，用 Bash 运行 console/dsh-plugin/calorie-agent-driver/setup-links.sh。
3. 通过所用 DSH 版本的插件管理命令安装本地 calorie-agent-driver，创建 calorie profile，加载 dsh-base、dsh-web-app、calorie-agent-driver。
4. 设置 DSH_HOME 指向本仓库 console/dsh-home，再在 DSH 仓库运行 `pnpm dsh --profile calorie`。

驱动 patch 设置后端地址、预设和端口。默认热量助手；/dsh、/calorie 切换之后新建会话的引擎。
setup-links.sh 会检查依赖及所需导出；升级 DSH 后重新运行此步骤。

## 运行评测

在仓库根目录运行：

```powershell
.\.venv\Scripts\python.exe harness/eval/runner.py --list
.\.venv\Scripts\python.exe harness/eval/runner.py --case U01
```

Linux 或 macOS 使用 `.venv/bin/python`。评测用 `--list` 查看场景、`--case ID[,ID]` 严格选择；全量用 `--all` 显式运行。`--real-model` 需选择 E 类场景，轨迹默认按 UTC 时间独立保存；`--output PATH` 可指定新文件，已有文件会在调用模型前拒绝覆盖。

## 接入飞书

先配置飞书应用的机器人能力、消息接收事件 `im.message.receive_v1` 和发消息权限。事件订阅通过 HTTP 发送到云函数的 `/webhook/feishu/events`，使用 Verification Token 校验。此入口使用明文事件，飞书的 Encrypt Key 留空。

创建需要的多维表格和 COS 存储桶，按下节配置对应的 ID、凭据及云函数。表的字段见[多维表格结构](../contracts/bitable_schema.md)。

## 环境配置

凭据和表 ID 从进程环境读取：

- 飞书：FEISHU_APP_ID、FEISHU_APP_SECRET、FEISHU_VERIFICATION_TOKEN、BITABLE_APP_TOKEN。
- 表：BITABLE_FOOD_TABLE_ID、BITABLE_INTAKE_TABLE_ID、BITABLE_STANDARD_TABLE_ID、BITABLE_PENDING_TABLE_ID、BITABLE_USAGE_TABLE_ID、BITABLE_KNOWLEDGE_TABLE_ID；可选 BITABLE_ALIASES_TABLE_ID、USER_ID_ALIASES。
- 模型与数据源：DEEPSEEK_API_KEY、BOOHEE_API_KEY；LightRAG 使用 SILICONFLOW_API_KEY。
- 生产COS：COS_BUCKET、COS_PREFIX、COS_REGION（上海）；SCF运行角色注入临时凭据，按每次请求／事件刷新。
- 受理与worker：CALORIE_MESSAGE_FUNCTION。CLI／DSH及定时提醒HTTP受理使用 CALORIE_SERVICE_URL、CALORIE_SERVICE_TOKEN；本地本人身份 CLI_USER_ID／CONSOLE_USER_ID。候选CAM认证入口配置 CALORIE_SERVICE_AUTH=cam 和 CALORIE_CLOUD_UIN，内部服务令牌独立传输；正式现有入口使用Bearer。令牌仅配置在私有环境，不能写入包。
- 轨迹：可设置 TRACE_PATH。

PRESET、KNOWLEDGE_RETRIEVER 优先于 YAML；model、timezone_offset_hours 优先使用 YAML，环境变量仅作缺省值。
每日目标及表字段见 contracts/bitable_schema.md。
生产业务、收件及会话以COS提交为准；Bitable的pending_tasks.payload用于设置归档和旧状态迁移。

## 云函数部署

Bash 构建脚本需 Linux/WSL 环境及其 Python 依赖；本机 Windows venv 不用于 Linux 构建。

```bash
PYTHON=python3.10 bash deploy/build_zip.sh
```

包或依赖变更需要装配验收时，在 Linux Python 3.10 运行以下命令。`--package` 指向待验业务 ZIP，`--layer` 指向本次使用的冻结 Layer ZIP。

```bash
python3.10 deploy/check_package.py --package /path/to/candidate.zip --layer /path/to/frozen-layer.zip --output reports/package-check.json
```

检查从两个 ZIP 临时解压加载，隔离仓库源码和本机 site-packages，记录业务包和 Layer 的 SHA256、业务包 BUILD_ID 和索引 INDEX_VERSION。该检查证明装配和启动。业务证据按[交流体验规格](../CONVERSATION_EXPERIENCE_SPEC.md#6-验收判据与交付)选取。`production_live.py` 的 `report` 阶段在最终门槛未满足时返回非零，补证后可直接重算报告。

本机日常更新可直接调用腾讯云 API。部署凭据在仓库外 `%USERPROFILE%\.config\calorie-agent\tencentcloud-credentials.csv`，CSV 列为 `SecretId`、`SecretKey`；文件只允许本机用户和 SYSTEM 访问。不要回显密钥或将文件复制进仓库、报告、部署包。

PowerShell 中把加载语句与后续 API 命令放在同一次脚本／终端调用中；变量仅对当前进程及其子进程生效，另一次工具调用需重新加载。下面示例只查询函数，不部署：

```powershell
$deployCredential = Import-Csv -LiteralPath (Join-Path $env:USERPROFILE '.config\calorie-agent\tencentcloud-credentials.csv')
$env:TENCENTCLOUD_SECRET_ID = $deployCredential.SecretId
$env:TENCENTCLOUD_SECRET_KEY = $deployCredential.SecretKey
Remove-Variable deployCredential
python deploy/production_release.py inspect --function calorie-agent-reminder
```

执行部署时，在同一次 PowerShell 调用中加载凭据并设置待发包路径。`$releasePackage` 使用发布记录中已验收且获准上传的 ZIP。

```powershell
$releasePackage = 'C:\path\to\approved-candidate.zip'
```

按实际改动更新受影响函数：受理 Web 函数 `calorie-agent-api-v3-test`、消息 Event 函数 `calorie-agent-reminder`、归档 Event 函数 `calorie-agent-archive`。状态兼容的更新沿用以下步骤：

1. 以发布记录的 BUILD_ID 和 SHA256 为准，用 `Get-FileHash -LiteralPath $releasePackage -Algorithm SHA256` 核对待上传文件一次。检查受影响函数，将原包及私有配置备份到仓库外。
2. 用 `production_release.py maintenance-on --function calorie-agent-api-v3-test` 暂停新受理，核对未完收件及未知提交，保留原任务。
3. 向受影响函数显式上传同一冻结包，例如 `python deploy/api_deploy.py --function calorie-agent-reminder $releasePackage`。保留角色、前缀、环境、触发及所需 Layer。消息 handler 为 app.message_handler，归档为 app.maintenance_handler。每个已受理更新仅轮询至 Active。
4. 每个更新的函数下载一次实际代码，核对包身份和所需配置，记录结果。下载 ZIP 的 SHA256 与原包一致即确认代码身份。HTTP 平台重封装导致整体哈希不同时，用同一份下载文件核对 BUILD_ID 及全部成员内容。代码核对结果沿用到本次发布结束，只有代码版本发生变化或出现具体疑点才复查。
5. 核对通过后用 `production_release.py maintenance-off --function calorie-agent-api-v3-test` 恢复受理。确认受理已恢复、health 返回发布记录中的 BUILD_ID 且回调鉴权正常后结束。更新后需要回退时，先确认受理已暂停，再恢复原包和配置并核对，最后恢复受理。

脚本从环境读取 TENCENTCLOUD_SECRET_ID／TENCENTCLOUD_SECRET_KEY，临时凭据另需 TENCENTCLOUD_SESSION_TOKEN；SCF_REGION 默认 ap-shanghai。资源及首次切换记录见 [生产计划](../deploy/cos-production-plan.json)。该计划及 `cloudshell_cutover.py` 保留首次迁移恢复用途；日常更新按上述发布步骤，不重跑首次迁移、COS维护回收或未受影响的候选验收。迁移接口见 [Bitable 契约](../contracts/bitable_schema.md#旧-bitable-状态与迁移)。

## 知识维护与发布

日常知识维护在 Bitable 表内编辑。初次建表可用 `python deploy/seed_knowledge.py` 同步内置条目。

在 Linux Python 3.10 环境构建，复用经 SHA256 核验的已发布 Layer 依赖：

```bash
export RAG_BASE_LAYER="$PWD/reports/production-candidate/knowledge-source-fix/rag-layer-sourcefix-20261102-69ca0bd3e30f6317.zip"
export RAG_BASE_LAYER_SHA256="3ac484a780b3adff15d2c2c625a67ac4fd05247994dc1ee6193071e9f7662936"
export RAG_INDEX_SOURCE="$PWD/deploy/rag_index/$(date -u +%Y%m%d%H%M%S)"
export RAG_LAYER_OUTPUT="$PWD/deploy/rag-layer-$(date -u +%Y%m%d%H%M%S).zip"
python3.10 deploy/ingest_knowledge.py --base-layer "$RAG_BASE_LAYER" --base-sha256 "$RAG_BASE_LAYER_SHA256" --output "$RAG_INDEX_SOURCE"
PYTHON=python3.10 bash deploy/build_rag_layer.sh
python deploy/deploy_rag_layer.py --function calorie-agent-reminder "$RAG_LAYER_OUTPUT"
```

索引默认读取 Bitable；显式 `--source seed` 用于本地样本。索引和输出 ZIP 均使用新路径，构建校验文档、块、向量及图来源。已有索引可用 `python deploy/ingest_knowledge.py --output INDEX --check-only` 检查；复检已有索引时，在该命令中加 `--base-layer`、`--base-sha256`、`--query` 和 `--expect-text`；`--reject-text` 可指定不应出现的文本。

Layer 发布保留目标函数原环境变量和其他层，设置 KNOWLEDGE_RETRIEVER=lightrag；运行时把 /opt 只读索引镜像到 /tmp。FAQ 读取已发布 COS 知识快照，更新说明见 [知识契约](../contracts/knowledge_schema.md)。
