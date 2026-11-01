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

COS 收件、消息、同步写入者使用独立范围、连续编号和前驱引用，禁止覆盖创建；桶不启用版本控制。LIST只用于定位候选；消息写入者每次接单用下一编号禁止覆盖创建确认最新状态，冲突后按确切提交追读。已知对象暂不可见时进入恢复，不能重置为空状态。shadow_readonly 只读取可见已发布提交及版本，不创建确认点。

## 既有状态与迁移

pending_tasks保留pending_id/user_id/chat_id/source_message_id/intent/food_name/grams/meal_type/raw_text/status/created_at/expires_at，payload必须为JSON文本列。

payload的物理编码为JSON封套 `{encoding:"zlib-base64",data:"..."}`，data是完整业务JSON的UTF-8经zlib无损压缩再base64；读取仍接受已有普通JSON行。编码只在BitableStore读写边界执行，Store调用方及FakeStore始终使用同一业务dict，不丢弃标准消息、原文、调用结果或恢复信息，以避免多轮对话触及单元格文本上限。

- message_received：飞书原消息、received_at、intake_date，ACK前保存，发送成功才completed。
- message_execution：message/day、标准序列化messages、model_calls、model_requests、calls（原生call_id/tool/args/index/action_id、prepared及result）、session/settings、确认快照、最终result。首批调用先整体持久化，逐项结果随后保存；执行完成才completed。
- agent_session：按归一user_id＋入口session_id隔离；标准messages包含摘要及近期完整配对调用；unfinished独立保存原文、原餐日期/餐次、已知数量、候选快照；refs/last_meal保存可操作记录ID；proposal保存固定目标/参数，awaiting仅在实际回复请求确认时设true。
- user_settings：按归一user_id共享；targets/daily_targets、preferences、portions（food_id:unit→eat_weight）、favorites（命名营养依据快照）、reminders、feishu_chat_id（最近本人飞书会话）。提醒默认关闭。
- reminder_receipt：规则ID＋本地日键，保存固定chat_id/reply/uuid/sent/send_result。

上述内部状态expires_at=0；未完项不再走旧业务pending路由。维护窗口先拒绝新受理并排空旧执行及待发回复；导出保留摄入含软删除、营养快照、原ID/身份/日期/数值、来源配置、设置、会话、未完事项及已有幂等和投递状态。尚待回答的missing_grams/missing_food在目标COS基线中迁移日期、已知数量、候选及原文事实并保留旧任务的取消状态；旧confirm_undo只保留目标引用，不执行旧计划。不修改或删除源行。

`deploy/migrate_cos.py export/import` 保存原始快照及独立审计；导入仅允许空前缀，通过生产Store API回读核对所有ID、有效行数、按日热量、完整营养快照和幂等任务摘要。测试前缀不导入。切换前可恢复旧入口；产生新COS业务提交后只能回退兼容当前提交格式的构建，不自动反向迁移。

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

文档状态保留上表稳定身份：message_key为`{kind}:{user_id}:{scope}`，intent=kind，food_name为空。提醒scope为`reminder:{rule_id}:{local_date}`，发送message_key同该scope。COS导入保留原pending_id及已有完成状态。

user_aliases与usage_events保留原有字段及业务含义；知识schema见knowledge_schema.md。列表翻页拉全；HTTP429/5xx一次退避重试，受消息处理期限限制。

发布时只通过agent/storage/migration.py添加缺列；保留既有行和未涉及配置。

执行日志只在messages保存一份完整消息；session及prepared.session只存业务状态，不复制messages。最终result只存回复/状态/守卫等结果元数据，重放从calls复原tool_results/native_calls，并关联model_requests；避免同一Bitable单元格重复嵌入历史和工具事实触发TooLargeCell。

请求作用域内合并读取并复用文档行ID。首次创建可直接采用成功响应的record_id，响应丢失按固定pending_id读回。初始化文档创建已含session/settings时省去紧接的相同更新；无变化快照不重复写；最终模型无工具调用时由finish先保存最终执行结果／交付版消息，再保存会话，预算预存、工具批次／prepared／结果保存屏障保留。

新执行保存previous_session_message_id，会话保存last_message_id及last_execution_complete。完成执行重放时只在会话仍为前序状态或本条未完成状态时补交已存交付版会话；较新会话不被旧消息覆盖。旧执行缺该标记时保持既有重放行为。

unfinished的offered_message_id标记当前有序候选首次出现或变化的原消息；同一原消息内不能自动选择新候选，未变化的已展示候选可由后续用户消息选择。

所有live入口共用云端受理，持久化来源、归一身份、会话、原键、原文、原received_at/intake_date及受理序号。飞书在持久收件及SCF异步承接后ACK；乱序唤醒由消息写入者按持久序号处理，重复完成任务不消耗重试次数或重发旧回复。CLI/DSH取得持久结果并回报实际展示，提案只在对应入口交付后presented，只有飞书来源向飞书投递。

每次实际worker尝试90秒，最后5秒留给提交/交付；原received_at只用于端到端计时，不让旧收件过期失去恢复机会。平台重试及后台补查共用最多3次持久尝试，保留4次主决策/8次工具累计预算。结果未知时保留相关后续任务，核对原提交后恢复；旧任务不回写较新会话。提醒定时及人工恢复进入消息写入者，保留原计划时间/业务日期/固定投递键；同步端不占业务执行位。
