# Calorie Agent

把低摩擦饮食记录、可信 AI 工具调用和产品验证闭环放进飞书会话。

`Calorie Agent` 是一个面向飞书机器人的自然语言饮食记录 Agent。用户在飞书里输入“鸡胸肉 50g”“早餐鸡蛋100g，中午米饭200g，查下今天还剩多少”，系统会完成意图理解、工具执行、饮食记录、今日查询、撤销、缺失信息追问和结果回复。

它不是一个通用健康 App，也不是让大模型自由计算热量的聊天机器人。

它更像一套可验证的 AI Agent 产品链路：飞书负责低摩擦入口，AgentRuntime 负责规划和编排，后端工具与数据库负责事实读写，Reply Guard 和 Eval 负责控制 AI 风险。

<p align="center">
  <strong>语言</strong>：
  <a href="./README.md">简体中文</a> |
  <a href="./README.en.md">English</a>
</p>

## 快速导航

- [简介](#简介)
- [为什么需要它](#为什么需要它)
- [适合谁](#适合谁)
- [它能做什么](#它能做什么)
- [效果示例](#效果示例)
- [核心能力](#核心能力)
- [和普通热量记录或 Chatbot 有什么不同](#和普通热量记录或-chatbot-有什么不同)
- [当前接入方式](#当前接入方式)
- [工作原理](#工作原理)
- [为什么可信](#为什么可信)
- [仓库结构](#仓库结构)
- [边界说明](#边界说明)
- [许可证](#许可证)

## 简介

这个仓库当前提供的是飞书饮食记录 Agent 的后端、Agent Runtime、工具层、评测集、部署脚本和产品文档材料。

项目的核心目标不是“让模型回答饮食问题”，而是把高频饮食记录流程拆成可审查、可回放、可评测的工程链路：

- `Feishu Webhook`：接收用户消息，快速 ACK，降低飞书重试和云函数超时风险。
- `AgentRuntime`：让 DeepSeek 只做理解、规划和回复组织。
- `Tools`：执行记录、查询、撤销、报告、提醒、记忆等后端动作。
- `Storage`：用 Bitable 兼容旧路径，V3 用 MySQL 作为主业务数据库方向。
- `Reply Guard`：校验回复中的数字、状态和权限边界，避免模型编造事实。
- `Eval`：用离线回归和 Agent 行为评测覆盖主链路与高风险异常。

V3 Beta 的产品口径是：飞书是入口，MySQL 是主业务数据库方向，Bitable 保留兼容和回滚，DeepSeek 不负责营养事实计算。

## 为什么需要它

饮食记录产品的难点通常不是“功能能不能做”，而是用户是否愿意长期记录。

传统记录路径往往要求用户打开 App 或表单、搜索食物、估算克重、确认营养数据，再回到原来的工作流。对减脂、健身、控糖和日常健康管理用户来说，这个路径在高频场景里很容易中断。

`Calorie Agent` 选择把入口放进飞书会话，解决几个具体问题：

- 用户可以直接用自然语言记录，不需要切换到独立 App。
- 复合消息可以被拆成多个工具动作，例如先记录多餐，再查询今日剩余。
- 缺克重、食物歧义和未知食物不会被模型硬猜，而是进入 pending 追问。
- 飞书重试、云函数超时和重复消息通过幂等机制控制。
- 营养数值来自食物库、摄入快照和后端计算，不来自模型自由生成。
- 产品是否值得继续投入，可以通过 usage、feedback、retention 和 eval 指标判断。

## 适合谁

这个项目适合：

- 想验证低摩擦饮食记录场景的 AI 产品经理或独立开发者。
- 想研究 AI Agent 工具调用、状态管理、异常闭环和评测体系的人。
- 想把聊天入口接到真实业务数据，而不是只做 Demo 对话的人。
- 需要理解飞书机器人、腾讯云函数、Bitable/MySQL 和 LLM 编排边界的人。

它不太适合：

- 需要拍照识别、商城社区、完整健康管理 App 或商业化控制台的场景。
- 需要医疗诊断、个性化治疗建议或严肃医学结论的场景。
- 希望让 LLM 直接计算营养值、直接写库或绕过工具层的实现方式。
- 不需要幂等、权限隔离、异常恢复和评测门禁的轻量聊天机器人。

## 它能做什么

当前仓库中已经沉淀的能力包括：

| 能力 | 说明 | 关键材料 |
|---|---|---|
| 飞书文本入口 | 接收飞书机器人文本事件，快速返回 accepted/queued，并继续后续处理 | [Tencent SCF README](backend/tencent_scf/README.md) |
| 自然语言饮食记录 | 支持已知食物、多食物消息、复合消息和今日汇总 | [后端 README](backend/README.md) |
| 查询与撤销 | 支持今日查询、摄入快照查询、撤销上一条或语义撤销 | [测试用例](backend/docs/test-cases.md) |
| 缺失信息追问 | 缺克重、未知食物、低置信候选进入 pending 流程 | [测试用例](backend/docs/test-cases.md) |
| Agent 工具编排 | DeepSeek 产出 action plan，后端只执行白名单工具 | [Agent Runtime v2](backend/docs/agent-runtime-v2.md) |
| 事实型回复校验 | Reply Guard 用 execution facts 校验最终回复 | [测试用例](backend/docs/test-cases.md) |
| 多用户与 MySQL 方向 | V3 身份层解析 tenant/user，MySQL repository 提供主库方向 | [飞书多租户](backend/docs/v3-feishu-tenant.md), [MySQL Schema](backend/docs/v3-mysql-schema.md) |
| 产品指标闭环 | usage、feedback、activation、retention 和 P0/P1/P2 规则 | [产品验证](backend/docs/v3-product-validation.md) |
| 离线评测门禁 | 11 套验证脚本覆盖 regression、Agent Eval、Reply Guard、Planner、Memory 等 | [测试用例](backend/docs/test-cases.md) |

## 效果示例

用户输入：

> 早餐鸡蛋100g，中午米饭200g鸡胸肉150g，查下今天还剩多少

系统不应该直接让模型“算一个答案”，而是按可审查链路处理：

1. 飞书事件进入 `/webhook/feishu/events`。
2. Webhook 校验、解析、幂等处理，并快速返回 accepted/queued。
3. AgentRuntime 让 DeepSeek 生成结构化 action plan。
4. Tool Executor 顺序执行多个 `record_food` 动作，再执行 `query_today`。
5. 食物匹配、克重、kcal、carbs、protein、fat 来自工具和数据表。
6. Reply Guard 检查最终回复，数字必须能追溯到 execution facts。
7. 飞书会话内返回自然语言结果。
8. metrics、diagnostics 和可选 trace 记录本次链路。

核心图表：

| 图表 | 链接 |
|---|---|
| 整体架构 | [fig_2_1_architecture.png](dv_document/prd_work/figures/fig_2_1_architecture.png) |
| 端到端泳道 | [fig_4_1_swimlane.png](dv_document/prd_work/figures/fig_4_1_swimlane.png) |
| 异步处理时序 | [fig_5_1_sequence.png](dv_document/prd_work/figures/fig_5_1_sequence.png) |
| Job 状态机 | [fig_5_2_job_state.png](dv_document/prd_work/figures/fig_5_2_job_state.png) |
| MySQL 数据关系 | [fig_6_1_er.png](dv_document/prd_work/figures/fig_6_1_er.png) |

![Calorie Agent architecture](dv_document/prd_work/figures/fig_2_1_architecture.png)

## 核心能力

`Calorie Agent` 的核心不是把回复写得更像人，而是把 AI Agent 的关键风险显式控制住。

- **低摩擦入口**：用户在飞书里用自然语言完成记录、查询、撤销和追问补全。
- **工具边界**：LLM 只负责理解、规划和表达，不直接写库、不直接查询、不计算营养事实。
- **幂等写入**：用 `message_id`、`idempotency_key` 和 per-action key 防止飞书重试导致重复摄入。
- **缺失信息闭环**：缺克重、未知食物、歧义候选进入 pending，而不是让模型猜。
- **软删除撤销**：撤销把摄入记录标记为 `deleted`，今日汇总排除该记录，同时保留审计空间。
- **多用户隔离**：V3 使用 `tenant_id + user_id` 作为数据边界，同一飞书 open_id 在不同 app 下隔离。
- **事实型回复**：Reply Guard 阻断编造数字、错误完成状态、权限泄露和医疗诊断类表达。
- **评测门禁**：本地回归和 Agent Eval 覆盖 happy path、重复消息、误删、pending、幻觉数字和工具失败。
- **产品验证**：以激活、留存、有效反馈、失败用户数和指标事件判断下一步投入优先级。

## 和普通热量记录或 Chatbot 有什么不同

| 对比项 | 普通热量记录 App | 普通 Chatbot | Calorie Agent |
|---|---|---|---|
| 入口 | 打开 App 或表单 | 对话窗口 | 飞书会话内自然语言 |
| 数据来源 | 用户手填或 App 数据库 | 模型回答为主 | 工具层和数据库事实 |
| 多动作处理 | 通常需要多步操作 | 容易自由发挥 | action plan + 白名单工具 |
| 错误处理 | 多为表单校验 | 常缺少状态闭环 | pending、retry、dead letter、diagnostics |
| 重复消息 | 通常不是核心问题 | 很少处理 | 幂等 key 和 duplicate skip |
| 撤销 | UI 操作 | 语义不稳定 | 软删除 + 候选定位 + 权限边界 |
| AI 风险 | AI 参与较少 | 容易编造事实 | Reply Guard + execution facts |
| 验证方式 | 功能测试为主 | 对话样例为主 | regression + agent eval + metrics |

## 当前接入方式

当前项目以飞书机器人和腾讯云函数为主要运行形态。本地最适合做离线验证、回归测试和 SCF 包构建。

### 1. 安装后端依赖

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

V3 MySQL repository 是 opt-in 能力。如需打包腾讯云函数依赖，还需要安装 `backend/tencent_scf/requirements.txt` 中的 PyMySQL 到 SCF 依赖目录，或使用现有打包流程。

### 2. 运行离线验证

```powershell
cd backend
.\.venv\Scripts\python.exe tests\run_regression.py
.\.venv\Scripts\python.exe tests\run_agent_eval.py
.\.venv\Scripts\python.exe tests\run_reply_guard_v2_eval.py
.\.venv\Scripts\python.exe tests\run_v2_agent_eval.py
```

完整验证命令见 [backend/docs/test-cases.md](backend/docs/test-cases.md)。这些离线测试使用 fake tables、fixtures 或 fake planner response，不会调用真实飞书、DeepSeek、腾讯云或 Bitable API。

### 3. 配置腾讯云函数

核心入口：

```text
GET  /health
POST /webhook/feishu/events
POST /tasks/daily-cleanup?token=<ROLLOVER_TASK_TOKEN>
POST /tasks/reminders?token=<ROLLOVER_TASK_TOKEN>
```

核心环境变量见 [backend/.env.example](backend/.env.example) 和 [Tencent SCF README](backend/tencent_scf/README.md)。

必填变量包括：

```env
PORT=9000
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_VERIFICATION_TOKEN=
DEEPSEEK_API_KEY=
ROLLOVER_TASK_TOKEN=
BITABLE_APP_TOKEN=
BITABLE_FOOD_TABLE_ID=
BITABLE_INTAKE_TABLE_ID=
BITABLE_PENDING_TABLE_ID=
```

### 4. 构建腾讯云函数包

```powershell
cd backend
.\.venv\Scripts\python.exe scripts\build_scf_zip.py
```

输出包：

```text
backend/dist/calorie-agent-api-tencent-scf.zip
```

不要用 Windows 右键压缩或 PowerShell `Compress-Archive` 直接打包 SCF 文件；SCF zip 根目录和 Linux-style 路径规则见 [release checklist](backend/docs/release-checklist.md)。

## 工作原理

`Calorie Agent` 的核心原则是：**模型做规划，工具做事实，评测做门禁。**

主流程可以理解为：

```text
Feishu message
  -> Webhook verification / parse / idempotency
  -> quick ACK
  -> AgentRuntime planner
  -> whitelisted tool execution
  -> storage read/write
  -> Reply Guard
  -> Feishu reply
  -> metrics / diagnostics / eval feedback
```

### Agent 事实边界

- DeepSeek 不计算 `kcal`、`carbs`、`protein`、`fat`。
- DeepSeek 不直接写数据库、不直接执行查询、不直接撤销。
- 食物模糊匹配只能返回候选；低置信或多候选必须追问。
- 缺克重、缺营养字段必须进入 pending 或追问。
- 回复中的事实数字必须来自工具结果或摄入快照。

### V3 数据方向

V3 将 MySQL 作为主业务数据库方向，关键表包括：

- `tenants`
- `users`
- `foods`
- `food_aliases`
- `intake_records`
- `standards`
- `memories`
- `usage_events`
- `feedback_samples`

其中 `intake_records` 保存食物名、克重和营养快照，历史查询读取当时快照，不用当前食物库重新计算。详见 [V3 MySQL Schema](backend/docs/v3-mysql-schema.md)。

### 评测与发布门禁

本项目把 Agent 风险拆成可测试场景，而不是只看聊天样例。当前文档中记录的离线验证覆盖：

- 63 个 regression scenarios
- 22 个 agent eval cases
- 12 个 reminder eval cases
- 10 个 skill registry eval cases
- 10 个 context loader eval cases
- 14 个 planner profile eval cases
- 12 个 memory learner eval cases
- 12 个 reply guard v2 eval cases
- 15 个 v2 agent eval cases
- 13 个 dynamic planner eval cases
- 10 个 intake nutrition query eval cases

这些数字来自项目离线验证集，不应被包装成真实线上用户数据。

## 为什么可信

这个项目的可信度主要来自仓库内可阅读、可运行、可复核的材料，而不是口头描述。

| 文件 | 说明 |
|---|---|
| [backend/README.md](backend/README.md) | 后端能力、重要文件、回归验证和部署入口 |
| [backend/tencent_scf/README.md](backend/tencent_scf/README.md) | 腾讯云函数部署、环境变量、飞书配置、日志排查 |
| [backend/docs/test-cases.md](backend/docs/test-cases.md) | 本地回归、Agent Eval、Reply Guard、Memory、Reminder 等验证集 |
| [backend/docs/agent-runtime-v2.md](backend/docs/agent-runtime-v2.md) | Agent Runtime v2 的目标、边界、模块职责和迁移策略 |
| [backend/docs/v3-mysql-schema.md](backend/docs/v3-mysql-schema.md) | V3 MySQL 数据层、幂等策略、隔离策略和环境变量 |
| [backend/docs/v3-feishu-tenant.md](backend/docs/v3-feishu-tenant.md) | 飞书接入、多租户身份解析和用户接入流程 |
| [backend/docs/v3-product-validation.md](backend/docs/v3-product-validation.md) | Beta 用户目标、激活/留存/反馈口径和优先级规则 |
| [backend/docs/release-checklist.md](backend/docs/release-checklist.md) | 发布冻结、回归门禁、云端冒烟、回滚步骤 |
| [dv_document/prd_work/飞书饮食记录Agent_V3_Beta_PRD.md](dv_document/prd_work/飞书饮食记录Agent_V3_Beta_PRD.md) | V3 Beta PRD 源稿索引与固定口径 |

## 仓库结构

当前仓库中与项目主线最相关的结构如下：

```text
.
├── backend/
│   ├── app/
│   │   ├── config.py
│   │   ├── feishu/
│   │   └── services/
│   ├── docs/
│   │   ├── agent-runtime-v2.md
│   │   ├── test-cases.md
│   │   ├── v3-feishu-tenant.md
│   │   ├── v3-mysql-schema.md
│   │   └── v3-product-validation.md
│   ├── scripts/
│   │   └── build_scf_zip.py
│   ├── tencent_scf/
│   │   ├── app.py
│   │   ├── scf_bootstrap
│   │   ├── requirements.txt
│   │   └── calorie_agent/
│   │       ├── agent/
│   │       ├── context/
│   │       ├── domain/
│   │       ├── identity/
│   │       ├── memory/
│   │       ├── product_metrics/
│   │       ├── skills/
│   │       ├── storage/
│   │       └── tools/
│   ├── tests/
│   ├── requirements.txt
│   └── README.md
├── dv_document/
│   └── prd_work/
│       ├── figures/
│       └── 飞书饮食记录Agent_V3_Beta_PRD.md
├── v2-plan/
├── v3-plan/
├── README.md
└── README.en.md
```

关键目录说明：

- `backend/tencent_scf/calorie_agent/agent/`：AgentRuntime、planner、tool executor、reply guard。
- `backend/tencent_scf/calorie_agent/domain/`：撤销、pending、报告、提醒、异常、食物匹配等业务逻辑。
- `backend/tencent_scf/calorie_agent/storage/`：V3 MySQL schema、repository 和连接层。
- `backend/tencent_scf/calorie_agent/identity/`：V3 飞书身份解析和 tenant/user 上下文。
- `backend/tests/`：离线回归、Agent Eval、Reply Guard、Memory、Planner 等验证脚本。
- `dv_document/prd_work/`：PRD、架构图、泳道图、时序图、状态机和 ER 图。

## 边界说明

当前项目更适合作为 AI Agent 产品验证与后端链路项目，而不是完整商业化应用。

已知边界：

- 当前主入口是飞书机器人，不是独立 App 或通用健康平台。
- V3 MySQL 是主业务数据库方向，但旧 Bitable 路径仍保留兼容和回滚。
- V2/V3 的部分能力采用 feature flag、shadow、read-only 或 opt-in 策略，不应默认理解为全部生产接管。
- 项目不做拍照识别、商城社区、完整控制台、教练端和复杂商业化权限体系。
- 项目不提供医疗诊断或治疗建议，所有营养信息应被视为记录和管理辅助。

如果你的目标只是一次性聊天问答，直接做一个轻量 Bot 可能更简单。这个项目的价值在于把 Agent 主链路、数据边界、异常恢复和评测闭环一起设计出来。

## 许可证


