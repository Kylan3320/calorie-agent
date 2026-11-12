# Calorie Agent 协作约定

需求与产品语义见 `REQUIREMENTS.md`，跨模块接口见相关 `contracts/`。
按当前任务读取相关内容，已确认的新决定同步到原文档。当前部署、验收结果和未解决项见 `reports/LATENCY_DELIVERY.md`。

## 实现边界

- 当前存储与执行接口见 `contracts/tool_protocol.md`，长期增量处理与保留目标见 `LATENCY_OPTIMIZATION_SPEC.md`。COS 保存运行状态及已提交业务事实，Bitable 后台归档；LLM 负责理解和规划，营养计算与读写由工具执行。
- 预设行为集中在 `agent/policy/presets.py`。
- COS 与 Feishu/Bitable HTTP 仅在 `agent/storage/`；工具实现和注册在 `agent/tools/`。
- 业务数字须有工具事实依据；知识回答数字须出现在检索原文中。
- 成功回复依据对应已提交业务结果，飞书归档进度单独确认。
- 创建防重及状态更新遵循 `contracts/bitable_schema.md`。

## 同步维护

- 改表字段、幂等键：更新 `contracts/bitable_schema.md`。
- 改 Store 接口：更新 `contracts/tool_protocol.md`，同步生产 Store 与 FakeStore 的业务语义。
- 改 trace 形状：更新 `contracts/trace_schema.md` 及实际消费者。
- 增删工具：同步注册表、需求能力清单及相关 eval 用例。
- 控制台仅监听 127.0.0.1；其依赖、DSH 数据和构建产物不进云函数包。

## 验证与交付

- 凭据按 [运维手册](docs/operations.md#云函数部署)从仓库外加载。密钥仅供部署进程读取。
- 按交付判据验证受影响路径。文档核对事实和引用，代码用 `harness/eval/runner.py --list` 查场景、`--case ID` 选用例。迁移、来源规模和恢复变更覆盖相应真实输入及旧状态。复用有效证据，只补失效项和缺项，满足判据即结束。`--all` 仅用于明确要求全量或定向用例无法覆盖具体回归风险的任务。
- 交付前保留必要证据及有效发布／回退包，按固定 ID／路径清理本轮创建的测试数据、临时文件和失效候选资源，并核对清理结果。直接阻塞本轮交付的已知残留按需处理，其余历史残留沿用原清单，单独清理。
- 整体开发任务完成并通过必要验证后，由主线程统一提交本任务改动，确认待推送提交范围后推送当前分支；独立审查子 Agent 默认只返回审查结果。改动归属不清或推送失败时说明。本地 `.gitignore` 不提交；默认不打 tag、不创建 Release、不部署，用户另有要求时按要求执行。
- 任务结果及必要验证满足后结束，交付实际改动、验证证据和未解决项，明确实现与部署状态。已约定下游线程完成后回报时，完成本轮可独立推进的工作，收到回报后再继续判定。删除失效指令，避免把一次性处理写成长期规则。
