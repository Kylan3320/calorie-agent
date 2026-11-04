# Store 与原生工具契约

本文记录当前代码接口。回复、整轮完成及恢复语义见 [交流体验规格第2节](../CONVERSATION_EXPERIENCE_SPEC.md#2-回复完成与恢复)。

工具对象唯一注册于 `agent/tools/registry.py`：名称、description、Pydantic 参数 schema、metadata.risk、执行函数。每次 create_agent 请求按 presets 筛选该对象。

工具：record_meal、revise_intake、query_today、query_history、query_food_nutrition、query_knowledge、calculate_meal、update_user_settings。record_meal、revise_intake、update_user_settings 的风险分别为 write/destructive/write；其余 read_only。只读工具可以在允许写入预设中缓存食物资料，不创建摄入或个人设置。

整餐食物匹配保留名称、品牌、种类和生熟特征。名称末尾括号内仅重复所选单位规格时，只有基础名称唯一匹配、规格与单位括号一致（允许“中/中号”等号字后缀）、且该食物来源资料存在唯一对应单位，才按基础名称匹配；不因搜索仅一个结果消除实际歧义。

补全沿用原餐日期和未改动的条件。替换食物时清除旧匹配及候选；新克重与单位数量互相替代，避免旧 grams 遮蔽新 quantity/unit。候选编号仅引用原快照，传入食物名须指原查询或所选候选。latest 复用按本会话最近餐引用定位，只有明确给定来源日期才过滤来源；新增日期取本次消息。

原生工具输出 `{tool,call_id,action_id,status,data,error?}`。完整业务结果持久化于 calls.result，当前轮 ToolMessage 通过 model_view 提供精简事实并保留配对 call_id。主请求的历史读取使用 `conversation.history(for_model=True)`，每轮以SystemMessage背景组携带turn_id、reply_status、旧原意／清洁对话及定位references，旧命令不再以独立user消息进入模型。reply_status只从正常dialogue-delivered提交或对应receipt_state的已确认最终交付取“已回复”，缺依据为“未知”；不以轮次顺序或complete猜测交付。execution另从同一份已保存结果提取business_status、原生调用工具名及tool_results的工具名/status，不带旧数值；实际调用为空且无业务提交时明确标注仅答复、未修改。仅为已确认实际交付的草案保留draft_id及item_id/食物名/餐次等对象引用，不回填旧份量或营养；明确空工具结果与执行未知分开，已回复不推断已执行、业务成功、待办关闭或授权。历史业务回执原文／营养数字不回填，知识原文及出处沿原证据身份单独保留在该组。分组元数据用于整轮预算裁剪，不写回持久清洁来源、摘要或会话。当前原话及原生call_id／结果／reasoning沿原路径独立保留。助手以 `fact_scope=business/knowledge/dialogue` 记录来源，无标记且无知识工具来源的旧正文不进入新输入。实际语义见[历史业务轮次](../CONVERSATION_EXPERIENCE_SPEC.md#32-输入选择与超限)。

record_meal.data.total表示本次完成项合计；record_meal/revise_intake.data.meal_totals按meal_id返回原餐当前有效合计、日期/餐次、有效项数及estimated（任一有效项为估算）。改量保留原meal_id，明确可食克重重新保存称重依据。query_today.data.total表示全天，calculate_meal.data.total表示方案。部分记录分别携带实际items和unfinished。

record_meal的每项食物事实与record_id取自同一条实际写入后读回行；读回顺序没有与输入顺序一致的保证。异常进度回复按meal_id保留最新成功结果中的原餐合计，不把中间改量值作为当前值；后续成功记餐／改量使此前全天查询失效。

query_today参数detail默认为calories，读取当前日期的有效餐项并返回日期、有效项数、总热量及最小行依据，不读取目标；summary提供宏量/目标/剩余，items提供完整逐项明细。完整持久结果保存本次实际读取事实，未读字段不伪造；摄入写入快照保持完整。非成功结果保留实际错误。

query_today.items另从同一批已读有效行按meal_id提供meal_totals（形状同记餐/改量），不额外读取账本；total仍为全天合计。核对特定餐时按last_meal及行的meal_id定位，再用对应合计回答；整日查询的直接回执不能代替特定餐的确认。新消息明确记录相同餐食仍新增，平台重投依原消息ID/动作键防重，不按餐食内容去重。

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

ModelCallLimitMiddleware与Runtime._execute固定动作序号分别限制消息累计4次模型决策/8次工具实际执行；整批调用先保存、再按序执行，恢复复用 prepared 的原参数、日期及目标。FakeStore 通过相同文档接口和注入后台执行器验证摘要语义。

生产消息链路不访问 Bitable；来源资料通过迁移／维护发布 COS 快照，薄荷详情先提交 COS 后归档。LightRAG继续读取已发布索引。模型及工具前预算、整批原生调用、prepared和完整动作结果均保留原持久化边界。Bitable适配器的请求预取仅用于维护与旧实现检验，不属于生产入口链路。

执行日志 messages 保存原生恢复交换，实际 HTTP 请求另存于 model_steps[].request_body；session 和 prepared.session 保存业务状态，result 保存回复等元数据，重放从 calls 复原完整工具结果。新执行的 previous_session_message_id 与会话 last_message_id/last_execution_complete 用于拒绝旧任务覆盖较新会话；offered_message_id 绑定实际展示候选。

## 2026-11-04 任务接续与首次反馈（本地实现，待候选）

- 显式record_ids的保存常用餐及复用逐项验证本人有效记录，任一失效则整次pending，不覆盖原同名快照或产生部分摄入。failed_selection保留本次原选择及缺项供修订；普通整餐部分完成规则不变。
- query_food_nutrition返回options和source_ref，绑定归一用户/会话实际返回的食物或摄入快照。food_sources保存完整来源；food_offer_candidate带offer_id/message_id/有序options，实际展示后才成为food_offer。choice仅用于该已展示营养选项，不能消费记餐pending。无效引用返回可纠正结果。
- calculate_meal支持scope=daily/full_day/remaining（旧调用缺省remaining），items中item_id/group/servings/portion_basis/assumption/source_ref；份数只缩放一次。daily用长期目标不扣当日摄入，full_day用当日目标不扣已吃，remaining扣一次有效摄入；data返回targets/consumed/consumed_scope/total/budget_difference/complete/within_tolerance，未计入只作小计。份量依据区分user_given/suggested/assumed_takeout。
- meal_draft_candidate保存完整参数、计算、限制及draft_id/message_id；最终菜单由选中草案唯一渲染，不重复附模型清单；只有真实未解决的来源选择才展示并绑定food_offer。result.presented_candidates记录实际公开身份。mark_reply_presented核对当前消息及候选ID后提升meal_draft/food_offer，迟到/旧回执不回滚。草案修改传draft_id+replacements指定item_id，其余组合保留并重新计算。采用草案不等于摄入。
- query_history的continue_scope=true继承history_scope中省略的日期/餐次/food_query，显式空串可清筛选，显式period重算日期。三detail、持久结果、投影和回执保留实际筛选；仍按当前账本重查。
- 提醒缺条件时保存原关闭规则，reminder_pending保存rule_id/缺项/启用意图与原快照。后轮补缺重查原规则，只启用同一条；取消、禁用或快照冲突使旧启用意图失效。
- 八工具均可携带action_note（可选公开文本字段）；与finish_after_tools一样在批次控制层剥离，不改变业务args/action_id/prepared。Feishu live首批按原调用顺序取第一条只读调用上的有效说明，整批直接事实收尾则跳过；首批全空且从未投递时，首个后续含calculate_meal/query_knowledge的批次另给一次机会。每个机会随完整原始批次持久，普通账本/非文本/已完成陈述跳过，无补调模型。
- 行动说明发送前保存attempted，单次HTTP尝试最多1秒，受原业务截止；失败或未知继续业务、恢复跳过。确认message_id时将最终正文编辑到该消息，其他情况用独立最终UUID发送；mode/message_id/正文/摘要在最终API前同次保存。说明不触发来源完成、提案或草案展示确认。
- CosObjects.list(prefix,marker='',limit=None)按字典序严格在marker之后返回，limit指定时最多该数量。Journal暖态从可信水位只追增量；冷态有界块探测定位头、直接最近128检查点、最多四并发校验尾段后按序应用。过期LIST/冲突仍精确追读相邻不可覆盖提交，最低版本不满足/检查点不可见不冒充成功；提交格式与Store接口不变。


候选实际故障修复：wrap_tool_call将剥离完成声明/行动说明后的持久business args交给原生handler，避免同一控制字段被二次当作业务参数拒绝。其他未知业务参数仍由schema拒绝。工具上限以持久index为唯一执行守卫，不叠加每次invoke的框架工具计数器；超过8的固定调用保存结构化limit与终结原因、零业务执行，批次后形成确定终态并推进交付。框架旧ToolMessage.status=error的纯文本按明确错误身份包装，历史/投影不强行json解析。已保存终态恢复不再打开模型或过期动作。

营养/计算的候选投影保留source_ref及单位名称/可食重，不注入记餐choice编号；本轮模型可依据source_ref直接计算并明确假设。用户营养choice仍只接受前轮实际展示选项，记餐pending候选仍按原编号接续。单位来自实际来源的unit/unit_name/name，投影统一为unit_name，eat_weight保留原数值或数值字符串；展示时作数字换算，不修改来源缓存。

## R5 菜单选择与说明机会

PlanItem.fixed标注用户指定的固定组成，max_grams是明确的每份上限。calculate_meal先计算固定subtotal，再用targets−consumed−fixed生成有符号supplement_gap。可选adjustment由replacements/item_id和additions组成，仅改变非固定补充项，复用同一固定来源及scope；初稿与备选在一次工具调用内计算。工具按个人规格3.6校正已有suggested补充项的每份量，不改固定/用户量/来源/份数/上限，不查新来源或添食材。改稿仅指定替换项可调。portion_preference=smaller_portions仅在宏量规则并列后用最大每份克重、备餐项数打破平局。selection保留实际比较版本的来源/份量/总量/差额/完整性/容差/相对偏差和selected/reason；选择按个人规格3.6，不能以热量达标替代宏量比较。未选审计仅留持久工具事实，模型投影只保留比较指标，下一轮草案视图不再复制全审计。

meal_draft_candidate的items/calculation始终是选中版。固定餐、每餐清单、整日汇总只由该稿渲染一次；同条请求的已提交记录/统计回执和有据知识答复仍单独保留。没有新草案时，营养查询不能覆盖“无替换对象”等主答复。直接计算发现首个来源歧义时，从该稿真实unfinished.candidates生成公开offer身份；交付后才同时确认草案/选项。已解决的营养选项、未选对照及旧中间稿不作为可续选菜单展示。

journal.action_note_opportunities记录first/supplement各自batch_id/status/call_id/text，与response_batch_saved同次落盘；status为ready/empty/direct/attempted/budget_skipped。说明只在该ready批次确未开始时开放原机会；投递前将action_notice及机会attempted持久，receipt_state另保存同一批次/调用/机会和原收件身份。任一次成功/失败/未知均用掉全消息唯一尝试，不因第三个批次有文案重新开放；旧执行没有已落盘ready机会时不事后补发。说明的1秒墙钟、最终同MID更新/独立UUID及展示确认沿R4。

R6真实输入修复：营养候选features.per_100g仅包含该来源快照已有的四项每100克值，不猜缺值、不为候选追加HTTP。选择source_ref时省略food_query；错名仍pending，但纠错给被引来源原名/已有营养、同一个原ref及摄入快照份量，不能退化成另一通用食物引用。已提交计算稿在确定终态异常时也以同一稿呈现并附终止原因，仍保留error/terminal，且实际交付后才提升；未确认/较新稿/旧投递保护不变。

归档核对使用独立精确Decimal数值解码，数字/数字文本及单值wrapper按原请求精确比较；不调用业务一位舍入，不以近似相等确认版本。只有限合法整数的较高agent_revision能证明superseded。Feishu已保存3.26/27.96的原值可直接读回确认，ACK丢失不因此重复create/update；字段真不等仍不推进水位。

R7批量读源：query_food_nutrition.queries支持1–8个NutritionQuery，和顶层非空单项定位/份量互斥；元素不含action_note等控制字段。按序复用原单项计算、每项检查原deadline、共享会话来源但不创建摄入，完整返回data.queries[{query,status,data}]，一次原生调用/提交。不吞存储或预算异常、不增加传输重试。公开选项仅绑定批内首个未解决/多快照查询，后续已知项不能覆写它；同消息后续批已全部解决时清除旧候选，不重发已解决选项。模型投影只对batch去除options/unfinished中重复的同源profile，持久原结果不裁剪；异常回执仍保留逐项已知结果。八工具及原4决策/8工具上限不变。

新归档prepare的client_token绑定app/table/kind/id/revision的canonical JSON；目的地/版本不同操作不共享创建键，旧已保存inflight请求的token与字段保持。显式操作员修复旧blocked属于独立处置，必须冻结写入者、核对原sync版本/请求哈希与目标稳定ID、先保留原失败审计，再依固定新请求恢复；运行时代码不自动换token，不把人工恢复当健康归档时效样本。

## R8 菜单直接完成与定性说明

份量校正从原建议到最近容差边界按来源单位或10克档位生成候选，两项互补只扩至另一项步长溢出所需范围；没有日常单位且明确上限不足10克时可用正整克。按既有热量/宏量/份量顺序选版，不新增距离排序或全域宏量优化。selection增加portion_adjustment版本及calibration（可调ID、搜索范围、起始版本、原/选份量、原/选数量假设、前后差额）供核对。已有版本达容差不再校正，改稿只有显式suggested替换项可调；只给新grams/quantity且未声明basis时视为本次用户固定量。最终公开只展示选中份量，原份量与原假设留在审计，数量文字按同来源换算同步、做法/组成保留。

calculate_meal支持可选finish_after_tools，默认false，沿用批次持久控制及剥离规则。运行时只接受本会话已展示draft_id的单计算批次；结果必须完整、status=ok、无unfinished/question，且与本消息待展示草案身份一致。此前成功只读结果可作为依据；此前写入及非成功结果保留，不能用纯改稿直接回执丢掉它们。首次规划、旧执行无声明、缺来源或尚需说明/建议继续原生循环。行动说明仍在工具前尝试，菜单直接完成不抑制该说明。

需要模型补充的规划理由使用最终正文的`<plan_note>...</plan_note>`公开说明块；只写定性内容，具体份量和营养由选定草案统一展示。渲染保留定性句，定量句不重复交付并记录调整，业务/知识同条结果沿既有独立回执。该标签不进入实际公开正文，也不产生工具或额外决策。

最近餐引用仍用last_meal和refs，模型对明确“刚才”且其中食物唯一时直接调用revise_intake；工具按原ID读当前事实及版本，原prepared、一次提交与原餐合计不变。未指定哪一份的日期/餐次引用由原生循环先定位和消歧，不增加意图分类器或第九工具。

R9真实反例修复：距来源整数日常单位≤1克的模型建议量可按该单位生成校正候选，仍用来源精确重量计算。完整自由文本assumption留在持久工具事实与原始参数；公开只保留不含数量的组成/做法分句，数量以已解析餐项为准。带assumption的计划项标估算，固定餐带假设时不再称用户给量。模型请求的可信状态增加execution_budget.model_decision/model_decisions_after_this/tool_calls_remaining，来自原持久计数，不新增预算、路由或执行分支；用于规划保留末次正文决策，已达容差不为可选项继续查源。

R10固定量反例：不仅工具自动校正，原adjustment.replacements入口也只允许修改既有suggested项；user_given或assumed_takeout项的备选变更整次拒绝，selection.rejected_adjustment保留原因，原稿继续核算。不能先改basis/source/份数绕过用户固定量；八工具、固定动作与提交协议不变。
