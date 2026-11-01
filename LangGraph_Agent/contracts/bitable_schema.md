# Bitable 表、状态及幂等

业务持久存储仍为现有 Bitable。字段默认名可按已有 BITABLE_FIELD_* 配置覆盖；不清空或替换现有表。

## 食物与摄入

foods 保留 food_id/food_name/alias/carbs_per_100g/protein_per_100g/fat_per_100g/status/source/boohee_id，新增 kcal_per_100g（数字，可空）、units（JSON文本，含单位原文、weight/eat_weight）、detail_loaded（文本true/false）。薄荷详情按code请求with_units=true，营养value归一；缺units时查询单位接口。

intake_records 保留 date/user_id/message_id/raw_text/meal_type/food_id/food_name_snapshot/grams/carbs/protein/fat/kcal/status/created_at，新增 snapshot（JSON文本）：food每100g依据、数量原文/quantity/unit、换算basis、estimated、kcal_source及reuse_ratio（如有）。grams为最终可食重；来源热量优先，否则按未舍入宏量4/4/9后round_number。旧摄入保持原值。

date默认毫秒，可按BITABLE_FIELD_INTAKE_DATE_VALUE_TYPE=text存YYYY-MM-DD；meal_type未提供为unknown。有效status为空或valid/active/normal/启用/正常/有效/已记录；撤销只设deleted。

standards原有user_id/standard_type/date/kstandard/cstandard/pstandard/fstandard保留并逐字段读取；个人目标通过user_settings逐字段覆盖，daily_targets按日期隔离，长期变更不覆盖当天值。

## 同一套可恢复状态

pending_tasks保留pending_id/user_id/chat_id/source_message_id/intent/food_name/grams/meal_type/raw_text/status/created_at/expires_at，payload必须为JSON文本列。

payload的物理编码为JSON封套 `{encoding:"zlib-base64",data:"..."}`，data是完整业务JSON的UTF-8经zlib无损压缩再base64；读取仍接受已有普通JSON行。编码只在BitableStore读写边界执行，Store调用方及FakeStore始终使用同一业务dict，不丢弃标准消息、原文、调用结果或恢复信息，以避免多轮对话触及单元格文本上限。

- message_received：飞书原消息、received_at、intake_date，ACK前保存，发送成功才completed。
- message_execution：message/day、标准序列化messages、model_calls、model_requests、calls（原生call_id/tool/args/index/action_id、prepared及result）、session/settings、确认快照、最终result。首批调用先整体持久化，逐项结果随后保存；执行完成才completed。
- agent_session：按归一user_id＋入口session_id隔离；标准messages包含摘要及近期完整配对调用；unfinished独立保存原文、原餐日期/餐次、已知数量、候选快照；refs/last_meal保存可操作记录ID；proposal保存固定目标/参数，awaiting仅在实际回复请求确认时设true。
- user_settings：按归一user_id共享；targets/daily_targets、preferences、portions（food_id:unit→eat_weight）、favorites（命名营养依据快照）、reminders、feishu_chat_id（最近本人飞书会话）。提醒默认关闭。
- reminder_receipt：规则ID＋本地日键，保存固定chat_id/reply/uuid/sent/send_result。

上述内部状态expires_at=0；未完项不再走旧业务pending路由。旧已收件执行/待发送回复须先由旧入口收尾；尚待回答的missing_grams/missing_food仅迁移日期、已知数量、候选及原文事实后关闭旧任务；旧confirm_undo只保留目标引用，不执行旧计划。

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

每个原生调用按首次持久化返回顺序分配N，动作消息键为message_key:agent:N，不按重试模型重新排序。同一整餐批量写入用该动作键；写前按message_id查重，再用client_token。改量/软删除固定record_id设置绝对结果，重试不再缩放一次。

文档状态复用上表：message_key为`{kind}:{user_id}:{scope}`，intent=kind，food_name为空。提醒scope为`reminder:{rule_id}:{local_date}`，发送message_key同该scope。状态更新始终按固定record_id，不引入另一套幂等后端。

user_aliases与usage_events保留原有字段及业务含义；知识schema见knowledge_schema.md。列表翻页拉全；HTTP429/5xx一次退避重试，受消息处理期限限制。

发布时只通过agent/storage/migration.py添加缺列；保留既有行和未涉及配置。

执行日志只在messages保存一份完整消息；session及prepared.session只存业务状态，不复制messages。最终result只存回复/状态/守卫等结果元数据，重放从calls复原tool_results/native_calls，并关联model_requests；避免同一Bitable单元格重复嵌入历史和工具事实触发TooLargeCell。
