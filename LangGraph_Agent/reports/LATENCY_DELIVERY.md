# 飞书＋COS 生产交付记录

## 当前部署

最近部署核对：2026-11-03 21:57（北京时间）。三个正式函数已发布并恢复受理；用户在获知候选性能未通过后明确要求此次上线。

| 项目 | 实际值 |
|---|---|
| 构建 | cos-production-8794be0e62e0015b |
| 业务包 | deploy/calorie-agent-v4.zip，22,257,853字节；SHA256 3eeac2319b2d3e7df6d5e564061d89862fec637379959aa3bb4e26d960bca30c |
| 函数 | 上海/default：受理 calorie-agent-api-v3-test（HTTP）、消息 calorie-agent-reminder（app.message_handler）、归档 calorie-agent-archive（app.maintenance_handler） |
| 运行配置 | Python3.10、512MB、120秒，各平台并发1；INGRESS_MAINTENANCE=0；每分钟提醒、每五分钟维护／归档 |
| Layer | 三函数统一 calorie-rag:7；INDEX_VERSION=sourcefix-20261102-69ca0bd3e30f6317；ZIP SHA256 3ac484a780b3adff15d2c2c625a67ac4fd05247994dc1ee6193071e9f7662936 |
| 数据域 | 私有桶 calorie-cos-probe-20261101-1440448581；正式前缀 calorie-agent/production/20261102/；保留原入口、角色、令牌和表 |

消息／归档实际下载 ZIP 与业务包哈希相同；HTTP 下载容器经平台重封装，SHA256 为 f232614eeda7a0cab9bc8aa98abfff10f03f1fa4216b0c99fcc42a3e0054c12f，BUILD_ID 及全部成员内容一致。线上 health、回调鉴权和飞书应用认证通过。此次部署复用同构建隔离业务证据，正式环境没有新增测试业务消息。核对及回退记录见 [facts-production-deploy.json](facts-production-deploy.json)，装配见 [facts-package-check.json](facts-package-check.json)。部署步骤维护于 [README](../README.md#云函数部署)。

## 验收结果与未解决项

- 事实回执与固定等待预算已随上述构建发布；隔离三条业务事实正确、均一次主决策。改量暖样本5107.432ms超过5秒，记餐缺暖样本，今日暖样本4940.651ms通过，候选整体未通过。详细结果见 [交流体验验收记录](CONVERSATION_DELIVERY.md)。
- 2026-11-02旧构建曾在今日0千卡的回复中附带未经本轮查询的昨日525.6千卡，正式回复质量失败。修复已随新构建发布，新版正式环境的同场景尚无复验结果。
- 知识内容及来源在2026-11-02候选通过；T_service=11772.607ms，比10秒目标多1772.607ms，按需求单列。
- [长期性能与数据保留目标](../LATENCY_OPTIMIZATION_SPEC.md)尚未实现：收件顺序列表、文档键覆盖检查、提交发现和归档发现仍随历史增长；未实现30天终态回收和代切换维护。
- 2026-11-03候选云端资源与数据清理完成。本机临时目录 C:\Users\33203\AppData\Local\Temp\calorie-facts-candidate-20261103 的12文件（802180字节）删除曾被自动审批拒绝，仍列为待清理；见 [清理记录](facts-local-cleanup.json)。

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

统一当前状态归属，移除历史执行指令，按改动范围限定验收要求；修正U04／E06覆盖说明及实际HTTP输入的审计位置。真实模型评测按运行独立保存轨迹，已有输出在模型调用前拒绝覆盖。相关摘要和脱敏关键证据已从忽略范围中排除，等待随本次变更提交。

定向RG01／U04共2项通过：覆盖默认与显式输出、已有文件拒绝、失败轨迹保存、选择范围以及跨轮状态恢复／只读。修改的4份Python文件通过3.10语法检查，文档引用核对通过。本次仅文档和本地评测变更，生产运行时代码及发布包未改，无需部署。

缓存、退役正式只读脚本、调试材料和旧重复证据包共67个文件、3,109,331字节移至下列归档的removed/，现用完整证据及两份发布物保留。

### 运行时截止与完成判定（2026-11-04，本地未部署）

薄荷、SCF派发、LightRAG关键词模型／embedding、LiveClient接入现有绝对截止传输；薄荷两尝试与退避共享10秒，SCF单次2秒，知识检索整体85秒或当前剩余业务时间，包含构建／初始化及线程HTTP。LiveClient单请求最多10秒，总等待从受理前开始，超时区分受理未知／已受理并保留原消息键，不自动重发；默认连接池按模块复用，避免暖进程每次创建并保留新资源。未修改共享传输或重建截止框架。

完成判定按工具注册表的只读属性处理成功的前置读取，不再为营养查询／方案计算追加工具名称排除分支。最终批次、此前写入及非成功结果仍参与回执判定；无法完整表达则继续原生循环。数字及知识保持现有自然回复契约，未新增数字守卫、语义审核器或生成协议。

定向 RR02、RP06／RP07／RP08／RP09、DB03／DB04 共7个独立用例通过，覆盖前置读取后直接完成、此前设置／失败回执保留、各新增外部边界持续保活、薄荷退避不续期、知识回调取消后的物理退出、正常响应／认证及原键恢复。复用既有主模型、Feishu和COS传输证据，未重跑其完整矩阵。补修Python3.10异步超时异常区别及默认连接池生命周期后，仅复验受影响单例；Python3.10语法检查通过。本机为Windows／Python3.13，LightRAG验收使用轻量依赖替身调用真实HTTP回调，未运行新一轮Linux装配、真实供应商、全量、冒烟或线上验收。

独立只读审查确认连接池生命周期问题已修复，最终未发现剩余实质问题。当前仅源码、相关用例及契约更新，发布包和正式函数未更新。自然语言的完整意图判断及知识数字依据仍由现有模型契约与语义验收保障，不能据此宣称自由正文已被确定性校验。部署控制脚本的独立HTTP超时未纳入本次运行时修复；既有性能未通过项与长期保留目标不因本次离线结果改判。

## 发布物与原始材料

当前业务包和冻结Layer保留在本机 deploy/ 与 reports/production-candidate/knowledge-source-fix/。最近一次完整材料归档位于 C:\Users\33203\AppData\Local\calorie-agent\archives\audit-20261104-97e186fa：original-reports/保留原始报告和证据，artifacts/保留业务包。新检出可从此归档恢复按上述SHA256冻结的发布物；原始轨迹及二进制不进Git。

2026-11-03部署前的原包和私有配置回退备份位于仓库外 C:\Users\33203\.config\calorie-agent\deploy-backups\facts-20261103-214110。首次迁移私有底账曾保存于CloudShell /home/cloudshell/data/calorie-release-20261102-r2/production-backup-private/。
