# 运行与部署

命令从仓库根目录运行。控制台使用 Python 3.12+，云函数及构建使用 Linux／WSL 的 Python 3.10。

## 本地控制台与 DSH

```powershell
.\.venv\Scripts\python.exe -m pip install -r console/requirements.txt
$env:CONSOLE_USER_ID = "本人飞书open_id"
.\.venv\Scripts\python.exe -m console.server
```

后端监听 http://127.0.0.1:8765。live 所需服务地址、令牌和账号见[环境配置](#环境配置)。

在已安装并构建的 DSH 上配置驱动。

1. 设置 `DSH_REPO` 为 DSH 仓库绝对路径，用 Bash 运行 `console/dsh-plugin/calorie-agent-driver/setup-links.sh`。脚本检查 `ReactLoopAgent` 和 `turnBoundaryProjectionDefinition` 导出，升级 DSH 后重跑。
2. 安装本地 `calorie-agent-driver`，创建 `calorie` profile 并加载 `dsh-base`、`dsh-web-app`、`calorie-agent-driver`。
3. 设置 `DSH_HOME` 指向本仓库 `console/dsh-home`，在 DSH 仓库运行 `pnpm dsh --profile calorie`。

默认热量助手。`/dsh` 和 `/calorie` 切换后续新建会话的引擎。

## 运行评测

```powershell
.\.venv\Scripts\python.exe harness/eval/runner.py --list
.\.venv\Scripts\python.exe harness/eval/runner.py --case U01
```

Linux 或 macOS 使用 `.venv/bin/python`。`--case` 接受逗号分隔的 ID，`--all` 适用范围见 [AGENTS.md](../AGENTS.md#验证与交付)。`--real-model` 需显式选择 E 类场景，记录默认按 UTC 时间单独保存，`--output PATH` 须为新文件。

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
每日目标、表字段及旧状态格式见 [Bitable 契约](../contracts/bitable_schema.md)。

## 云函数部署

```bash
PYTHON=python3.10 bash deploy/build_zip.sh
```

依赖版本匹配时，`build_zip.sh --reuse-deps` 复用 `deploy/build`。

需要装配验收时指定本次业务 ZIP 和冻结 Layer ZIP。

```bash
python3.10 deploy/check_package.py --package /path/to/candidate.zip --layer /path/to/frozen-layer.zip --output reports/package-check.json
```

装配检查核对两个 ZIP 的 SHA256、BUILD_ID、INDEX_VERSION 和隔离启动。`production_live.py` 的 `report` 阶段判定账本三项、归档和清理。整包证据见[交流体验规格](../CONVERSATION_EXPERIENCE_SPEC.md#6-验收判据与交付)。

部署凭据位于 `%USERPROFILE%\.config\calorie-agent\tencentcloud-credentials.csv`，列为 `SecretId`、`SecretKey`，仅本机用户和 SYSTEM 可读。密钥不得回显或写入仓库、报告和部署包。

PowerShell 中在同一次调用内加载凭据和执行 API 命令。临时凭据另设 `TENCENTCLOUD_SESSION_TOKEN`，`SCF_REGION` 默认 `ap-shanghai`。以下示例查询消息函数。

```powershell
$deployCredential = Import-Csv -LiteralPath (Join-Path $env:USERPROFILE '.config\calorie-agent\tencentcloud-credentials.csv')
$env:TENCENTCLOUD_SECRET_ID = $deployCredential.SecretId
$env:TENCENTCLOUD_SECRET_KEY = $deployCredential.SecretKey
Remove-Variable deployCredential
python deploy/production_release.py inspect --function calorie-agent-reminder
```

部署时，将 `$releasePackage` 指向已验收且获准上传的 ZIP。

```powershell
$releasePackage = 'C:\path\to\approved-candidate.zip'
```

按实际改动更新受影响函数：受理 Web 函数 `calorie-agent-api-v3-test`、消息 Event 函数 `calorie-agent-reminder`、归档 Event 函数 `calorie-agent-archive`。状态兼容的更新沿用以下步骤：

1. 用 `python deploy/production_release.py freeze --package $releasePackage` 读取待上传包的 BUILD_ID 和 SHA256，与发布记录核对。检查受影响函数，将原包及私有配置备份到仓库外。
2. 用 `python deploy/production_release.py maintenance-on --function calorie-agent-api-v3-test` 暂停新受理，核对未完收件及未知提交，保留原任务。
3. 向受影响函数显式上传同一冻结包，例如 `python deploy/api_deploy.py --function calorie-agent-reminder $releasePackage`。保留角色、前缀、环境、触发及所需 Layer。消息 handler 为 app.message_handler，归档为 app.maintenance_handler。每个已受理更新仅轮询至 Active。
4. 每个更新的函数下载一次实际代码，记录包身份和配置核对结果。比较 ZIP 的 SHA256。HTTP 平台重封装导致哈希不同则比较同一份下载文件的 BUILD_ID 和全部成员内容。仅在代码版本变化或存在具体疑点时复查。
5. 核对通过后用 `python deploy/production_release.py maintenance-off --function calorie-agent-api-v3-test` 恢复受理，确认 health 的 BUILD_ID 和回调鉴权。回退时先暂停受理，再恢复并核对原包和配置，最后恢复受理。

[生产计划](../deploy/cos-production-plan.json)和 `cloudshell_cutover.py` 用于首次迁移恢复。日常更新按以上步骤，验收范围按 [AGENTS.md](../AGENTS.md#验证与交付)。迁移接口见 [Bitable 契约](../contracts/bitable_schema.md#旧-bitable-状态与迁移)。

## 收件恢复

归档函数 `app.maintenance_handler` 定期唤醒仍可自动恢复的收件。自动恢复耗尽或出现 `needs_attention` 时，先核对原收件、提交状态和交付状态。原因查明后，向同一数据域的消息函数提交原 `calorie_message` 事件，附 `manual_recovery=true`。沿用原 `receipt_id`、`ingress_revision`、`message_id`、`user_id` 和 `chat_id`。

## 知识维护与发布

日常知识维护在 Bitable 表内编辑。初次建表可用 `python deploy/seed_knowledge.py` 同步内置条目。

在 Linux Python 3.10 环境构建。用[当前部署记录](../reports/LATENCY_DELIVERY.md#当前部署)中的已发布 Layer ZIP 和 SHA256 替换下列示例值。

```bash
export RAG_BASE_LAYER=/path/to/published-layer.zip
export RAG_BASE_LAYER_SHA256=published-layer-sha256
export RAG_INDEX_SOURCE="$PWD/deploy/rag_index/$(date -u +%Y%m%d%H%M%S)"
export RAG_LAYER_OUTPUT="$PWD/deploy/rag-layer-$(date -u +%Y%m%d%H%M%S).zip"
python3.10 deploy/ingest_knowledge.py --base-layer "$RAG_BASE_LAYER" --base-sha256 "$RAG_BASE_LAYER_SHA256" --output "$RAG_INDEX_SOURCE"
PYTHON=python3.10 bash deploy/build_rag_layer.sh
python deploy/deploy_rag_layer.py --function calorie-agent-reminder "$RAG_LAYER_OUTPUT"
```

索引默认读取 Bitable；显式 `--source seed` 用于本地样本。索引和输出 ZIP 均使用新路径，构建校验文档、块、向量及图来源。已有索引可用 `python deploy/ingest_knowledge.py --output INDEX --check-only` 检查；复检已有索引时，在该命令中加 `--base-layer`、`--base-sha256`、`--query` 和 `--expect-text`；`--reject-text` 可指定不应出现的文本。

Layer 发布保留目标函数原环境变量和其他层，设置 KNOWLEDGE_RETRIEVER=lightrag；运行时把 /opt 只读索引镜像到 /tmp。FAQ 读取已发布 COS 知识快照，更新说明见 [知识契约](../contracts/knowledge_schema.md)。

## 阶段 C 候选维护

本分支不修改现有已批准生产角色策略、不创建函数、不部署、不切换正式前缀。隔离验证必须另有明确获准的 bucket／prefix、三域角色、计算函数、真实模型凭据与活动材料；当前 Cloud 缺口见 [交付记录](../reports/STAGE_C_DELIVERY.md)。`deploy/stage-c-isolated-plan.template.json` 仅列配置要求，模板不是资源授权。

凭据从仓库外按原手册加载。先对指定隔离域取预检，再准备目标：

```bash
python deploy/maintain_cos.py preflight --prefix "$COS_PREFIX" --task candidate-v2
python deploy/maintain_cos.py prepare --prefix "$COS_PREFIX" --task candidate-v2 --isolated
python deploy/maintain_cos.py switch --prefix "$COS_PREFIX" --task candidate-v2 --isolated
python deploy/maintain_cos.py resume --prefix "$COS_PREFIX" --task candidate-v2 --isolated
python deploy/maintain_cos.py verify --prefix "$COS_PREFIX" --task candidate-v2
python deploy/maintain_cos.py collection-plan --prefix "$COS_PREFIX" --task candidate-v2
```

`switch/resume` 使用现有 SCF 派发，三个函数必须事先具备候选代访问和固定控制键权限。消息角色确认当前写入后在本次调用中启动 watchdog，入口、sync 分别确认最终版本。无需等待用户补全；40 秒后只能争用取消，统一结果不可读时保持关闭。启用后不自动反向切换。配置 `CALORIE_COMPUTE_FUNCTION` 指向同包、并发 2、无 COS 权限的 `app.compute_handler`，旧三个角色保持并发 1。

实际新进程核对通过后，由获准维护者收束旧角色前缀权限；保留仍有效的旧角色凭据，在同一隔离域直接 PUT 验证 COS 返回 403。仓库外 JSON 文件将 ingress/message/sync 映射到各自旧凭据文件，文件含 bucket、region、secret_id、secret_key、可选 token，绝不提交或输出。只有此时执行：

```bash
python deploy/maintain_cos.py collect --prefix "$COS_PREFIX" --task candidate-v2 --old-role-envs "$OLD_ROLE_FILES" --isolated
```

该入口重新用只读进程读取目标代，并实际验证旧凭据拒写；任一角色未拒绝、Worker 签名仍有效、固定候选未对账或恢复未关闭，都拒绝删除。中断用同命令沿原清单续做。正式首次切换／回收需单独发布授权。自动维护默认关闭；`CALORIE_MAINTENANCE_VALIDATED=isolated-v2` 仅为隔离通过后的操作门，不能代替真实证据。

自动维护将已登记的取消清理／旧代回收优先于新阈值处理。隔离启用门之外，启用后的自动回收还要求仓库外 `CALORIE_RETIRED_ROLE_FILES` 指向上述三角色凭据映射；缺少时保留 `pending_permission_evidence`，不删除。消息 watchdog 恢复后在同次调用内补查原计划距当前至多 15 分钟的提醒。失败投递由 `calorie_reminder_recover` 沿原 UUID／累计尝试恢复；超出自动额度需明确人工恢复。
