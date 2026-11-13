# Store 与原生工具契约

本文描述仓库接口。阶段 C 的通用来源、Router／Worker、分页索引和回收候选见下文，产品要求见[统一规格第10节](../PERSONAL_ASSISTANT_SPEC.md#10-下一阶段-c通用事务与可靠运行)及[COS 规格](../LATENCY_OPTIMIZATION_SPEC.md)。验收与部署状态见[交付记录](../reports/STAGE_C_DELIVERY.md)。

回复、整轮完成和恢复规则见[交流体验规格第 2 节](../CONVERSATION_EXPERIENCE_SPEC.md#2-回复完成与恢复)。字段的观测口径见 [Trace 契约](trace_schema.md)。

控制权、份量补证、记忆与持续委托的产品行为见[统一规格](../PERSONAL_ASSISTANT_SPEC.md)。生产能力以[交付记录](../reports/LATENCY_DELIVERY.md)为准。

## 工具注册与执行

- `agent/tools/registry.py` 定义名称、说明、Pydantic 参数、`metadata.risk` 和执行函数。每次 `create_agent` 请求按预设选用注册对象。`record_meal`、`update_user_settings`、`manage_diet_goal` 为 `write`，`revise_intake` 为 `destructive`。其余为 `read_only`，在可写预设中可以缓存食物资料。`submit_goal_result` 仅由受限委托运行装载。
- 原生工具返回 `{tool,call_id,action_id,status,data,error?}`。整批先保存原生调用和固定序号，再执行。完整结果存于 `calls.result`。本轮 `ToolMessage` 按 `model_view` 投影并保留配对 `call_id`。参数错误以结构化 `error` 返回，允许在原预算内纠正。旧框架 `ToolMessage.status=error` 的纯文本按错误身份包装，保留原文审计。网络或存储异常中止后续排队动作，原固定动作可恢复。
- 投影附 `feedback.{outcome,unmet_conditions,remaining}`，给出事实进度、缺口代码及关联引用、剩余决策／工具／网页请求与时间。模型选择下一步。发现尚需语义判断的新缺口时，本批原有完成声明不自动生效。
- 每条消息累计最多 4 次主决策和 8 次工具执行。超过上限的固定调用按持久 `index` 保存 `limit` 和终结原因，跳过业务执行。预算确定超限时保存终态回执和未执行原因，交付后推进游标。重投返回原回执。
- 任一尚未执行批次可提供 `action_note`，每条收件最多尝试投递一次。四个账本工具和 `calculate_meal` 接受 `finish_after_tools`，缺省为 false。控制字段随批次持久保存，执行保持原 `action_id` 和 `prepared` 身份。
- 直接回执使用完整 `calls.result`，返回 `reply_source=tool_facts` 和空 `model_reply`。纯改稿或采用已核算候选须绑定本轮待展示草案，结果完整且无剩余缺口。此前成功的只读结果可作为依据，此前写入及非成功结果仍参与完成核对。

## 摄入、历史与食物来源

- 整餐食物匹配保留名称、品牌、种类和生熟特征。只有基础名称唯一匹配、末尾括号规格与所选单位一致（兼容“中／中号”）且来源有唯一对应单位时，才可用去括号名称匹配。单条搜索结果不能消除实际歧义。
- 补全沿用原餐日期和未修改条件。替换食物清除旧匹配和候选。新克重与单位数量互斥，`choice` 绑定原待办快照，食物名须指原查询或所选候选。`latest` 按本会话最近餐定位，显式来源日期才过滤来源，新增日期取本次消息。保存常用餐或复用显式 `record_ids` 时逐项验证本人有效记录，任一失效整次 `pending`，由 `failed_selection` 保留原选择及缺项。
- `record_meal.data.total` 为本次完成项合计，部分完成分别返回实际 `items` 和 `unfinished`。每项事实与 `record_id` 来自同一条实际读回行，输入与读回顺序可不同。记餐和改量的 `meal_totals` 按 `meal_id` 返回原餐当前有效合计、日期／餐次、有效项数和任一项估算标记。异常回执取每餐最新成功结果，后续成功写入使先前的全天查询失效。
- 明确指向 `last_meal`／`refs` 中唯一摄入项时，`revise_intake` 按原 ID 和版本修改，保留 `meal_id`，用户给出可食克重时更新称重依据。范围不唯一时先定位。新消息明确记录同款餐仍新增，平台重投按原消息键和动作键防重。
- `query_today.detail` 默认 `calories`，返回日期、有效项数、总热量和最小行依据，无目标读取。`summary` 提供宏量、目标及剩余。`items` 返回完整明细，并从同批行计算 `meal_totals`。全天 `total` 与特定餐合计分别回答，核对旧餐用 `last_meal` 和 `meal_id` 定位。持久结果保留本次实读字段和错误。
- `query_history.detail` 默认 `summary`，模型可见范围、计数及合计。`daily` 展示 `[start_date,end_date)` 各天的计数、合计及状态，`unrecorded` 表示未记录。`items` 供定位或复用，返回可操作明细。三种视图使用相同筛选集合，持久结果保留实际行。`continue_scope=true` 继承 `history_scope` 中省略的日期、餐次、食物筛选，显式空餐次／食物清除筛选，显式 `period` 重算日期。续查读取当前账本。
- `query_food_nutrition` 的显式 `record_id` 优先读取摄入快照。一般查询给出 `grams` 或 `quantity` 时按指定数量计算，未指定时只在原消息当日范围查同名摄入。`queries` 接受 1–8 项 `NutritionQuery`，与顶层非空单项参数互斥。元素只含业务参数，按序共享会话来源和原截止时间，完整返回 `data.queries[{query,status,data}]`。整批只占一次原生调用，不吞存储和预算异常。
- 营养选项的 `source_ref` 绑定归一用户、会话及实际食物或摄入快照，完整来源在 `food_sources`。`features.per_100g` 只取来源已有值。单位 `unit`／`unit_name`／`name` 归一为 `unit_name`，兼容数值或数值字符串 `eat_weight`，展示换算不改缓存。选择 `source_ref` 时省略 `food_query`。错名返回 `pending` 及原引用、来源和快照份量，供纠正。
- `food_offer_candidate` 保存 `offer_id`、`message_id`、有序选项。批查只绑定首个未解决查询，后续全部解决则清除旧选项。仅展示确认后的 `food_offer` 接受营养 `choice`，与记餐待办编号独立。批查模型投影去除同源重复 profile，持久结果和异常回执保留各项事实。
- `query_food_nutrition.portion_web` 支持 `search(query)`、`fetch(urls)`、`read_cache(ref)`。同一次可附 `food_query` 定位营养来源，分别返回营养和份量证据。取文接受本消息用户提供的公开 URL 或本消息搜索返回的 URL。搜索和取文共享 3 次实际 HTTP 额度，发送前预留并随原执行恢复，重复请求与缓存复用不产生新 HTTP。官方凭据用 `DEEPSEEK_SEARCH_API_KEY`，推理地址为官方时可复用 `DEEPSEEK_API_KEY`。
- `record_meal.items[].portion_evidence` 使用本轮正文或已读有效缓存引用，保留逐字引文、食物与单位、可食口径和原数值。工具核验后换算写入快照。可用本会话实际返回的 `source_ref` 定位营养来源。现有待办沿原 `pending_id` 补全，保留日期、餐次与已知数量。精确要求禁用估算。用户证据缓存按原获取时间计，首期有效期七天。

## 菜单、提案与提醒

- `calculate_meal.scope` 为 `daily`（长期目标）、`full_day`（当日目标）或 `remaining`（当日目标减一次有效摄入）。旧调用省略时按 `remaining`，改稿省略时沿草案范围。餐项含 `item_id/group/servings/portion_basis/assumption/source_ref`，份数只缩放一次。结果含 `targets/consumed/consumed_scope/total/budget_difference/complete/within_tolerance`，未计入项只计已有小计。
- `fixed` 标注固定组成，`max_grams` 为每份上限。结果保存 `fixed_subtotal/fixed_complete/supplement_gap`，缺口按目标减已计摄入和固定小计计算。初稿与 `adjustment` 的 `replacements/item_id`、`additions` 在同次调用比较，复用固定来源和范围。备选只能替换非固定 `suggested` 项。改动 `user_given` 或 `assumed_takeout` 时整份备选被拒，`selection.rejected_adjustment` 留原因并继续核算原稿。只给新数量、未声明依据的改稿按用户固定量处理。
- 份量校正、热量容差和宏量选版见[个人规格第 3.6 节](../PERSONAL_CALORIE_SPEC.md#36-饮食计划与草案)。`portion_preference=smaller_portions` 在宏量并列时比较最大每份克重及备餐项数。候选使用来源单位或 10 克档位，建议量距来源整数单位不超过 1 克时按来源精确重量核算。无日常单位且上限不足 10 克时使用正整克。已有版本达容差时不搜索，改稿只调整明确允许的替换项，复用来源且不加食材、外部查询或模型步骤。
- `selection_mode=compare` 返回本轮候选 `candidate_ref`、餐项、差额和默认建议。采用前不会形成待展示草案。后续 `calculate_meal(candidate_ref=...)` 核对本轮、目标、摄入及依赖后准备选中稿。`use_default_calibration=false` 可关闭默认调量。已选稿的数量由工具事实渲染，定性说明可使用 `<plan_note>...</plan_note>`。
- `meal_draft_candidate` 保存选中稿的参数、计算、限制及 `draft_id/message_id`，`result.presented_candidates` 只保存实际发布的候选身份。缺来源时用实际 `unfinished.candidates` 形成待选项。终态异常可以交付已核算草案并保留原错误状态。交付后 `mark_reply_presented` 按消息和候选 ID 提升 `meal_draft/food_offer`，旧展示不得覆盖新稿。改稿按 `draft_id+replacements.item_id` 替换指定项，保留其余限制。未找到替换项时保留原稿及主答复。计划和采用草案均不写摄入。
- 跨会话续改用 `calculate_meal.draft_ref`，与本会话 `draft_id` 互斥。引用要求已实际交付且保有重算所需食物快照。继承源稿范围，本次指定覆盖。新日期按本次收件或用户指定确定，`remaining` 只扣该日有效摄入一次。`condition_refs` 和 `experience_refs` 分开保存，按有效状态投影；新稿交付后才保存可回查版本及绑定当前会话。
- 提案保存 `proposal_id`、固定参数、目标版本和 `response_id`，实际交付后标记 `presented`。模型理解当前自然表达，确认或取消绑定紧邻提案及本消息 `authorization_quote`。确认执行原参数，修改参数走明确的新请求。摄入或设置目标变化时拒绝旧确认。相对改量冻结绝对克重，旧准备与已提交动作按原恢复边界处理。
- 提醒缺条件时保存原规则为关闭，`reminder_pending` 保存 `rule_id`、缺项、启用意图和快照。补缺时重查原规则。取消、禁用或快照冲突会使旧意图失效。

## 个人记忆与经验

- `query_memory.search` 支持关键词排序、原句匹配、按类型浏览、日期和分页。无匹配与旧覆盖状态分别返回。`read` 按 `kind:identity:version` 在归一用户命名空间解析，分别报告未知、已知旧来源未覆盖、撤回和存储错误。主题关联沿同一读操作展开。
- `update_user_settings.condition` 保存、修订或结束明确条件。`feedback` 可用本消息原话关联并立即停用明确被反证的经验。`memory` 按当前版本撤回，设置变化仍写原权威对象。普通反馈通过 `review_sources` 登记复盘。
- `memory_object` 按用户与引用保存版本，`memory_index` 保存用户级索引与覆盖状态，`memory_state` 保存现行版本、撤回截止版本、条件／主题入口及派生关联。对话、草案和记忆对象不进入 Bitable 归档。撤回标记约束历史投影、检索、摘要、经验发布和回填。明确重新保存设置会产生新版本。摘要应用额外核对撤回版本。
- `review_sources` 是模型文本或原生工具响应正文末尾的保留控制区，携带 `[{ref,reason}]`。宿主剥离后绑定 `current_turn` 或本轮已读引用，确认交付后才登记到 `memory_work`。每次维护在摘要和经验中选一项。经验的继续决策、传输及重基准共享累计 20 秒、4,096 生成 token、两次决策与四次只读检索。固定输入、补读摘要及候选持久保存；重投只恢复应用。消息写入者复核用户、来源、版本及补读摘要后原子发布。
- `calorie_memory_backfill` 是消息写入者的按用户续作入口，参数为 `user_id` 和可选 `limit`（每页 1–100，默认 25）。只接收有交付证据的清洁来源和实际保有的结构化草案，保存进度及缺失范围，禁止从旧回复补造事实或启动经验学习。生产执行须另有授权。

## 持续饮食委托

- `manage_diet_goal` 用当前 `user_quote` 授权。宿主绑定本轮看见的 `goal_id` 与 `revision`，实际提交仍核对归一用户和版本。`start` 接收委托目的、日期、完成标准、条件、偏好、授权触发和可选通知目的地。资料不足时保存筹备并给出缺口。`revise` 可用 `change_dates` 限定本次核算和条件。新锁定餐次累加，解除需 `unlock`。`stop_run`、`set_auto`、`pause`、`resume`、`set_notifications`、`end` 各有独立效果。`request_run` 可在关闭自动触发时发起检查。
- `query_diet_goal` 默认只查状态和版本。`show_latest` / `show_delivered` 使用当前用户明确要求查看的 `view_quote`，由实际已算的计划渲染正文。`goal_delivery_ref` 随显示最新版的回复保存，只有相应回复交付确认后才推进可接续版本。
- 委托状态、归属、运行与计划版本存 `diet_goal`、`diet_goal_owner`、`diet_goal_run` 和 `diet_goal_plan`；`diet_goal_active` 供维护扫描，`diet_goal_event` 按触发来源去重。消息写入者提交 `claim` 和 `begin`。维护复用原生循环，只装载授权读／计算和 `submit_goal_result`，在独立 `sync/` 进度键保留原期限、模型调用和草案。后台通过 `calorie_diet_goal` 交回候选，写入者检查运行身份、委托版本、输入版本和业务事实摘要。
- `calculate_meal.days` 仅委托后台使用。一次最多七个不同日期，各自使用原计算器核对当天目标、摄入及份量。接受的日期在授权范围内，已有锁定或已食用餐次从原计划保留。`submit_goal_result` 引用本次确实完成的逐日草案，未完成范围与待决事项保留。
- 触发只排入授权范围的实际实例。终止、暂停或来源变化会拒绝迟到结果。通知另验目的地、最新有效版本、授权与来源，失败以同一投递 UUID 恢复，当前交付版本以确认送达为准。所有维护工作仍让位于现有恢复和归档；委托使用自己的 90 秒、4 次决策和 8 次工具预算。

## Store

生产 CosStore 和 FakeStore 对齐业务结果。BitableStore 服务来源维护、归档及迁移。

- `load_foods()/cache_food(food)` 保存每 100 克宏量、可选来源热量、`boohee_id/units/detail_loaded`。旧食物缺来源热量时用未舍入宏量公式。
- `intake_items(user_id,start_date="",end_date="",include_deleted=False)` 返回摄入快照，含 `record_id/date/message_id/meal_type/grams/nutrition/snapshot`。`intake_by_ids`／`intake_for_action` 定位固定目标，其他 Store 可通过明细读取。
- `write_intake(message,items,meal_type)` 批量写入，重试按固定动作键读回原行。`revise_intake(user_id,record_id,item=None)` 设置固定行的量、营养和快照，`None` 表示软删除。`today_summary()/summarize_history()` 汇总有效快照。
- `base_standard()/get_standard(user_id)` 按字段覆盖项目默认、原标准、长期设置、当日设置。
- `load_document(kind,user_id,scope="")/save_document(kind,user_id,scope,payload)` 以 kind、用户、scope 保存文档。`list_documents(kind)` 按类型读取，用于提醒规则等。`load_session()/save_session(user_id,session_id,session)` 保存近期清洁消息、`unfinished/refs/last_meal/proposal`。
- `begin_execution()/load_execution()/save_execution()/load_unfinished_executions()` 保存原消息、日期、原生交换、固定批次、`prepared`、结果和最终正文。别名、知识与 usage 接口继续提供，usage 失败不阻断业务。

## 入口及预设

- live 入口持久保存来源、归一身份、会话、原消息键、原文、`received_at/intake_date` 和序号，重投沿用原记录。飞书在持久收件且 SCF Event Invoke 受理后 ACK，承接失败返回 503。事件携 `ingress_revision`，worker 按确切收件版本核对身份和会话顺序。入口、消息和归档各平台并发 1。
- CLI／控制台／DSH live 通过 `LiveClient` 调用 `/internal/intake`、`/internal/result`、`/internal/presented`，沿用 `session_id/tool_events`。配置见[运维手册](../docs/operations.md#环境配置)。飞书来源才发送飞书回复。CLI 输出和 DSH 实际追加后用已保存 UUID 及结果摘要确认展示。旧展示记为 `presentation_rejected`。
- live 使用 COS 当前事实。shadow_fakewrite 使用 FakeStore。shadow_readonly 返回可见 COS 提交版本，对 COS、Bitable、会话、缓存、usage 和投递零写入，索引重建仅在内存进行。
- `model_transport` 的绝对截止覆盖连接、响应头、保活和完整响应体。薄荷两次尝试与退避共享 10 秒，SCF 派发单次 2 秒，LightRAG 初始化、检索及关键词／embedding 线程共享 85 秒，均受当前剩余预算限制。LiveClient 单请求最多 10 秒，受理前开始计算总等待，超时按原消息键核对。COS 使用 SDK 签名、MD5、禁止覆盖及 `retry=0`。
- 飞书 live 首批选按顺序第一条有效只读说明，直接账本批次跳过。首批无说明时，第一个符合条件的后续计算或知识批次可补一次。机会与批次同存，只有未执行的 `ready` 批次可发送，`attempted` 在 RPC 前保存。全消息最多一次说明尝试，墙钟最多 1 秒且受原截止限制。失败或未知时继续业务，恢复不重发说明。旧执行无持久 `ready` 机会时不补发。收到说明的 `message_id` 后用它编辑终稿，否则用独立最终 UUID 发送。新结果及摘要在发送前保存，说明本身不确认草案或提案。最终交付独立保存恢复次数。
- 维护先唤醒未完收件再归档。提醒沿原计划时刻及逐规则进度恢复。

## COS 提交与归档

- `CosStore(objects,prefix,offset_hours=8,readonly=False)` 采用 `message/` 范围，worker 执行前调用 `confirm_current()`。`Journal` 在 ingress、message、sync 分别保存连续编号、前驱和内容寻址引用，v1 每 128 版本保存索引检查点，v2 每次保存有界分页根，见下文候选协议。空确认点只写版本关系，同值保存不新增提交，普通保存只提交变化文档。COS 桶从未启用版本控制。
- `CosObjects.list(prefix,marker='',limit=None)` 按字典序返回严格在 marker 之后的键，最多返回 limit 项。LIST 只定位候选。暖视图从已确认水位增量追读，冷视图有界探测头和最近检查点，至多四路并发校验尾段后按序应用。下一编号禁止覆盖，冲突须精确追读相邻提交。已知版本不可见或提交结果未知时停止，确认后恢复。文档上传全部成功后发布唯一提交。
- `commit_action(action_id,callback)` 原子提交业务变化、固定餐项 ID、工具结果、执行进度和归档版本。新餐项 ID 按动作和首次项序号派生，导入保留原飞书 ID。重放读原结果。`committed_action` 只读，确认屏障未完成须报错。FakeStore 对齐重放和回滚语义，失败回滚未提交业务，已保存的 `flush_preparation` 准备事实可恢复。
- 补全或取消在 `prepared.pending_before` 保存待办快照，执行及恢复均核对未变化且未消费。待办变化随业务结果提交，只合并 `unfinished/refs` 及仍指向原餐的 `last_meal`。旧未提交准备缺 `pending_before` 时要求重新发起，其他固定动作按原准备恢复。旧任务不得覆盖较新会话。
- v1 文档按原散列身份存储，`document_index:<kind>` 保存键列表，`document_index:ready` 保存排序后的 `document:` 键集合 SHA256。摘要缺失、旧布尔标记或覆盖变化时重建，以兼容旧写入器回退后新增的文档。可写时索引与本次保存原子发布，普通正文更新不重写索引。LRU 上限 512 项和 8 MiB 编码数据，超大文档直接从 COS 读取。批来源 `source:foods` 可按内容哈希预热，实际食物版本由 `food:` 索引选择，单项更新不重写批快照。
- 仅摄入、食物及个人设置的业务变化写 `archive:<kind>:<id>` 并推进业务版本，`feishu_chat_id` 等运行元数据不推进版本。同步函数只写 `sync/`，合并尚未发送的同项变更，先持久保存 inflight 的绝对字段和 `client_token`，响应未知按原稳定 ID 核对。新 token 绑定 app/table/kind/id/revision，旧 inflight 沿用原 token 和字段。
- 归档以独立读回的精确字段及版本确认水位。数值用 Decimal 比较，兼容数字文本和单值 wrapper。较高 `agent_revision` 须为有限合法整数才可判定 superseded。人工修复 blocked 请求须冻结写入者，核对原 sync 版本、哈希和目标 ID，保留旧失败审计后再用固定新请求恢复。人工恢复样本不计健康归档时效。
- 生产消息热路径从 COS 读取业务和来源事实，薄荷详情先提交 COS 后归档，LightRAG 读取已发布索引。迁移入口为 `migration.export_cos_baseline`／`import_cos_baseline`，独立核对含软删除 ID、日合计和幂等任务。已交付旧收件转换为 completed `receipt_state`，重投不重发。归档确认不删除 COS 当前事实。

## 上下文、预算与恢复日志

- 动态状态每次重建。完整请求预算按最终 HTTP body 计量，供应商 usage 独立保存。默认值见 `config.yaml`，编码身份见 `agent/encoding/version.json`。固定 `deadline_at/legacy_budget/model_steps` 与累计次数随执行持久，`execution_budget.model_decision/model_decisions_after_this/tool_calls_remaining` 从该计数投影。
- `journal.messages` 保存原生恢复交换。DeepSeek 首次发出 HTTP 前，同一检查点持久保存决策计数、`model_steps[].request_body` 实际输入和尝试预留；响应及原请求供恢复使用。业务状态在 `session/prepared.session`，结果元数据在 `result`，重放用 `calls` 重建完整工具事实。`previous_session_message_id` 与会话 `last_message_id/last_execution_complete` 阻止旧任务回写。`offered_message_id` 绑定实际展示候选。
- `conversation.history(for_model=True)` 将历史轮次投影为含 `turn_id/reply_status/execution/dialogue/references` 的 SystemMessage 背景组，当前原话及本轮原生配对保持独立。`reply_status` 只依据确认的 dialogue-delivered 或 receipt_state 最终交付。`execution` 从原保存结果提取业务状态、原生调用和工具状态，明确无调用且无提交时标仅回复，缺执行依据标未知。仅为已展示草案保留对象引用，旧账本正文和数字不回填。已回复仅表示交付。分组不写回原来源。
- `conversation_context` 保存摘要身份、`fact_scope=dialogue`、覆盖游标和未覆盖轮次。 `dialogue_turn` 按固定用户消息 ID 保存清洁轮次，`agent_session.messages` 保留近期兼容视图。旧有效 dialogue 摘要及现有清洁轮次可作初次基准和后缀，无来源标记或错误占位不可作为基准。无 `fact_scope` 且无知识工具来源的旧正文不进入新输入。知识原文沿证据身份投影一份，规则见[知识契约](knowledge_schema.md)。
- 交付确认后将轮次标记 complete，并保存 `summary_request(user,chat)` 及 `summary_active(system)`。sync Journal 保存计算进度、连续前缀快照和候选。候选含 `task_id/user_id/chat_id/base_id/turn_ids/source_hash`。`calorie_summary` 控制事件由消息写入者验证基准及完整前缀，再按应用动作键原子去重。事件不经业务受理，不推进用户轮次、业务序号、展示状态或主预算。调度按[后台摘要规则](../CONVERSATION_EXPERIENCE_SPEC.md#5-后台话题摘要)。

## 阶段 C 候选接口（未部署）

实现及验收状态见 [阶段 C 交付](../reports/STAGE_C_DELIVERY.md)。以下是本分支代码协议；不能据此认定正式 COS 已切代。

- 主循环新增 `query_sources`（独立通用 Search／Fetch、已读引用和当前用户文本）、`calculate_values`（有来源的 Decimal 四则运算／日期天数差）、`manage_task`、`query_task`、`delegate_task`。工具注册、schema 和预设共用原注册表。Worker／Router 计算上下文另提供 `submit_task_result`，不开放递归委派或业务写工具。
- 通用委托与饮食委托分别最多一个。`manage_task` 的授权原话须出现在本条消息，修改／控制绑定当前 revision；时间、反馈触发、单次停止、指定 Worker 停止、暂停、未来触发开关及通知开关分开。`query_task.status` 只查询；`read_artifact` 另须当前查看原话，实际交付才确认 delivered。成果采用 `items/evidence/unresolved/sources`，不使用餐次 schema。
- `delegate_task.jobs[].urls` 携带用户已提供的未取文公开链接，`source_refs` 绑定工具已核对的原文。父周期取到的正文按独立引用保存，跨入口或额度耗尽可保留来源。失败周期不覆盖已有成果，通知绑定当前委托版本和实际投递确认。
- 父周期绝对截止从受理起 180 秒，父与两个 Worker 总上限为 12 决策／24 工具／9 网页 HTTP。当前分配是父与每子各预留 4／8／3，最多两个子任务；预留与固定身份在消息写入者的原动作提交中保存。未知退出不归还预留，重启不重新领额度。配置初值见 `task_cycle_*`；单次计算仍受原 90 秒和平台剩余时间限制。通用与食品网页共享父侧 HTTP 余量。前台按原消息键累计，后续状态／控制消息不重置本周期父侧用量；Router 从同一剩余额度续作。
- `task/task_owner/task_active/worker_active/task_source/task_artifact/task_input/worker_attempt/task_event/task_router_execution/task_run_audit` 均由原消息写入者写入。正文来源和成果独立保存；状态中仅保存引用。Worker 使用同一模型和 `create_agent`，从固定输入快照获取相关来源及已授权记忆，快照 SHA256 绑定本次尝试。
- `app.compute_handler` 是受限计算入口，部署要求并发 2、角色没有 COS 权限。消息写入者先登记固定 input／candidate 键，再签发原截止内的 GET／禁止覆盖 PUT。使用 SCF 临时凭据签发时，将 COS 会话令牌作为签名查询参数。签名不进模型或 trace。消息写入者在派发前持久记录 15 秒认领窗口，同一窗口内不重发；未认领可由后续维护重发同一尝试。认领由消息入口串行处理；后续 `/internal/worker-control` 是只读查询，绑定用户、父子身份、运行实例和每次尝试的独立能力令牌，令牌只经 `X-Calorie-Worker-Token` 请求头传输；无法读取控制即停止。HTTP 签名错误只记录错误类别，不把带签名 URL 写到公开日志。
- 候选按固定键读取并检查 128 KB 上限、身份、委托／撤回版本、截止、取消、实际引文、数值和运行时用量。发布与父状态更新走原 `commit_action`。同摘要重投返回 duplicate，异摘要拒绝；迟到／撤回候选仅审计。丢失回报时按已登记键发现，不扫描候选前缀。委托记忆逐项绑定当前可见内容散列，普通版本变化和撤回均使旧候选失效；已保存成果读取时也按当前来源投影。`worker_active` 独立于父活动索引，父结束后仍保留已登记候选接收，直至消费或原签名过期。子任务失败不丢弃另一项成果；Router 使用父侧剩余额度核对覆盖和未决。
- 后台 Router 和子 Worker 共用受限计算入口及两个计算槽。Router 从原父周期剩余额度取得一次固定尝试，读已核实来源及子成果，最后一次决策用于提交。消息写入者保存尝试、认领和发布。待派发窗口使用独立派发身份与截止，过期认领不改变状态；已认领且用量未知不以同额度重算。固定候选已存在可由消息写入者直接核对，普通消息和回调只推进所属用户。每分钟提醒触发同时扫描活动任务，独立于五分钟归档。

### COS v2 与保留接口

- Journal 同时读取 v1 和 v2；已有 v1 在显式切换前仍沿原协议，首次升级使用同一三域停写／唯一决议。新空数据域写 v2。v2 每次提交保存有界 radix 索引根，叶页最多 32 项、目录扇出最多 17（含终止符）；不可变页按内容复用，不再每 128 次复制全部键。冷读直接取得根，暖读只定位新增提交；v1 的 128 检查点算法单独保留。写结果未知仍关闭该实例，按固定键及禁止覆盖屏障恢复。
- 同次提交的不可变文档和索引页共用有界传输批次；全部上传成功后才创建唯一可见的提交。传输失败不更新当前索引根。
- v2 `catalog:<kind>:<document-key>` 和用户目录在新建文档时同动作发布；不再用全键集合哈希核对覆盖。普通正文更新不改目录。记忆元数据的可变映射、活动目录、会话轮次／摘要覆盖采用独立点键或 32 项页；正文通过原引用读取。旧目录由维护显式转换；只读 v1 重建不落盘。v2 不能回退到不维护该格式的写入器。
- 收件顺序为每会话 32 项页与 position 游标；历史重投仍按 receipt 点读。sync 自己维护增量收件发现和活动恢复索引，未完成／人工恢复独立保留，不跨域共同修改队列。分钟事件直接检查提醒，只为实际投递保存固定防重任务。今日／有边界日期请求直接定位日索引。确认 COS 头后，原截止已过的人工恢复任务独立保留，推进顺序游标以允许同会话新消息；未知写入仍先确认屏障。
- 实际提醒使用原逐规则 `reminder_receipt` 和 UUID，`reminder_delivery_active` 只收录准备过的未确认投递。每次 RPC 前持久增加累计尝试（自动最多三次），重启不重领；sync 只唤醒消息写入者。当前关闭／规则变化／已记餐抑制旧投递。维护消息调用恢复后立即按原计划时刻补查至多 15 分钟，超过跳过；已准备失败独立恢复。旧记录缺累计尝试时不自动补额度。所有新控制／维护／计算入口在只读预设先退出。
- `archive_scan` 的代与水位和 `archive_pending` 发现项共同提交；只扫描新增消息提交，已确认项退出活动发现。原 inflight 请求、token、版本和行映射保持不变。切代转换水位，并携带准备后新增的待归档项。
- 执行快照的大型请求／调用／结果拆成不可变 `$artifact` 引用，`load_execution`、`committed_action` 和结果接口仍返回原业务形状。内部物理形状不成为消费者接口。缓存仍统一受 512 项／8 MiB 上限约束。
- `retention.preflight` 只读分类，返回保留／瘦身身份、逻辑字节、原因和未分类项。终态工作和交付均结束 30 天后，在新基线中用精简凭据替换过程；未知、待展示、needs_attention、活动依赖、归档 inflight 不按年龄清理。旧结束时间不明时保存首次升级锚点，不读文件年龄。终态重投返回原回执与 `audit_status=unavailable`，过期 tool_events／trace 不重算；精简动作凭据仍阻止 callback 再执行。
- `GenerationControl` 的控制记录独立于三域代前缀。三个角色分别用禁止覆盖屏障确认最终版本；消息调用内自持 40 秒取消／60 秒恢复 watchdog，不依赖后续请求。唯一 decision 解决 enable／cancel 和未知响应；旧 ingress 事件映射到新根水位。旧实例每次提交前查代，陈旧 LIST 不能复用旧序号。
- `Maintenance.prepare/finish/collection_plan/collect` 复用同一切换协议。准备期独立读回正文；暂停窗只追并核对增量。删除必须先新进程核对、COS 实际拒绝三个旧角色写入，等待旧候选签名失效并逐键对账。不可变删除清单逐项留进度，响应丢失和中断重做同一清单。控制记录不删除。默认自动维护关闭，隔离验证后才可设置操作门；正式切代和回收不在本任务授权内。
