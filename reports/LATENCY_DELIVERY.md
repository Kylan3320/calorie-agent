# 飞书＋COS 生产交付记录

## 当前部署

最近核实的部署为 2026-11-05 03:55（北京时间）。三个正式函数切换至 R15 原包并恢复受理，开始个人试用。

| 项目 | 实际值 |
|---|---|
| 构建 | cos-production-303ddad6d78b8b0a（R15个人试用） |
| 原上传业务包 | `deploy/calorie-agent-r15-4f6b408.zip`，SHA256 `5db7c6f3ac525fe39a9f1a6e149f1fa826a8e2b1752681347945370cba29534b` |
| 函数 | 上海/default：受理 calorie-agent-api-v3-test（HTTP）、消息 calorie-agent-reminder（app.message_handler）、归档 calorie-agent-archive（app.maintenance_handler） |
| 运行配置 | Python3.10、512MB、120秒，各平台并发1；INGRESS_MAINTENANCE=0；每分钟提醒、每五分钟维护／归档 |
| Layer | 三函数统一 `calorie-rag:7`，索引版本 `sourcefix-20261102-69ca0bd3e30f6317`。本机 ZIP 位于 `reports/production-candidate/knowledge-source-fix/rag-layer-sourcefix-20261102-69ca0bd3e30f6317.zip`，SHA256 `3ac484a780b3adff15d2c2c625a67ac4fd05247994dc1ee6193071e9f7662936` |
| 数据域 | 私有桶 calorie-cos-probe-20261101-1440448581；正式前缀 calorie-agent/production/20261102/；保留原入口、角色、令牌和表 |

消息／归档实际下载ZIP与原包SHA相同；HTTP下载经平台重封装，SHA256为 `b091a6ee08af27f5587e2d27c1701800eefbe56ca8cc74f970730c1d6de116e8`，BUILD及全部文件内容仍与原包一致。三函数Active，原角色、前缀、环境、表、Layer、触发及并发保护核对通过，INGRESS_MAINTENANCE恢复0。线上health、飞书回调有效token challenge／无效token 403及应用认证通过。部署复用已接受的同包业务证据，正式没有新增测试业务消息或测试餐。实际核对及新回退目录见[R15正式发布](r15-personal-trial-production-20261105.json)，同包装配见[r15-cloud-package-20261104.json](r15-cloud-package-20261104.json)，原授权及全部体验差额见[正式个人试用记录](CONVERSATION_DELIVERY.md#r15-正式飞书个人试用2026-11-05)。部署步骤仍按[README](../README.md#云函数部署)。

## 验收结果与未解决项

- **R15 正式个人试用。** 用户接受该包三类暖账本的速度差额、知识速度差额和答复末尾的题外邀约。准确的差额、实际正文和授权范围见 [R15 记录](CONVERSATION_DELIVERY.md#r15-正式飞书个人试用2026-11-05)。
- **长期性能与保留。** 日志已支持暖视图增量追读。收件顺序列表、文档键覆盖检查、归档发现仍随历史增长。30 天终态回收、自动切代和维护后 15 分钟内提醒补查仍待实现。见 [COS 规格](../LATENCY_OPTIMIZATION_SPEC.md)。
- **旧正式回复质量。** 2026-11-02 的旧构建曾在今日 0 千卡的回复中附带未经本轮查询的昨日 525.6 千卡。修复已发布，同一场景暂无新版正式复验记录。

R7–R14 候选及 R15 补证前的判定见[交流体验验收记录](CONVERSATION_DELIVERY.md)。

## 历史结果

| 日期与范围 | 结果与证据 |
|---|---|
| 2026-11-03，上下文候选 a0c20e95eb79b5cc | 记餐／改量60976.144／26377.773ms，今日受DeepSeek503影响，速度未通过；真实摘要先失败20.162秒，定向复验2.546秒通过。见 [交流体验验收记录](CONVERSATION_DELIVERY.md#上下文治理候选2026-11-03)。 |
| 2026-11-02，COS迁移 | 正式基线含摄入0行、状态61条、食物125条、知识36条、别名1项、标准3条；来源SHA256 7b0de794e3671152bb8e8f4f487b25b4b53e93d48d984f259d8db4637d7858c7。独立Store核对及中断恢复通过。 |
| 2026-11-02，COS候选 | 第一候选记餐5529.877ms超标且缺暖证据；第二候选暖记餐／改量／今日为4535.374／4076.694／3677.453ms，五条ACK≤3秒，知识内容来源通过。完整最终材料见 [cloud-evidence-final](production-delivery/cloud-evidence-final/)。 |
| 2026-11-02，归档与防重 | 暂停归档期间业务正常完成，恢复至确认220285.108ms；独立读回两行唯一、最终187.4kcal，原消息重投未新增／回滚／重发。候选餐项按固定ID软删除，恢复链保留。 |
| 2026-11-02，旧正式回复 | 构建4c3fe0d5ea618cc5，消息formal-readonly-20261102-v1；ACK649.097ms、T_service4777.875ms，发送成功但附带旧525.6，质量失败。见 [final-status.json](production-delivery/final-status.json)。 |
| 2026-11-02，来源修复 | 米饭／主食两条答案与来源更新，其余34条保留，随Layer7发布。见 [修复记录](production-candidate/knowledge-source-fix/README.md)。 |
| 2026-11-01及接入前探针 | 个人功能、旧Bitable链路及COS隔离实验分别见 [个人功能记录](PERSONAL_DELIVERY.md)、[提速旧记录](latency-pre-cos-delivery-20261101.md)、[COS探针记录](COS_PROBE.md)。 |

2026-11-02本地维护完成旧会话事实隔离、显式评测选择、动作／待办恢复检查及冻结包检查，相关修复已包含于当前发布构建。19个定向用例、后续16个恢复相关用例及PS03/RL01/P04的各次原验证记录保留在原始报告归档；普通正文更新仍不改写派生索引，readonly重建零写入。

## 本地维护（2026-11-04）

当日调整评测选择、真实模型证据独立保存及旧状态恢复。RG01、U04 通过。

运行时外部调用接入绝对截止传输，完成判定复用工具注册表的只读属性。RR02、RP06–RP09、DB03、DB04 的当时定向结果保留在原始报告归档。接口见[工具契约](../contracts/tool_protocol.md)。

## 发布物与原始材料

当前业务包和冻结Layer保留在本机 deploy/ 与 reports/production-candidate/knowledge-source-fix/。最近一次完整材料归档位于 C:\Users\33203\AppData\Local\calorie-agent\archives\audit-20261104-97e186fa：original-reports/保留原始报告和证据，artifacts/保留业务包。新检出可从此归档恢复按上述SHA256冻结的发布物；原始轨迹及二进制不进Git。

2026-11-03部署前的原包和私有配置回退备份位于仓库外 C:\Users\33203\.config\calorie-agent\deploy-backups\facts-20261103-214110。首次迁移私有底账曾保存于CloudShell /home/cloudshell/data/calorie-release-20261102-r2/production-backup-private/。

### P0→P2任务接续与首次反馈（2026-11-04，未发布）

各候选的构建身份、场景差额和清理证据见[当时交付](CONVERSATION_DELIVERY.md#2026-11-04-p0p2实现完成候选部分通过正式未发布)。
