# Calorie Agent 协作约定

需求与产品语义见 `REQUIREMENTS.md`，跨模块接口见相关 `contracts/`。
按当前任务读取相关内容；已确认的新决定同步到原文档，历史计划不自动成为新任务。

## 实现边界

- Bitable 是业务持久存储；LLM 负责理解和规划，营养计算与读写由工具执行。
- 预设行为集中在 `agent/policy/presets.py`；节点消费策略结果，不增加旧候选版本分支。
- Feishu/Bitable HTTP 仅在 `agent/storage/`；工具实现和注册在 `agent/tools/`。
- 业务数字须有工具事实依据；知识回答数字须出现在检索原文中。
- 成功记录、撤销和建库的回复分别依据对应写入结果；pending 不代表已写入。
- 创建防重及状态更新遵循 `contracts/bitable_schema.md`。
- 复用现有实现，不新增无用转发文件、演示回复或与任务无关的抽象。

## 同步维护

- 改表字段、幂等键：更新 `contracts/bitable_schema.md`。
- 改 Store 接口：更新 `contracts/tool_protocol.md`，同步 BitableStore 与 FakeStore 的业务语义。
- 改 trace 形状：更新 `contracts/trace_schema.md` 及实际消费者。
- 增删工具：同步 schemas、registry、需求能力清单，并补相关 eval 用例。
- 控制台仅监听 127.0.0.1；其依赖、DSH 数据和构建产物不进云函数包。

## 验证

先按需求或当前功能规格约定的验收范围执行；未约定时按影响选择：纯文档检查准确性与引用，局部行为运行相关用例，共享核心或跨链路改动运行全量。
基线仅在需要区分既有故障时运行；没有新增改动或疑点，不重复验证。
评测统一从 `harness/eval/runner.py` 运行，支持 `--smoke`、`--case ID` 和全量。
交付说明实际改动、验证结果及未解决事项。
