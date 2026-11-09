# 个人功能历史验收记录（2026-11-01）

本记录保存当时Bitable链路的实现及真实飞书验收；当前部署与未解决项见 [生产交付记录](LATENCY_DELIVERY.md)。

## 实际实现

统一以LangChain v1 create_agent和原生工具循环承接飞书、CLI及DSH /calorie；保留/dsh引擎及账号归一。注册表派生八个工具的schema、说明及风险，去除固定AgentPlan链路。Bitable/FakeStore同步整餐、部分补全、来源可食单位、快照复用、指定项改量/软删除、目标/偏好/计划、提醒及会话恢复语义。内置摘要和调用限制；三预设控制真实读写/发送；事实守卫绑定工具字段及检索原文。

D阶段实际发现并修复：单位规格重复进食物名导致额外候选追问；仅当基础名称唯一、括号规格与所选单位一致且来源存在唯一该单位时去掉重复规格，不忽略品牌或生熟差异。多轮执行状态曾触发Bitable TooLargeCell；先消除重复嵌套，再在BitableStore边界用zlib/base64无损编码完整状态，读回已有普通JSON，不裁掉原生消息或恢复信息。同步contracts。README/CLI说明已去除旧的无网络/确定性规划描述。

## 必要离线验收与独立审查

U01–U07均经harness/eval/runner.py通过。独立FakeStore、假时钟、NativeFake原生工具模型、假薄荷；预期来自固定夹具，不从待验工具反算。U01整餐270kcal且B可食100g；U02跨日补全和语义歧义；U03复用/修改/撤销后300kcal及3项；U04压缩、重建、schema/结果回传、会话隔离及只读零写入；U05目标逐字段与邻接确认、计划/食用分离；U06关闭/到期/重复/已记餐及无模型提醒；U07写成功未存结果时中断、恢复两动作各一次及原回复uuid。

U05/U06沿用前序首次通过结果，无无关重复执行。后续证据：offline-check.log（U01/U02/U03/U04/U07）；review-recheck.log（U02/U07）；live-fix-recheck.log（U04/U07）；unit-fix-recheck.log（U01/U02）；state-codec-recheck.log（U04/U07，含超过100KB的完整状态真实Store编码往返及旧JSON读取）。


## 最终构建与云端

- 构建：personal-ce16308e8c26bf71，13,902,378 bytes。
- ZIP：deploy/calorie-agent-v4.zip，SHA256 b15ef1176782d1ffae1028609328f7fe8ed838741f3bffe1693548c9c4ab112b。
- Linux Python3.10.22实际包启动、create_agent、原生产calorie-rag:6/索引及app.reminder_handler加载通过；package-check.json。索引175实体、241关系、36片段，未重建或用FAQ替代。
- 主函数：ap-shanghai/default/calorie-agent-api-v3-test，Python3.10、端口9000、512MB、120秒、最大并发1；原VPC、环境配置、Layer6和飞书webhook保留。线上health标识与最终包一致，模型deepseek-v4-flash、检索器lightrag；live-health.json。供应商返回model_name为deepseek-flash，未迁移模型配置。
- 提醒：同地域/default/calorie-agent-reminder，同ZIP、app.reminder_handler、Python3.10、512MB、120秒；calorie-reminder-minute触发器启用、默认流量/$LATEST、cron `0 */1 * * * * *`。从控制台下载实际已部署代码包，逐字节与本地最终包一致；reminder-deployed-package.json。最新17:54+08定时日志成功、sent=0；reminder-live-log-final.txt及reminder-log-final.jpg。用户规则默认关闭；发送分支沿用U06，不额外等线上发送。

## Bitable更新与真实飞书

仅增加foods的kcal_per_100g/units/detail_loaded、intake.snapshot和pending.payload五列；收尾7条已过期旧追问，切换前无待恢复已收件执行。bitable-migration.json。未清空表或删除既有业务行。

实际应用：热量agent-v3（cli_aabc166fccb8dcef），本人Windows飞书应用。来源夹具在写入前独立获取：米饭116kcal/100g；鸡蛋139kcal/100g，薄荷只(中)总重60g、可食52.8g。实际两只按105.6g计算并标估算。见boohee-release-check.json、live-source-fixtures.json、live-expectations.json。

| 真实场景 | 实际落表与回复核对 |
|---|---|
| 初次整餐与补全 | 米饭200g=232kcal；鸡蛋105.6g=146.8kcal；2行合计378.8kcal。原版本出现额外候选，补全无重复米饭。 |
| 改量并存常用餐 | 只把米饭改100g=116kcal，鸡蛋不变；常用餐升级验收餐20261101保存两项快照，2行合计262.8kcal。 |
| 半份复用并查询 | 新增米饭50g=58kcal、鸡蛋52.8g=73.4kcal；4行合计碳水42.7g/蛋白24.6g/脂肪14.1g/394.2kcal。快照复用未重查供应商。 |
| 知识问答 | DeepSeek原生query_knowledge，LightRAG实际aquery_data/mix检索到两个生产索引chunk，原文来源中国居民膳食指南2022，飞书包含原文证据及来源。 |
| 单位问题定向复验 | 同一真实会话整餐一次写两项，无unfinished，无候选追问；两项及378.8kcal与独立夹具一致。 |
| 最终包持久化复验 | 最终ce16308e包在SCF自动完成query_today与后续决策，读取6行合计碳水97g/蛋白43.6g/脂肪23.8g/773kcal，reply_sent=true、status=ok。 |

真实journal见live-execution-*.json；原生选择、provider元数据、工具结果进入后续请求及LightRAG chunk证据汇总live-native-evidence.json。真实行见live-intake-before-cleanup.json、live-final-meal-rows.json；飞书截图feishu-reuse.png、feishu-knowledge.png、feishu-final-meal.png、feishu-final-scf.png。

如实记录恢复经过：改量/存常用餐在SCF已成功写入后，因原单元格限制，通过本地显式恢复入口重放已存结果并送达原回复；未重写业务。最终整餐两项也是SCF一次写成，后续查询保存触发同类限制，改无损编码后由本地原收件继续第三个查询及最后决策并发送，未重写摄入。随后最终包上的SCF独立查询自动全链路成功，未依赖本地恢复。

## 清理与剩余事项

仅按本次消息ID、本人user_id及固定record_id清理：共7条本次摄入全部软删除（失败复验1条提前清理、最后6条清理），移除测试常用餐；测试后有效摄入0、启用提醒0、pending收件0。目标/偏好/常用份量未被修改；保留最近本人飞书会话作为投递路由。删除状态/前后行及设置见live-cleanup.json；保留食物来源缓存、原生审计、会话历史和usage，不触碰非本次配置。

当时display引用失败由事实守卫回执，存在正文较长和空方括号问题，后续由自然回复及普通账本事实回执路径替代。两次已恢复执行的旧usage失败按原键保留。
