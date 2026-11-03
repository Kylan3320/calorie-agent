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

record_meal、revise_intake、query_today、query_history 可选 finish_after_tools=false；模型仅在本批足以回应完整请求（含缺项问询）时显式置true。控制标记独立保存在calls中，不进入业务参数、action_id或prepared身份。全批均明确true、只含四工具、所有结果已持久保存且结构完备，才从完整calls.result合成正常事实回执（reply_source=tool_facts、model_reply为空、非fallback）；pending/部分完成须区分已完成和缺项，候选与日期绑定保持。其他情况继续create_agent原生循环，不新增分类调用。多批保留此前写入事实，以最新meal_totals为准；写后过期的查询不能回答。知识、解释及建议继续由模型表达。来源引用与提案确认提示仍沿用原实现。

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

自然语言消息deadline固定为原received_at+90秒，含排队、保存与交付；最后5秒不开始新模型或业务动作。旧收件缺时间时首次接管保存一次legacy起点，T_service为空。语义模型步骤从首次投影/准备起最多40秒，首次预留、固定deadline及全部传输尝试可靠保存，恢复不续期。HTTP连接/超时/429/5xx仅在模型适配器额外尝试1次，同一wire及截止；其他拒绝/截断不盲重试。累计4主决策/8工具不变，传输重试不增加语义决策。

模型耗尽或业务截止后，只核对原action并交付已有正文/事实；确定未提交的动作终结，不因模型失败进入needs_attention。未知提交仍阻塞，不能当成回滚。每次必要核对/交付最多90秒且受平台剩余限制，最多3次自动尝试；已存正文只恢复同UUID交付。旧进度正文改为最终正文时，同次保存新结果及result_digest，Feishu覆盖原message_id，CLI/DSH按正文摘要确认；拒绝旧展示事件本身终结，不阻塞后续业务。提升后的最终交付单独记录delivery.recovery_attempts，不能获得新模型/业务预算。终止说明进入清洁对话，业务数字仍不成为历史事实。旧任务不得覆盖较新会话。

Feishu及COS复用绝对截止HTTP传输，实际连接、响应头、保活及完整body共用RPC墙钟；COS保留SDK签名/MD5/禁止覆盖及retry=0，失败仍须按原提交键恢复。每5分钟维护先唤醒未完收件再归档。提醒独立预算及原计划/逐规则进度保持；三次投递失败终结自动阶段、推进队列，关闭规则后人工恢复结束检查。

### COS 提交与归档

`CosStore(objects,prefix,offset_hours=8,readonly=False)` 采用 `message/` 提交范围；`confirm_current()` 在消息执行前确认最新状态。`Journal` 为 ingress、message、sync 分别保存连续编号、前驱和内容寻址文档引用；每128版本保存引用索引快照。空确认点只保存版本关系，普通提交只保存变化文档。按会话和日期／餐项取文档，不逐保存复制全库。LIST只定位候选，禁止覆盖创建下一编号遇冲突必须精确追读；已知版本不可见或提交结果未知时停止，重新确认后恢复。同值保存不新增提交。桶从未开启版本控制，保留恢复链。 暖缓存的已知连续增量与独立不可变文档最多4路并发传输；全部精确读取校验后按序应用，全部文档上传收束成功后才发布唯一提交，错误使实例停止服务。基线批来源 `source:foods` 按内容哈希预填本地缓存，实际食物仍由当前 `food:` 索引选择版本；单项来源更新不重写批快照。

Runtime 使用生产Store的 `commit_action(action_id, callback)` 将固定业务变化、稳定餐项ID、完整工具结果、执行进度和归档版本原子提交；重放使用原结果，提交后才成功。新餐项ID由action_id与首次项序号派生，导入保留原飞书record_id。`intake_by_ids`／`intake_for_action`用于固定目标定位，其他Store可沿原明细接口读取。

`committed_action(action_id)`只读原提交结果，不执行callback；确认屏障未完成须抛错，不能冒充未提交。FakeStore使用同一commit_action/committed_action语义；失败回滚未提交内存业务，flush_preparation后的准备事实可恢复。

补全／取消的 prepared.pending_before 保存所引用待办的原状态；首次执行和恢复均检查同一待办尚未变化、完成或取消，同一动作不能重复消费一个 pending_id。待办变化与业务结果同次提交，只合并 unfinished、refs 及仍指原餐的 last_meal，不替换较新会话的正文或 last_message_id。提交失败回滚内存中未提交的结果，保留已保存的准备事实。旧未提交补全准备缺少 pending_before 时要求用户重新发起，避免沿用可能错误的旧食物／份量；普通固定动作恢复不受影响。

模型工具参数校验错误作为结构化 error 工具结果进入原生循环，允许模型在原预算内纠正；不对框架的普通错误文本强行解析 JSON。网络／存储等执行异常中止本次后续排队动作，保留原固定动作供新执行尝试恢复。

COS 文档保留原散列身份，document_index:<kind> 保存按类型的键列表，document_index:ready 保存排序后 document: 键集合的 SHA256。缺失、旧布尔标记或覆盖摘要变化时重建，因此回退旧写入器新增文档后再升级不会漏列；写入者与本次保存原子发布，只读状态仅在内存重建，零写入。覆盖一致时 list_documents(kind) 只扫描键元数据，不读取其他类型正文；普通正文更新不重写索引。内容缓存统一按 LRU 限制为512项及8 MiB编码数据，超大文档直接读取COS；淘汰缓存不删除持久历史。

仅摄入、食物、个人设置业务值变化创建 `archive:<kind>:<id>` 最新结果并推进该对象业务版本。会话、收件、模型审计、交付等元数据不推进账本版本；feishu_chat_id为运行路由。同步函数只写 `sync/`，先保存inflight绝对字段与固定client_token，响应丢失先核对原稳定ID，读回字段及版本后确认水位；尚未发出的同项改量合并。COS当前事实不因归档成功删除。迁移按 `migration.export_cos_baseline`／`import_cos_baseline`，独立核对含软删除ID、日合计和幂等任务；已交付旧收件转换为completed receipt_state，重投不重发旧回执。

## 当前实现的限制和压缩

上下文预算在每次最终模型 HTTP body 组装后检查；本地 DeepSeek tokenizer 对 wire JSON 的输入分项计量与供应商 usage 分开保存。主请求目标16000，max_tokens=8192；摘要输入目标12000、max_tokens=4096，正文上限1024；硬输入上限=context_window-output_reserve-2048，当前官方端点绑定窗口1048576。当前整轮原生调用、固定call_id、原用户消息及协议必需reasoning完整保留，历史清洁表示不携带原生工具调用。动态状态每次调用重建；裁剪仅用于本次投影，不替换来源或执行审计。无前台同步摘要或旧summary_trigger_tokens/summary_keep_tokens策略。

query_history.detail默认为summary，只向模型提供范围/count/total；daily额外提供[start_date,end_date)内每个日期的count/total/status，unrecorded仅表示未记录；items完整提供可操作明细。三者来自同一筛选集合，持久完整结果保留实际items；历史定位/复用一次调用items。知识完整结果保留原文，模型只接收entries的一份原文及对应source_id/source，不重复evidence。

conversation_context(user,chat)保存摘要身份、fact_scope=dialogue、覆盖游标及未覆盖轮次ID/交付状态/token数；dialogue_turn(user,turn_id)按固定用户消息ID保存清洁轮次。agent_session.messages仅保存近期清洁兼容视图，原生恢复仍读执行日志。旧有效dialogue摘要及现存清洁轮次首次升级为基准和后缀，错误占位与无来源标记摘要不成为基准。来源初始化不重放业务。

消息写入者在实际交付/展示确认后标记轮次complete，按最近2轮之外未覆盖前缀达到4000登记summary_request及summary_active索引；每会话仅一活动需求，合并保留最早登记顺序。后台复用sync Journal，输入快照保持连续前缀且有界，最多2次计算；成功未应用只重送已存候选，终止失败移除额外input引用，消息端结束活动需求，无新完成轮次不重建同一失败。

五分钟维护先恢复收件/维护/归档，无待处理错误且至少剩25秒时至多计算一会话；摘要最多20秒、留5秒保存退出，Linux Event入口使用整次及嵌套墙钟截止时间。后台不写session。calorie_summary控制事件携带已存候选及task_id/user/chat/base_id/turn_ids/source_hash，消息写入者核对基准与完整前缀后原子应用，不经accept、不增加收件/业务序号/用户轮次/模型预算，不改变last_message_id或提案presented。新追加消息保留；独立context文档不会被旧session保存覆盖。应用action key保证幂等。

硬预算超限不发HTTP，标记terminal并保存原回执与已提交事实，未执行动作终止；Pipeline完成交付、推进游标、work_status=terminated，无自动重试或needs_attention阻塞。重投返回原结果；后续清洁历史仅携带超限终态说明，原文仍在收件/执行审计。未知写入先走既有恢复核对，不能因预算超限断言失败。

ModelCallLimitMiddleware/ToolCallLimitMiddleware继续限制每消息4次主决策/8次工具，处理预算90秒；先存整批调用，再按序执行，恢复复用prepared原参数、日期与目标。shadow_readonly只读有效摘要及可见状态，零需求/候选/事件写入；FakeStore通过同一文档接口及注入后台执行器验证语义。

生产消息链路不访问 Bitable；来源资料通过迁移／维护发布 COS 快照，薄荷详情先提交 COS 后归档。LightRAG继续读取已发布索引。模型及工具前预算、整批原生调用、prepared和完整动作结果均保留原持久化边界。Bitable适配器的请求预取仅用于维护与旧实现检验，不属于生产入口链路。

执行日志的 messages 保存实际模型视图；session 和 prepared.session 保存业务状态，result 保存回复等元数据，重放从 calls 复原完整工具结果。新执行保存 previous_session_message_id；会话的 last_message_id/last_execution_complete 用于拒绝旧任务覆盖更新会话。未完项的 offered_message_id 绑定已展示候选，同一消息不能自动选择本轮新候选。
