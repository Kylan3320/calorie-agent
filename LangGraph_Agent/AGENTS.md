# Calorie Agent 协作约定

需求与产品语义见 `REQUIREMENTS.md`，跨模块接口见相关 `contracts/`。
按当前任务读取相关内容，已确认的新决定同步到原文档。当前部署、验收结果和未解决项见 `reports/LATENCY_DELIVERY.md`。

## 实现边界

- 飞书＋COS 存储与执行约定见 `LATENCY_OPTIMIZATION_SPEC.md`：COS 保存运行状态及已提交业务事实，Bitable 后台归档；LLM 负责理解和规划，营养计算与读写由工具执行。
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
- 按当前任务和实际影响选择最小充分验证：文档核对准确性与引用，代码选相关用例；涉及迁移、来源规模或恢复时覆盖相关真实输入和旧状态。评测用 `harness/eval/runner.py --list` 查场景、`--case ID` 选择；全量须显式 `--all`，仅在影响范围确实需要时使用。
- 复用对应实现、配置、来源和会话状态仍有效的证据；仅在相关变更、前次失败、证据缺失或具体疑点时复验。历史规格中的执行批次不自动触发重跑。
- 任务结果及必要验证满足后结束，交付实际改动、验证证据和未解决项，明确实现与部署状态。删除失效指令，避免把一次性处理写成长期规则。
