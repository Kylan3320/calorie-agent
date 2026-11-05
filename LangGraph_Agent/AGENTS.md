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

- 本机 API 部署凭据保存在仓库外 `%USERPROFILE%\.config\calorie-agent\tencentcloud-credentials.csv`，加载方式见 `README.md`「云函数部署」。部署进程按需读取，不回显密钥，不复制进仓库、报告或部署包。
- 按任务要证明的结果选择验证。文档核对准确性与引用，代码选相关用例。涉及迁移、来源规模或恢复时覆盖相关真实输入和旧状态。用 `harness/eval/runner.py --list` 查场景、`--case ID` 选用例。仅在任务明确要求全量，或能指出定向用例不足以覆盖的实际回归风险时，使用 `--all`。
- 复用仍能支持当前判定的证据。相关实现、配置、来源或状态有变动时，只复验受影响项。失败、缺证或有具体疑点时，只检查对应路径。证据满足当前交付判据后结束验证。
- 交付前保留必要证据及有效发布／回退包，按固定 ID／路径清理本轮创建的测试数据、临时文件和失效候选资源，并核对清理结果。直接阻塞本轮交付的已知残留按需处理，其余历史残留沿用原清单，单独清理。
- 整体开发任务完成并通过必要验证后，由主线程统一提交本任务改动，确认待推送提交范围后推送当前分支；独立审查子 Agent 默认只返回审查结果。改动归属不清或推送失败时说明。本地 `.gitignore` 不提交；默认不打 tag、不创建 Release、不部署，用户另有要求时按要求执行。
- 任务结果及必要验证满足后结束，交付实际改动、验证证据和未解决项，明确实现与部署状态。已约定下游线程完成后回报时，完成本轮可独立推进的工作，收到回报后再继续判定。删除失效指令，避免把一次性处理写成长期规则。
