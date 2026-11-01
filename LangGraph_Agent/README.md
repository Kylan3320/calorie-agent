# Calorie Agent

飞书饮食记录助手：记录、查询、确认撤销、追问补全、历史聚合及知识问答。
需求见 [REQUIREMENTS.md](REQUIREMENTS.md)，协作与验证见 [AGENTS.md](AGENTS.md)，数据接口见 [contracts/](contracts/)。

个人饮食管理行为及最小验收见 [PERSONAL_CALORIE_SPEC.md](PERSONAL_CALORIE_SPEC.md)，实际交付证据见 [reports/PERSONAL_DELIVERY.md](reports/PERSONAL_DELIVERY.md)。

交流行为见 [CONVERSATION_EXPERIENCE_SPEC.md](CONVERSATION_EXPERIENCE_SPEC.md)，历史交付记录见 [reports/CONVERSATION_DELIVERY.md](reports/CONVERSATION_DELIVERY.md)。后续任务按有效规格和改动影响选择验证，规则见 [AGENTS.md](AGENTS.md)。

飞书＋COS 已迁移并启用，存储、提交与同步规则及验收要求见 [LATENCY_OPTIMIZATION_SPEC.md](LATENCY_OPTIMIZATION_SPEC.md)。实际构建、逐项验收状态及未解决项统一见 [LATENCY_DELIVERY.md](reports/LATENCY_DELIVERY.md)，启用不代表全部验收通过。[COS 验证记录](reports/COS_PROBE.md) 保留探针历史，不是后续任务清单。

## 本地运行

本地控制台使用 Python 3.12+；云函数运行时为 Linux Python 3.10。

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe cli.py "记录米饭200g"
.\.venv\Scripts\python.exe harness/eval/runner.py --case U01
```

CLI 默认 FakeStore，仍需配置 DeepSeek 凭据并联网调用模型；交互模式保留同一会话状态。
Linux/WSL 下使用 `.venv/bin/python`。评测支持 --smoke、--case ID 和全量；范围按 AGENTS.md 选择。

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

## 环境配置

凭据和表 ID 从进程环境读取，不依赖临时 JSON 文件：

- 飞书：FEISHU_APP_ID、FEISHU_APP_SECRET、FEISHU_VERIFICATION_TOKEN、BITABLE_APP_TOKEN。
- 表：BITABLE_FOOD_TABLE_ID、BITABLE_INTAKE_TABLE_ID、BITABLE_STANDARD_TABLE_ID、BITABLE_PENDING_TABLE_ID、BITABLE_USAGE_TABLE_ID、BITABLE_KNOWLEDGE_TABLE_ID；可选 BITABLE_ALIASES_TABLE_ID、USER_ID_ALIASES。
- 模型与数据源：DEEPSEEK_API_KEY、BOOHEE_API_KEY；LightRAG 使用 SILICONFLOW_API_KEY。
- 生产COS：COS_BUCKET、COS_PREFIX、COS_REGION（上海）；SCF运行角色注入临时凭据，按每次请求／事件刷新。
- 受理与worker：CALORIE_MESSAGE_FUNCTION。CLI／DSH及定时提醒HTTP受理使用 CALORIE_SERVICE_URL、CALORIE_SERVICE_TOKEN；本地本人身份 CLI_USER_ID／CONSOLE_USER_ID。候选CAM认证入口配置 CALORIE_SERVICE_AUTH=cam 和 CALORIE_CLOUD_UIN，内部服务令牌独立传输；正式现有入口使用Bearer。令牌仅配置在私有环境，不能写入包。
- 轨迹：可设置 TRACE_PATH。

PRESET、KNOWLEDGE_RETRIEVER 优先于 YAML；model、timezone_offset_hours 优先使用 YAML，环境变量仅作缺省值。
每日目标及表字段见 contracts/bitable_schema.md。
生产业务、收件及会话以COS提交为准；Bitable的pending_tasks.payload用于设置归档和旧状态迁移。
旧确认任务没有固定目标时会被拒绝，取消后重新发起即可。

## 云函数部署

Bash 构建脚本需 Linux/WSL 环境及其 Python 依赖；本机 Windows venv 不用于 Linux 构建。

```bash
PYTHON=python3.10 bash deploy/build_zip.sh
python deploy/api_deploy.py                 # 默认 deploy/calorie-agent-v4.zip
```

生产分为受理Web函数、消息Event函数和独立归档Event函数，三者各平台并发1；Python3.10、上海私有COS。消息worker是业务及会话唯一写入者，Bitable等待不占消息链路。受理持久化且异步调用接受后才ACK，失败503；消息最多自动尝试3次，原日期、动作、累计模型预算和回复正文在恢复时保留。每5分钟维护先补查收件，再归档；每分钟提醒经同一受理保存原计划时间。

具体函数、角色权限、触发器及当次迁移步骤见 [部署计划](deploy/cos-production-plan.json)。当次授权、迁移和切换已执行，实际结果见交付记录；后续发布按变更影响执行，不自动重复正式基线迁移。

```bash
# 当次生产接入的参考命令，不是后续任务的待办清单。
python deploy/production_release.py freeze
python deploy/production_release.py inspect --function calorie-agent-api-v3-test
python deploy/migrate_cos.py export --snapshot reports/production-baseline.json --audit reports/production-source-audit.json
# 完成维护排空、独立导入核对和候选验收后，才按部署计划切换。
```

发布脚本从环境读取 TENCENTCLOUD_SECRET_ID／TENCENTCLOUD_SECRET_KEY，SCF_FUNCTION指定目标，SCF_REGION默认ap-shanghai。迁移保留原ID、日期、数值、软删除、未完事项和幂等／投递状态。COS有新业务提交后只能回退到兼容当前提交格式的构建。实际完成情况及未解决项见 [LATENCY_DELIVERY.md](reports/LATENCY_DELIVERY.md)。

## 知识维护与发布

生产表内维护知识；seed 初始化/更新指定条目：`python deploy/seed_knowledge.py`。

```bash
# 使用本次版本的新空目录，保留旧索引；构建环境需 lightrag-hku、httpx。
export RAG_INDEX_SOURCE="$PWD/deploy/rag_index/$(date -u +%Y%m%d%H%M%S)"
python deploy/ingest_knowledge.py --output "$RAG_INDEX_SOURCE"
./deploy/build_rag_layer.sh
python deploy/deploy_rag_layer.py deploy/rag-layer.zip
```

索引默认从 Bitable 读取；显式 --source seed 用于本地样本构建。
构建拒绝非空目录和生产伪向量；Layer 发布读取现有函数配置，保留原环境变量及其他层。
生产 Layer 设置 KNOWLEDGE_RETRIEVER=lightrag；仓库默认 faq，切换不代表自动错误回退。
Layer 依赖位于 zip 根目录，保留内部 parser 依赖；/opt 只读索引在运行时镜像到 /tmp。

## 目录

agent/：图、工具、策略与存储；console/：本地控制台及 DSH 驱动；
harness/eval/：统一评测入口；deploy/：构建及发布；contracts/：跨模块契约。


## 历史记录

COS接入前的真实运行、部署及验收证据保留在 [PERSONAL_DELIVERY.md](reports/PERSONAL_DELIVERY.md) 和 [CONVERSATION_DELIVERY.md](reports/CONVERSATION_DELIVERY.md)。历史构建与Bitable恢复命令不适用于新的COS业务前缀；本轮以 [提速规格](LATENCY_OPTIMIZATION_SPEC.md) 和实际交付记录为准。
