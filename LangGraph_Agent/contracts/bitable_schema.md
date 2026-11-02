# Bitable 表、状态及幂等

当前事实及运行状态保存在 COS，Bitable 是后台单向归档及来源资料维护入口。字段默认名可按已有 BITABLE_FIELD_* 配置覆盖；不清空或替换现有表。生产消息链路不访问 Bitable。

## 食物与摄入

foods 保留 food_id/food_name/alias/carbs_per_100g/protein_per_100g/fat_per_100g/status/source/boohee_id，新增 kcal_per_100g（数字，可空）、units（JSON文本，含单位原文、weight/eat_weight）、detail_loaded（文本true/false）。薄荷详情按code请求with_units=true，营养value归一；缺units时查询单位接口。

intake_records 保留 date/user_id/message_id/raw_text/meal_type/food_id/food_name_snapshot/grams/carbs/protein/fat/kcal/status/created_at，snapshot（JSON文本）保存 food每100g依据、数量原文/quantity/unit、换算basis、estimated、kcal_source及reuse_ratio（如有）。新增 agent_item_id（文本，COS稳定业务ID）及 agent_revision（数字，对应已提交餐项版本）。grams为最终可食重；来源热量优先，否则按未舍入宏量4/4/9后round_number。旧摄入保持原值及原飞书行ID。

date默认毫秒，可按BITABLE_FIELD_INTAKE_DATE_VALUE_TYPE=text存YYYY-MM-DD；meal_type未提供为unknown。有效status为空或valid/active/normal/启用/正常/有效/已记录；撤销只设deleted。

standards原有user_id/standard_type/date/kstandard/cstandard/pstandard/fstandard保留并逐字段读取；个人目标通过user_settings逐字段覆盖，daily_targets按日期隔离，长期变更不覆盖当天值。

## COS 当前事实与后台归档

按消息写入者的已发布业务提交生成 `archive:{kind}:{id}`（kind 为 intake/food/settings），内容包含稳定 id、revision、value、user_id；仅这些业务内容变化才推进对应版本。收件、模型预算、会话、投递状态不推进归档版本。归档成功不删除 COS 当前餐项。

每项后台同步进度独立保存在 COS 的 sync 范围，包含 ack_revision、inflight（发送前保存的原请求、原版本和固定client_token）、record_id、status、last_error、confirmed_at。每5分钟选择各项尚未发送的最新版本；若已有不确定请求，先按该请求的稳定键查询并解决，再推进新版本。仅独立读回全部目标字段与版本后推进水位；已确认版本跳过；暂时失败保留原请求，权限、配置或身份重复错误标记 blocked 待处理。

导入摄入以原飞书 record_id 为业务 ID，另保存 feishu_record_id 映射；按已知飞书行 ID 更新。新摄入 ID 按固定 action_id 和首次项序号派生，归档按 agent_item_id 查找并保存实际飞书行 ID，不能按批量响应顺序绑定。创建token为 `stable_uuid("calorie-agent:archive:{kind}:{id}")`，恢复不生成新token。改量及软删除发送绝对值；读到更高 agent_revision 不覆盖为旧版本。

食物按 food_id 稳定键归档，个人设置按既有 user_settings 文档 pending_id 稳定键归档，保留来源表与字段。后台归档只保存用户设置，不把收件、执行日志、会话、usage、投递进度重新写回 pending_tasks。

受理、提交及恢复规则见 [Store 与工具契约](tool_protocol.md#入口及预设)。

## 旧 Bitable 状态与迁移

pending_tasks保留pending_id/user_id/chat_id/source_message_id/intent/food_name/grams/meal_type/raw_text/status/created_at/expires_at，payload必须为JSON文本列。

payload的物理编码为JSON封套 `{encoding:"zlib-base64",data:"..."}`，data是完整业务JSON的UTF-8经zlib无损压缩再base64；读取仍接受已有普通JSON行。编码只在 BitableStore 边界执行，Store 调用方及 FakeStore 使用业务 dict。

- message_received：飞书原消息、received_at、intake_date，ACK前保存，发送成功才completed。
- message_execution：message/day、标准序列化messages、model_calls、model_requests、calls（原生call_id/tool/args/index/action_id、prepared及result）、session/settings、确认快照、最终result。首批调用先整体持久化，逐项结果随后保存；执行完成才completed。
- agent_session：按归一user_id＋入口session_id隔离；标准messages包含摘要及近期完整配对调用；unfinished独立保存原文、原餐日期/餐次、已知数量、候选快照；refs/last_meal保存可操作记录ID；proposal 保存固定目标/参数、response_id 和 presented；offered_message_id 标记未完项候选首次出现或变化的消息。
- user_settings：按归一user_id共享；targets/daily_targets、preferences、portions（food_id:unit→eat_weight）、favorites（命名营养依据快照）、reminders、feishu_chat_id（最近本人飞书会话）。提醒默认关闭。
- reminder_receipt：规则ID＋本地日键，保存固定chat_id/reply/uuid/sent/send_result。

上述内部状态 expires_at=0。导出保留摄入含软删除、营养快照、原 ID/身份/日期/数值、来源配置、设置、会话、未完事项及幂等和投递状态。尚待回答的missing_grams/missing_food在目标COS基线中迁移日期、已知数量、候选及原文事实并保留旧任务的取消状态；旧confirm_undo只保留目标引用，不执行旧计划。不修改或删除源行。

`deploy/migrate_cos.py export/import` 保存原始快照及独立审计；首次导入要求空前缀，同一基线可幂等重试；通过生产 Store API 回读核对所有 ID、有效行数、按日热量、完整营养快照和幂等任务摘要。测试前缀不导入。迁移与回退要求见 [COS 规格](../LATENCY_OPTIMIZATION_SPEC.md#5-迁移与回退)。

## 幂等公式

message_key=message_id，空则chat_id:text。stable_uuid为sha256前16bytes置UUIDv4位。

| 用途 | 公式 |
|---|---|
| 摄入client_token | stable_uuid("calorie-agent:{message_key}:intake") |
| 回复uuid | stable_uuid("calorie-agent:{message_key}:reply") |
| pending_id | stable_uuid("calorie-agent:pending:{message_key}:{intent}:{food_name}") |
| pending client_token | stable_uuid("calorie-agent:pending-create:{message_key}:{intent}:{food_name}") |
| food_id | stable_uuid("calorie-agent:food:{normalized_name}") |
| 食物client_token | stable_uuid("calorie-agent:food-create:{normalized_name}") |
| usage event_id | stable_uuid("calorie-agent:usage:{message_key}") |
| 知识client_token | stable_uuid("calorie-agent:knowledge-create:{sorted_entry_ids_joined_by_colon}") |

每个原生调用按首次持久化返回顺序分配N，动作消息键为message_key:agent:N，不按重试模型重新排序。同一整餐批量写入用该动作键；COS原子提交绑定动作键、业务变更及完整工具结果。改量/软删除固定record_id及准备时版本、日期、营养依据和绝对结果，重试不再缩放一次；旧失败任务不能覆盖后续已修改的版本。

动作防重与待办生命周期分别核对：prepared.pending_before 绑定补全／取消的原待办，提交前检查其当前状态，业务提交同时保存待办进度，防止不同消息重复完成同一 pending_id。提案 target_version 绑定展示时的修改目标；相对改量冻结为绝对结果。旧未提交补全准备或修改提案缺少这些依据时，保留已提交事实并要求重新发起相关操作；不修改幂等键公式或新增Bitable字段。

文档状态保留上表稳定身份：message_key为`{kind}:{user_id}:{scope}`，intent=kind，food_name为空。提醒scope为`reminder:{rule_id}:{local_date}`，发送message_key同该scope。COS导入保留原pending_id及已有完成状态。

user_aliases与usage_events保留原有字段及业务含义；知识schema见knowledge_schema.md。列表翻页拉全；HTTP429/5xx一次退避重试，受消息处理期限限制。

发布时只通过agent/storage/migration.py添加缺列；保留既有行和未涉及配置。
