# AGENTS.md — Calorie Agent V4 常读契约

单用户飞书饮食记录 Agent。Bitable（飞书多维表格）是唯一持久存储；LLM 只做理解和规划，
不计算营养、不直接读写存储；回复中的每个数字必须能追溯到工具执行事实。

## 每次开工必读

1. `REQUIREMENTS.md`（需求与语义仲裁）+ 本次改动涉及的 `contracts/` 契约。
2. 契约之间冲突时，仲裁顺序：REQUIREMENTS.md > contracts/ > 本文件 > 对话记忆。

## 目录所有权

- `agent/storage/`：数据访问唯一入口（feishu_client / bitable_adapter / fake_store / food_match）。
- `agent/tools/`：工具实现与注册表；`agent/planners/`：意图 → AgentPlan。
- `agent/nodes/`：LangGraph 图节点；`agent/policy/presets.py`：预设与风险分级。
- `harness/eval/`：评测集与 runner；`console/`：本地控制台（DSH 外壳 + Python 桥接后端，仅 127.0.0.1，Node/TS 依赖与构建产物不进云函数包）；`app.py`：云函数 webhook 入口；`cli.py`：本地入口。
- `contracts/`：跨域契约；谁改形状，谁在同一交付内更新契约。

## 硬规则（违反即返工）

- 图节点内禁止 preset / mode / candidate 版本分支；行为差异只允许来自
  `agent/policy/presets.py` 与 `config.yaml`。
- 回复中的每个数字必须来自 `tool_results`（execution facts），由 reply_guard 强制校验。
- 除 `agent/storage/` 外，任何模块不得直接发起 Feishu / Bitable HTTP 调用。
- 所有写入必须携带 `contracts/bitable_schema.md` 定义的幂等键，不得另造第三种幂等。
- pending / needs_confirmation 状态不得在回复中宣称"已记录"。
- 新增工具必须同时登记 `agent/schemas.py` 的 SUPPORTED_TOOLS、`agent/tools/registry.py`
  的注册表，并在 `harness/eval/cases.yaml` 至少补一个用例。

## Worker loop

- 首轮：读 REQUIREMENTS.md + 相关契约 → 改前先跑基线
  `python3 harness/eval/runner.py --smoke` → 实现 → 跑全量 `python3 harness/eval/runner.py`。
- 返回：改动文件清单 / eval 通过数 / 剩余风险；结论 `READY_FOR_REVIEW` 或
  `ESCALATION_REQUIRED`（附所需决定）。

## 维护协议（触发条件 → 同一交付内的动作）

- 改 Bitable 表字段或幂等键 → 更新 `contracts/bitable_schema.md`。
- 改 store 接口 → 更新 `contracts/tool_protocol.md`，并同步 bitable_adapter 与 fake_store 两个实现。
- 改 trace 字段 → 更新 `contracts/trace_schema.md`。
- 增删工具或预设 → 更新 REQUIREMENTS.md 能力清单 + eval cases。
- 文档超过行数预算 → 精炼或替换，禁止只增不减（本文件 ≤80 行）。

## 反模式禁令

- 不新建转发 shim 文件；一个概念只允许一个导入路径。
- 不跑与改动无关的全量评测；日常用 `--smoke`。
- 不新增 no-op 节点、硬编码示例数据或 demo 回复文案。
- 不做多租户隔离、家人代记拦截（见 REQUIREMENTS.md OUT OF SCOPE），除非需求基线变更。
  RAG 知识问答已纳入基线（D13-D17）：检索器必须是 `knowledge_retriever` 接缝实现，
  知识回答的数字必须出现在检索原文中（reply_guard 引用模式）。
