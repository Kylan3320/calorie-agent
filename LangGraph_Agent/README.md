# Calorie Agent

飞书饮食记录助手：记录、查询、确认撤销、追问补全、历史聚合及知识问答。
需求见 [REQUIREMENTS.md](REQUIREMENTS.md)，协作与验证见 [AGENTS.md](AGENTS.md)，数据接口见 [contracts/](contracts/)。

个人饮食管理行为及最小验收见 [PERSONAL_CALORIE_SPEC.md](PERSONAL_CALORIE_SPEC.md)，实际交付证据见 [reports/PERSONAL_DELIVERY.md](reports/PERSONAL_DELIVERY.md)。

交流体验改进的本地实现及离线验收已完成，最终发布验收待完成。行为及最小验收见 [CONVERSATION_EXPERIENCE_SPEC.md](CONVERSATION_EXPERIENCE_SPEC.md)，实际构建、线上结果及待办见 [reports/CONVERSATION_DELIVERY.md](reports/CONVERSATION_DELIVERY.md)。

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

后端监听 http://127.0.0.1:8765。真实表格访问需配置账号和飞书环境变量；别名按 D25 归一。
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
- 本地真实表格账号：CONSOLE_USER_ID。轨迹可设置 TRACE_PATH。

PRESET、KNOWLEDGE_RETRIEVER 优先于 YAML；model、timezone_offset_hours 优先使用 YAML，环境变量仅作缺省值。
每日目标及表字段见 contracts/bitable_schema.md。
当前代码依赖 pending_tasks 表的 `payload` 文本列；Bitable live 写入及快速 ACK 依赖该表。
旧确认任务没有固定目标时会被拒绝，取消后重新发起即可。

## 云函数部署

Bash 构建脚本需 Linux/WSL 环境及其 Python 依赖；本机 Windows venv 不用于 Linux 构建。

```bash
PYTHON=python3.10 bash deploy/build_zip.sh
python deploy/api_deploy.py                 # 默认 deploy/calorie-agent-v4.zip
```

SCF 使用 Web 函数、Python 3.10、端口 9000、scf_bootstrap。
设置 TENCENTCLOUD_SECRET_ID / TENCENTCLOUD_SECRET_KEY；SCF_FUNCTION 指定目标函数，SCF_REGION 默认 ap-shanghai。
部署入口上传后显示函数状态；发布验收检查涉及的能力及配置，写入变更再验证对应落表事实。
如环境变量仍呈旧值，核对实例和发布状态，必要时重新上传代码刷新实例。

飞书请求先保存收件事实再 ACK，后台按同会话收件顺序处理；处理/发送失败最多尝试两次。
持续失败或进程退出后，配置相同环境运行 `python app.py --retry-pending` 恢复未完成的飞书收件；
该命令恢复已存原生工具调用及原回复，沿用原日期及防重键，不处理 DSH/CLI 任务，不新增定时调度。
恢复前查看日志中的错误并修复配置或服务故障；本地 FakeStore 的状态不跨进程保留。

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


## Personal 升级运行与发布

CLI、飞书、DSH /calorie共用LangChain v1 create_agent，DeepSeek模型不变，不再提供deterministic/AgentPlan入口。CLI默认假写存储，交互进程共享会话，需要DEEPSEEK_API_KEY。DSH保持/dsh引擎，/calorie按session_id隔离；控制台仅127.0.0.1。

必要验收：`python harness/eval/runner.py --case U01,U02,U03,U04,U05,U06,U07`，使用独立FakeStore、假模型/薄荷/时钟，不访问真实表。不要把旧smoke或全量基线作为本次发布门槛。

Linux Python3.10构建：`PYTHON=/path/to/python3.10 bash deploy/build_zip.sh`，包内BUILD_ID写入health。构建检查必须启动该包并用当前生产LightRAG Layer检查Agent、检索器和提醒入口可加载。打包仅app.py/agent/config.yaml/scf_bootstrap/requirements.txt/BUILD_ID/deps，排除console/DSH/报告/构建目录。

固定目标ap-shanghai/default/calorie-agent-api-v3-test，保留飞书cli_aabc166fccb8dcef现有webhook。新增Event函数calorie-agent-reminder复用同包，入口`app.reminder_handler`，每分钟cron `0 * * * * * *`，按事件计划Time及项目+8时区匹配；默认无启用用户规则。主函数至少120秒、端口9000，维持单实例串行接收，避免跨实例同时更新会话状态。

切换前旧入口收尾已收件/待发送任务；`agent.storage.migration.ensure_schema(store)`仅添加缺列，`migrate_legacy_pending(store,dry_run=False)`仅迁移等待回答的事实并关闭旧任务。执行日志保存于现有pending表；上线后`python app.py --retry-pending`显式恢复执行/发送，最终回复与uuid不变。三预设和字段/状态见contracts。

线上按Spec完成指定对话组，核对真实回复、落表、原生工具轨迹和LightRAG检索，最后仅软删除该次新增摄入、移除测试常用餐、恢复变动设置。构建/离线/线上结果见reports/PERSONAL_DELIVERY.md。
