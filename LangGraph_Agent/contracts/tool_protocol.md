# Store 与原生工具契约

本文记录当前代码接口。回复、整轮完成及恢复语义见 [交流体验规格第2节](../CONVERSATION_EXPERIENCE_SPEC.md#2-回复完成与恢复)。

工具对象唯一注册于 `agent/tools/registry.py`：名称、description、Pydantic 参数 schema、metadata.risk、执行函数。每次 create_agent 请求按 presets 筛选该对象。

工具：record_meal、revise_intake、query_today、query_history、query_food_nutrition、query_knowledge、calculate_meal、update_user_settings。record_meal、revise_intake、update_user_settings 的风险分别为 write/destructive/write；其余 read_only。只读工具可以在允许写入预设中缓存食物资料，不创建摄入或个人设置。

整餐食物匹配保留名称、品牌、种类和生熟特征。名称末尾括号内仅重复所选单位规格时，只有基础名称唯一匹配、规格与单位括号一致（允许“中/中号”等号字后缀）、且该食物来源资料存在唯一对应单位，才按基础名称匹配；不因搜索仅一个结果消除实际歧义。

补全沿用原餐日期和未改动的条件。替换食物时清除旧匹配及候选；新克重与单位数量互相替代，避免旧 grams 遮蔽新 quantity/unit。候选编号仅引用原快照，传入食物名须指原查询或所选候选。latest 复用按本会话最近餐引用定位，只有明确给定来源日期才过滤来源；新增日期取本次消息。

原生工具输出 `{tool,call_id,action_id,status,data,error?}`。完整业务结果持久化于 calls.result，当前轮 ToolMessage 通过 model_view 提供精简事实并保留配对 call_id。历史业务 ToolMessage 仅含 `status=historical` 及定位 references；业务 AI 回执不进入新输入。助手消息以 `fact_scope=business/knowledge/dialogue` 记录来源；无标记且无知识工具来源的旧正文不进入新输入。用户原话、独立状态及原执行审计保留。

record_meal.data.total表示本次完成项合计；record_meal/revise_intake.data.meal_totals按meal_id返回原餐当前有效合计、日期/餐次、有效项数及estimated（任一有效项为估算）。改量保留原meal_id，明确可食克重重新保存称重依据。query_today.data.total表示全天，calculate_meal.data.total表示方案。部分记录分别携带实际items和unfinished。

record_meal的每项食物事实与record_id取自同一条实际写入后读回行；读回顺序没有与输入顺序一致的保证。异常进度回复按meal_id保留最新成功结果中的原餐合计，不把中间改量值作为当前值；后续成功记餐／改量使此前全天查询失效。

query_today参数detail默认为calories，读取当前日期的有效餐项并返回日期、有效项数、总热量及最小行依据，不读取目标；summary提供宏量/目标/剩余，items提供完整逐项明细。完整持久结果保存本次实际读取事实，未读字段不伪造；摄入写入快照保持完整。非成功结果保留实际错误。

query_food_nutrition 显式 record_id 优先返回该摄入快照；一般食物查询给了 grams 或 quantity 时按本次数量计算，不被同名摄入覆盖。未指定份量时只在原消息完整当日范围查同名摄入。

record_meal、revise_intake、query_today、query_history 接受可选 finish_after_tools=false，控制标记独立保存于 calls，不进入业务 args、action_id 或 prepared 身份。满足 [完成条件](../CONVERSATION_EXPERIENCE_SPEC.md#22-直接回执的完成条件) 时从完整 calls.result 返回事实回执：reply_source=tool_facts、model_reply为空；其余继续原生循环。

提案固定proposal_id、动作/参数、目标摘要及response_id，交付成功后由入口标记presented。紧邻确认仅执行该提案的固定参数；已完成消息重放不覆盖更新会话。飞书发送成功、CLI输出和DSH响应交付使用同一mark_reply_presented语义，并归一用户别名。

修改提案保存 target_version，按目标的身份、日期、克重、营养、快照、状态及业务版本计算；相对改量在提案时冻结绝对克重并展示，另存 target_ratio 保留原换算及估算依据。确认核对原版本后复用快照缩放，prepared 保存绝对结果。已被后续消息作废或目标变化的提案不执行。旧修改提案缺少版本依据时需重新发起，已提交动作仍只重放原结果。

## Store

生产 CosStore 与测试 FakeStore 对齐以下业务结果；BitableStore 用于来源维护、归档和旧账本迁移：

- `load_foods()/cache_food(food)`：食物每100g宏量、可选来源热量、boohee_id、units、detail_loaded；旧数据缺来源热量用未舍入宏量公式。
- `intake_items(user_id,start_date="",end_date="",include_deleted=False)`：有效摄入快照明细，含 record_id/date/message_id/meal_type/grams/nutrition/snapshot。
- `write_intake(message,items,meal_type)`：一次批量创建多项，固定动作消息键防重。重试读回原行，不再创建。
- `revise_intake(user_id,record_id,item=None)`：按固定 ID 设置量/营养/快照；None 表示软删除。不重新选择最近记录。
- `today_summary()/summarize_history()`：按有效摄入快照汇总，不重算热量。
- `base_standard()/get_standard(user_id)`：逐字段覆盖项目默认→原标准→长期设置→当天设置。
- `load_document(kind,user_id,scope="")/save_document(kind,user_id,scope,payload)`：COS 按 kind/user/scope 保存独立文档（FakeStore 保留内存文档语义）；会话 agent_session、个人设置 user_settings、提醒投递 reminder_receipt。
- `load_session()/save_session(user_id,session_id,session)`：近期标准序列化清洁消息、unfinished、refs、last_meal、proposal；清洁来源与摘要独立保存于conversation_context/dialogue_turn文档，原生配对消息在执行日志恢复。
- `list_documents(kind)`：提醒扫描个人规则。
- `begin_execution()/load_execution()/save_execution()/load_unfinished_executions()`：原消息/接收日期、原生消息、决策次数、完整工具批次参数、固定动作序号、prepared 写前快照、逐项结果、最终回复。创建/更新幂等见 bitable_schema。
- 保留别名、知识、usage 接口；usage 失败不阻断业务。

## 入口及预设

所有 live 入口通过独立受理函数提交来源、归一身份、会话、原消息键、原文、原 received_at、intake_date 和持久序号。重复受理保留原记录与序号。飞书持久收件且 SCF Event Invoke 接受后 ACK；承接失败返回503。消息 worker 按会话持久序号处理；入口、消息及归档各平台并发1。异步事件只引用原收件并携带 ingress_revision，按确切版本读回校验身份。

CLI／控制台／DSH 的 live 使用 `agent/storage/live_client.py` 经服务端 `/internal/intake`、`/internal/result`、`/internal/presented`；配置 CALORIE_SERVICE_URL/TOKEN、本人的 CLI_USER_ID 或 CONSOLE_USER_ID。保持各入口现有输出、session_id 和原生 tool_events。飞书来源才向飞书投递；CLI输出、DSH实际追加回复后回报 saved UUID，由消息写入者标记对应提案 presented。只读或 fake 预设在本地 Runtime运行。

live 使用 COS 当前事实；shadow_fakewrite 使用 FakeStore；shadow_readonly 仅读取可见已发布 COS 提交并返回版本，不创建重建确认点、不写COS／Bitable／缓存／会话／usage、不投递。

等待、重试及到期恢复遵循 [交流体验规格第2节](../CONVERSATION_EXPERIENCE_SPEC.md#2-回复完成与恢复)。执行日志保存固定 deadline_at、legacy_budget、model_steps 及累计调用预算；步骤 request_body 和 response 用于恢复同一模型请求，不进入公共 trace。

进度正文提升为最终正文时，同次保存新结果及 result_digest。Feishu 更新原 message_id；CLI／DSH 按正文摘要确认，旧展示事件记录 presentation_rejected 后结束，不阻塞后续业务。最终交付独立保存 delivery.recovery_attempts；终止说明进入清洁对话。

运行时外部HTTP共用 `model_transport` 的绝对截止传输，覆盖连接、头、保活和完整 body；薄荷两次尝试与退避共享10秒逻辑RPC预算，SCF派发单次2秒，均不超过当前任务剩余时间。LightRAG整体85秒（含构建／初始化）且不超过当前业务剩余时间，关键词模型与embedding线程继承该截止。LiveClient每次请求最多10秒，submit的受理、轮询与等待共用从受理前开始的总预算，超时以原消息键恢复。COS 保留 SDK 签名、MD5、禁止覆盖及 retry=0。维护先唤醒未完收件再归档；提醒按原计划及逐规则进度恢复。

### COS 提交与归档

`CosStore(objects,prefix,offset_hours=8,readonly=False)` 采用 `message/` 提交范围；`confirm_current()` 在消息执行前确认最新状态。`Journal` 为 ingress、message、sync 分别保存连续编号、前驱和内容寻址文档引用；每128版本保存引用索引快照。空确认点只保存版本关系，普通提交只保存变化文档。按会话和日期／餐项取文档，不逐保存复制全库。LIST只定位候选，禁止覆盖创建下一编号遇冲突必须精确追读；已知版本不可见或提交结果未知时停止，重新确认后恢复。同值保存不新增提交。桶从未开启版本控制，保留恢复链。 暖缓存的已知连续增量与独立不可变文档最多4路并发传输；全部精确读取校验后按序应用，全部文档上传收束成功后才发布唯一提交，错误使实例停止服务。基线批来源 `source:foods` 按内容哈希预填本地缓存，实际食物仍由当前 `food:` 索引选择版本；单项来源更新不重写批快照。

Runtime 使用生产Store的 `commit_action(action_id, callback)` 将固定业务变化、稳定餐项ID、完整工具结果、执行进度和归档版本原子提交；重放使用原结果，提交后才成功。新餐项ID由action_id与首次项序号派生，导入保留原飞书record_id。`intake_by_ids`／`intake_for_action`用于固定目标定位，其他Store可沿原明细接口读取。

`committed_action(action_id)`只读原提交结果，不执行callback；确认屏障未完成须抛错，不能冒充未提交。FakeStore使用同一commit_action/committed_action语义；失败回滚未提交内存业务，flush_preparation后的准备事实可恢复。

补全／取消的 prepared.pending_before 保存所引用待办的原状态；首次执行和恢复均检查同一待办尚未变化、完成或取消，同一动作不能重复消费一个 pending_id。待办变化与业务结果同次提交，只合并 unfinished、refs 及仍指原餐的 last_meal，不替换较新会话的正文或 last_message_id。提交失败回滚内存中未提交的结果，保留已保存的准备事实。旧未提交补全准备缺少 pending_before 时要求用户重新发起，避免沿用可能错误的旧食物／份量；普通固定动作恢复不受影响。

模型工具参数校验错误作为结构化 error 工具结果进入原生循环，允许模型在原预算内纠正；不对框架的普通错误文本强行解析 JSON。网络／存储等执行异常中止本次后续排队动作，保留原固定动作供新执行尝试恢复。

COS 文档保留原散列身份，document_index:<kind> 保存按类型的键列表，document_index:ready 保存排序后 document: 键集合的 SHA256。缺失、旧布尔标记或覆盖摘要变化时重建，因此回退旧写入器新增文档后再升级不会漏列；写入者与本次保存原子发布，只读状态仅在内存重建，零写入。覆盖一致时 list_documents(kind) 只扫描键元数据，不读取其他类型正文；普通正文更新不重写索引。内容缓存统一按 LRU 限制为512项及8 MiB编码数据，超大文档直接读取COS；淘汰缓存不删除持久历史。

仅摄入、食物、个人设置业务值变化创建 `archive:<kind>:<id>` 最新结果并推进该对象业务版本。会话、收件、模型审计、交付等元数据不推进账本版本；feishu_chat_id为运行路由。同步函数只写 `sync/`，先保存inflight绝对字段与固定client_token，响应丢失先核对原稳定ID，读回字段及版本后确认水位；尚未发出的同项改量合并。COS当前事实不因归档成功删除。迁移按 `migration.export_cos_baseline`／`import_cos_baseline`，独立核对含软删除ID、日合计和幂等任务；已交付旧收件转换为completed receipt_state，重投不重发旧回执。

## 上下文文档与查询投影

请求预算与历史选择遵循 [交流体验规格第3节](../CONVERSATION_EXPERIENCE_SPEC.md#3-完整请求预算)。计量最终 HTTP body，本地 wire 计量与供应商 usage 分开保存；动态状态每次重建，投影不覆盖来源或恢复审计。编码身份见 agent/encoding/version.json，工程默认值见 config.yaml。

query_history.detail默认为summary，只向模型提供范围/count/total；daily额外提供[start_date,end_date)内每个日期的count/total/status，unrecorded仅表示未记录；items完整提供可操作明细。三者来自同一筛选集合，持久完整结果保留实际items；历史定位/复用一次调用items。知识完整结果保留原文，模型只接收entries的一份原文及对应source_id/source，不重复evidence。

conversation_context(user,chat)保存摘要身份、fact_scope=dialogue、覆盖游标及未覆盖轮次ID/交付状态/token数；dialogue_turn(user,turn_id)按固定用户消息ID保存清洁轮次。agent_session.messages仅保存近期清洁兼容视图，原生恢复仍读执行日志。旧有效dialogue摘要及现存清洁轮次首次升级为基准和后缀，错误占位与无来源标记摘要不成为基准。来源初始化不重放业务。

消息端确认交付／展示后将 dialogue_turn 标记 complete，在 summary_request(user,chat) 保存任务，summary_active(system) 保存活动索引。计算进度、连续前缀输入快照和候选位于 sync Journal；候选携带 task_id/user_id/chat_id/base_id/turn_ids/source_hash。调度及失败次数按 [后台摘要规则](../CONVERSATION_EXPERIENCE_SPEC.md#5-后台话题摘要)。

后台通过 calorie_summary 控制事件交付已存候选，由消息写入者核对基准与完整前缀后原子应用。该事件不经 accept，不增加业务序号、用户轮次或调用预算，不改变 last_message_id／presented；应用 action key 保证幂等。

硬预算超限保存 terminal 结果及未执行动作终止原因，Pipeline 交付后设置 work_status=terminated 并推进游标；重投取原回执。完整原输入留在收件／执行审计，后续清洁历史使用终止说明。

ModelCallLimitMiddleware／ToolCallLimitMiddleware 使用消息累计预算；整批调用先保存、再按序执行，恢复复用 prepared 的原参数、日期及目标。FakeStore 通过相同文档接口和注入后台执行器验证摘要语义。

生产消息链路不访问 Bitable；来源资料通过迁移／维护发布 COS 快照，薄荷详情先提交 COS 后归档。LightRAG继续读取已发布索引。模型及工具前预算、整批原生调用、prepared和完整动作结果均保留原持久化边界。Bitable适配器的请求预取仅用于维护与旧实现检验，不属于生产入口链路。

执行日志 messages 保存原生恢复交换，实际 HTTP 请求另存于 model_steps[].request_body；session 和 prepared.session 保存业务状态，result 保存回复等元数据，重放从 calls 复原完整工具结果。新执行的 previous_session_message_id 与会话 last_message_id/last_execution_complete 用于拒绝旧任务覆盖较新会话；offered_message_id 绑定实际展示候选。
