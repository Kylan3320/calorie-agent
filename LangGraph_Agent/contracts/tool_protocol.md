# Store 与原生工具契约

工具对象唯一注册于 `agent/tools/registry.py`：名称、description、Pydantic 参数 schema、metadata.risk、执行函数。每次 create_agent 请求按 presets 筛选该对象；不维护 AgentPlan 或另一份工具说明。

工具：record_meal、revise_intake、query_today、query_history、query_food_nutrition、query_knowledge、calculate_meal、update_user_settings。前三种写能力的风险分别为 write/destructive/write；其余 read_only。只读工具可以在允许写入预设中缓存食物资料，不创建摄入或个人设置。

整餐食物匹配保留名称、品牌、种类和生熟特征。名称末尾括号内仅重复所选单位规格时，只有基础名称唯一匹配、规格与单位括号一致（允许“中/中号”等号字后缀）、且该食物来源资料存在唯一对应单位，才按基础名称匹配；不因搜索仅一个结果消除实际歧义。

原生工具输出 `{tool,call_id,action_id,status,data,error?}`。完整业务结果持久化于calls.result；模型通过统一model_view读取精简事实，标准ToolMessage保留配对call_id。个人设置只向模型提供相关目标/偏好及常用餐名称。

record_meal.data.total表示本次完成项合计；record_meal/revise_intake.data.meal_totals按meal_id返回原餐当前有效合计、日期/餐次、有效项数及estimated（任一有效项为估算）。改量保留原meal_id，明确可食克重重新保存称重依据。query_today.data.total表示全天，calculate_meal.data.total表示方案。部分记录分别携带实际items和unfinished。

query_today参数detail默认为calories，模型视图只提供日期、有效项数及总热量；summary提供宏量/目标/剩余，items提供逐项明细。完整持久结果始终保留全量事实，非成功结果保留实际错误。

模型依据工具事实自然表达，默认简短确认＋本餐热量，估算时说明，逐项营养按需展开。回复合成只解析本轮已声明来源引用，并追加参数明确的提案确认提示；异常时生成当前请求相关的简短事实回执。

提案固定proposal_id、动作/参数、目标摘要及response_id，交付成功后由入口标记presented。紧邻确认仅执行该提案的固定参数；已完成消息重放不覆盖更新会话。飞书发送成功、CLI输出和DSH响应交付使用同一mark_reply_presented语义，并归一用户别名。

## Store

BitableStore 与 FakeStore 同步实现：

- `load_foods()/cache_food(food)`：食物每100g宏量、可选来源热量、boohee_id、units、detail_loaded；旧数据缺来源热量用未舍入宏量公式。
- `intake_items(user_id,start_date="",end_date="",include_deleted=False)`：有效摄入快照明细，含 record_id/date/message_id/meal_type/grams/nutrition/snapshot。
- `write_intake(message,items,meal_type)`：一次批量创建多项，固定动作消息键防重。重试读回原行，不再创建。
- `revise_intake(user_id,record_id,item=None)`：按固定 ID 设置量/营养/快照；None 表示软删除。不重新选择最近记录。
- `today_summary()/summarize_history()`：按有效摄入快照汇总，不重算热量。
- `base_standard()/get_standard(user_id)`：逐字段覆盖项目默认→原标准→长期设置→当天设置。
- `load_document(kind,user_id,scope="")/save_document(kind,user_id,scope,payload)`：复用 pending_tasks；会话 agent_session、个人设置 user_settings、提醒投递 reminder_receipt。
- `load_session()/save_session(user_id,session_id,session)`：标准序列化消息（含摘要及配对工具消息）、unfinished、refs、last_meal、proposal；不依赖内存恢复。
- `list_documents(kind)`：提醒扫描个人规则。
- `begin_execution()/load_execution()/save_execution()/load_unfinished_executions()`：原消息/接收日期、原生消息、决策次数、完整工具批次参数、固定动作序号、prepared 写前快照、逐项结果、最终回复。创建/更新幂等见 bitable_schema。
- 保留别名、知识、usage 接口；usage 失败不阻断业务。

## 入口及预设

飞书 ACK 前保存收件原文、received_at 和 intake_date；同会话按已存收件顺序串行处理，前消息失败时后消息保留。执行与发送分开确认。最终回复先持久化，发送失败复用原回复及 uuid；`python app.py --retry-pending` 恢复收件、执行及发送。

DSH `POST /api/agent` 接收 text/session_id/message_id/preset；组合稳定消息键，按 session_id 隔离。`tool_events` 使用原生 call_id/args/result。/dsh 编码引擎保留，/calorie 调同一 runtime，不向飞书发送。CLI 交互进程内使用同一独立会话 ID。

live 真实业务/会话/usage；shadow_fakewrite 使用 FakeStore，不访问真实 Bitable；shadow_readonly 对真实 Store 不执行任何业务、缓存、会话、追问、usage 写入，状态只在当次运行保留。后两者不发送飞书消息。

## 限制和压缩

LangChain SummarizationMiddleware 默认12000 tokens触发，保留4000 tokens完整近期配对消息；配置在config.yaml。ModelCallLimitMiddleware/ToolCallLimitMiddleware限制每消息4次主决策/8次工具，处理预算90秒；无自动模型重试。摘要调用仅触发压缩时额外发生。先存整批调用，再按返回顺序执行；动作恢复复用 prepared 原参数、日期与目标。未生成的步骤由原生循环继续决定。
