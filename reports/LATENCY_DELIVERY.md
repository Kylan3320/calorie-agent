# 飞书＋COS 生产交付记录

## 当前部署

2026-11-05 03:55（北京时间），三个正式函数切换至原 R15 包并恢复受理，开始个人试用。

| 项目 | 实际值 |
|---|---|
| 构建 | `cos-production-303ddad6d78b8b0a` |
| 原上传业务包 | `deploy/calorie-agent-r15-4f6b408.zip`，SHA256 `5db7c6f3ac525fe39a9f1a6e149f1fa826a8e2b1752681347945370cba29534b` |
| 函数 | 上海／default，受理 `calorie-agent-api-v3-test`（HTTP）、消息 `calorie-agent-reminder`（`app.message_handler`）、归档 `calorie-agent-archive`（`app.maintenance_handler`） |
| 运行配置 | Python 3.10、512 MB、120 秒、各平台并发 1、`INGRESS_MAINTENANCE=0`。每分钟提醒，每五分钟维护与归档。 |
| Layer | 三函数为 `calorie-rag:7`，索引 `sourcefix-20261102-69ca0bd3e30f6317`。本机 ZIP 为 `reports/production-candidate/knowledge-source-fix/rag-layer-sourcefix-20261102-69ca0bd3e30f6317.zip`，SHA256 `3ac484a780b3adff15d2c2c625a67ac4fd05247994dc1ee6193071e9f7662936` |
| 数据域 | 私有桶 `calorie-cos-probe-20261101-1440448581`，正式前缀 `calorie-agent/production/20261102/` |
| 回退 | 上一正式构建 `cos-production-8794be0e62e0015b` 的逐函数原包及私有配置位于 `%USERPROFILE%\.config\calorie-agent\deploy-backups\r15-20261105-034922` |

实际下载的两个 Event ZIP 与原包同 SHA256。HTTP ZIP 经平台重封装，BUILD_ID 与所有成员字节一致。原配置、健康检查和回调鉴权核对通过，正式切换复用同包隔离业务证据。 [发布读回及回退清单](r15-personal-trial-production-20261105.json) · [同包装配](r15-cloud-package-20261104.json) · [运维手册](../docs/operations.md#云函数部署)

## 验收结果与未解决项

- **R15 正式个人试用。** 三类暖账本及知识答复有速度差额，知识末尾有题外邀约。用户接受该包例外。 [实测与授权](CONVERSATION_DELIVERY.md#r15-正式飞书个人试用2026-11-05)
- **长期性能与保留。** 暖视图已增量追读日志。收件顺序列表、文档键覆盖检查和归档发现仍随历史增长。阶段 C 分支已实现有界索引、30 天终态瘦身、自动维护候选和维护后 15 分钟提醒补查；正式部署仍保留原行为，真实隔离切代／回收未验。 [COS 规格](../LATENCY_OPTIMIZATION_SPEC.md)
- **旧正式回复。** 构建 `4c3fe0d5ea618cc5` 的今日查询带出未经本轮查询的昨日 525.6 千卡。修复已发布，同一场景暂无新版正式复验记录。原证据为本机归档 `original-reports/production-delivery/final-status.json`。

## 历史发布与证据

- **2026-11-03 `cos-production-8794be0e62e0015b`。** 事实回执与固定等待预算上线。部署前回退资料位于 `%USERPROFILE%\.config\calorie-agent\deploy-backups\facts-20261103-214110`。 [候选结论](CONVERSATION_DELIVERY.md#事实回执与固定等待预算候选) · [正式部署](facts-production-deploy.json)
- **2026-11-02 COS 切换。** 迁移基线含摄入 0 行、状态 61 条、食物 125 条、知识 36 条、别名 1 项、标准 3 条。独立读回、中断恢复及重投防重通过。首次私有底账位于 CloudShell `/home/cloudshell/data/calorie-release-20261102-r2/production-backup-private/`。候选、归档水位和切换原证据见本机归档 `original-reports/production-delivery/cloud-evidence-final/`。
- **2026-11-02 Layer 7。** 两条主食知识及来源更新，余下 34 条保留。 [来源修复](production-candidate/knowledge-source-fix/README.md)
- **此前验收。** [交流候选](CONVERSATION_DELIVERY.md) · [个人功能](PERSONAL_DELIVERY.md) · [Bitable 链路](latency-pre-cos-delivery-20261101.md) · [COS 探针](COS_PROBE.md)

本机归档根为 `%LOCALAPPDATA%\Packages\OpenAI.Codex_2p2nqsd0c76g0\LocalCache\Local\calorie-agent\archives`。上述 `original-reports/` 和当时留存的业务包 `artifacts/` 位于其子目录 `audit-20261104-97e186fa`。候选证据的旧 `raw_evidence.path` 可按 `archives/` 后的相对路径定位，再核对原 SHA256。

## 阶段 C 未部署候选

基于 `5ee5fab7f55a96c3b6a792947b3aeb68bee14110` 的 C0／C1／S1～S3 实现、受控验收、构建与外部缺口统一见 [阶段 C 交付](STAGE_C_DELIVERY.md)。本任务不变更上述当前部署、生产角色或数据代；PR 保持草稿，等待真实模型／通用来源及已授权隔离 COS 证据。
