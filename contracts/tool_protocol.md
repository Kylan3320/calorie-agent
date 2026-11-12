# Store 与原生工具契约

回复、整轮完成和恢复规则见[交流体验规格第 2 节](../CONVERSATION_EXPERIENCE_SPEC.md#2-回复完成与恢复)。字段的观测口径见 [Trace 契约](trace_schema.md)。

本次[控制权修订与持续委托目标](../PERSONAL_ASSISTANT_SPEC.md#1-目标范围与保留决定)尚需实现。以下当前接口中的固定查证步骤、默认排序、收尾白名单等是现实现描述，不是新目标的永久规则；实现者按目标调整后再同步实际接口，不能把文档目标当作已有能力。

本文以下记录当前实现接口。[省心饮食与跨会话记忆规格](../PERSONAL_ASSISTANT_SPEC.md) 定义尚未实现的增量：query_memory 分层读取、份量证据、条件／经验生命周期、有界后台候选发布及跨会话源草案。下游随实现更新本契约；当前八工具、source_ref 会话绑定、本会话草案及摘要接口不能据目标文档推定已支持新能力。

## 工具注册与执行

- `agent/tools/registry.py` 定义名称、说明、Pydantic 参数、`metadata.risk` 和执行函数。每次 `create_agent` 请求按预设选用注册对象。八个工具为 `record_meal`、`revise_intake`、`query_today`、`query_history`、`query_food_nutrition`、`query_knowledge`、`calculate_meal`、`update_user_settings`。三个修改工具的风险依次为 `write`、`destructive`、`write`。其余为 `read_only`，在可写预设中可以缓存食物资料，不能创建摄入或设置。
- 原生工具返回 `{tool,call_id,action_id,status,data,error?}`。整批先保存原生调用和固定序号，再执行。完整结果存于 `calls.result`。本轮 `ToolMessage` 按 `model_view` 投影并保留配对 `call_id`。参数错误以结构化 `error` 返回，允许在原预算内纠正。旧框架 `ToolMessage.status=error` 的纯文本按错误身份包装，保留原文审计。网络或存储异常中止后续排队动作，原固定动作可恢复。
- 每条消息累计最多 4 次主决策和 8 次工具执行。超过上限的固定调用按持久 `index` 保存 `limit` 和终结原因，跳过业务执行。预算确定超限时保存终态回执和未执行原因，交付后推进游标。重投返回原回执。
- 八个工具均可提供 `action_note`。四个账本工具和 `calculate_meal` 另接受 `finish_after_tools`，缺省及旧调用无声明时为 false。控制字段随批次持久保存，剥离后交给原生业务 handler，保持业务参数、`action_id` 和 `prepared` 身份。
- 直接回执使用完整 `calls.result`，返回 `reply_source=tool_facts` 和空 `model_reply`。纯菜单改稿限本会话已展示 `draft_id` 的单次计算，须与本消息待展示草案一致，结果完整、`status=ok` 且无 `unfinished` 或 `question`。此前成功的只读结果可以作为依据，此前写入及非成功结果仍参与完成判定。首次规划、缺来源或尚需解释时继续原生循环。

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

## 菜单、提案与提醒

- `calculate_meal.scope` 为 `daily`（长期目标）、`full_day`（当日目标）或 `remaining`（当日目标减一次有效摄入）。旧调用省略时按 `remaining`，改稿省略时沿草案范围。餐项含 `item_id/group/servings/portion_basis/assumption/source_ref`，份数只缩放一次。结果含 `targets/consumed/consumed_scope/total/budget_difference/complete/within_tolerance`，未计入项只计已有小计。
- `fixed` 标注固定组成，`max_grams` 为每份上限。结果保存 `fixed_subtotal/fixed_complete/supplement_gap`，缺口按目标减已计摄入和固定小计计算。初稿与 `adjustment` 的 `replacements/item_id`、`additions` 在同次调用比较，复用固定来源和范围。备选只能替换非固定 `suggested` 项。改动 `user_given` 或 `assumed_takeout` 时整份备选被拒，`selection.rejected_adjustment` 留原因并继续核算原稿。只给新数量、未声明依据的改稿按用户固定量处理。
- 份量校正、热量容差和宏量选版见[个人规格第 3.6 节](../PERSONAL_CALORIE_SPEC.md#36-饮食计划与草案)。`portion_preference=smaller_portions` 在宏量并列时比较最大每份克重及备餐项数。候选使用来源单位或 10 克档位，建议量距来源整数单位不超过 1 克时按来源精确重量核算。无日常单位且上限不足 10 克时使用正整克。已有版本达容差时不搜索，改稿只调整明确允许的替换项，复用来源且不加食材、外部查询或模型步骤。
- `selection` 保留各版本实际来源、份量、合计、完整性、容差与偏差以及 `selected/reason/calibration`。模型只取比较指标，动态草案视图省略重复明细及未选审计。公开数量由选中草案渲染，原 `assumption` 保留供审计，公开其定性部分。有组成假设的固定餐标估算。模型补充定性理由使用 `<plan_note>...</plan_note>`，公开正文去掉标签，定量内容由草案统一展示。
- `meal_draft_candidate` 保存选中稿的参数、计算、限制及 `draft_id/message_id`，`result.presented_candidates` 只保存实际发布的候选身份。缺来源时用实际 `unfinished.candidates` 形成待选项。终态异常可以交付已核算草案并保留原错误状态。交付后 `mark_reply_presented` 按消息和候选 ID 提升 `meal_draft/food_offer`，旧展示不得覆盖新稿。改稿按 `draft_id+replacements.item_id` 替换指定项，保留其余限制。未找到替换项时保留原稿及主答复。计划和采用草案均不写摄入。
- 提案保存 `proposal_id`、固定参数、目标摘要和 `response_id`。交付后标记 `presented`，紧邻确认执行同一组参数。修改提案以目标身份、日期、克重、营养、快照、状态和版本计算 `target_version`。相对改量冻结绝对克重，`target_ratio` 保留换算依据。确认须匹配原版本，`prepared` 保存绝对结果。旧提案缺目标版本时需重新发起，已提交动作仍重放原结果。
- 提醒缺条件时保存原规则为关闭，`reminder_pending` 保存 `rule_id`、缺项、启用意图和快照。补缺时重查原规则。取消、禁用或快照冲突会使旧意图失效。

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

- `CosStore(objects,prefix,offset_hours=8,readonly=False)` 采用 `message/` 范围，worker 执行前调用 `confirm_current()`。`Journal` 在 ingress、message、sync 分别保存连续编号、前驱和内容寻址引用，每 128 版本保存索引检查点。空确认点只写版本关系，同值保存不新增提交，普通保存只提交变化文档。COS 桶从未启用版本控制。
- `CosObjects.list(prefix,marker='',limit=None)` 按字典序返回严格在 marker 之后的键，最多返回 limit 项。LIST 只定位候选。暖视图从已确认水位增量追读，冷视图有界探测头和最近检查点，至多四路并发校验尾段后按序应用。下一编号禁止覆盖，冲突须精确追读相邻提交。已知版本不可见或提交结果未知时停止，确认后恢复。文档上传全部成功后发布唯一提交。
- `commit_action(action_id,callback)` 原子提交业务变化、固定餐项 ID、工具结果、执行进度和归档版本。新餐项 ID 按动作和首次项序号派生，导入保留原飞书 ID。重放读原结果。`committed_action` 只读，确认屏障未完成须报错。FakeStore 对齐重放和回滚语义，失败回滚未提交业务，已保存的 `flush_preparation` 准备事实可恢复。
- 补全或取消在 `prepared.pending_before` 保存待办快照，执行及恢复均核对未变化且未消费。待办变化随业务结果提交，只合并 `unfinished/refs` 及仍指向原餐的 `last_meal`。旧未提交准备缺 `pending_before` 时要求重新发起，其他固定动作按原准备恢复。旧任务不得覆盖较新会话。
- 文档按原散列身份存储，`document_index:<kind>` 保存键列表，`document_index:ready` 保存排序后的 `document:` 键集合 SHA256。摘要缺失、旧布尔标记或覆盖变化时重建，以兼容旧写入器回退后新增的文档。可写时索引与本次保存原子发布，普通正文更新不重写索引。LRU 上限 512 项和 8 MiB 编码数据，超大文档直接从 COS 读取。批来源 `source:foods` 可按内容哈希预热，实际食物版本由 `food:` 索引选择，单项更新不重写批快照。
- 仅摄入、食物及个人设置的业务变化写 `archive:<kind>:<id>` 并推进业务版本，`feishu_chat_id` 等运行元数据不推进版本。同步函数只写 `sync/`，合并尚未发送的同项变更，先持久保存 inflight 的绝对字段和 `client_token`，响应未知按原稳定 ID 核对。新 token 绑定 app/table/kind/id/revision，旧 inflight 沿用原 token 和字段。
- 归档以独立读回的精确字段及版本确认水位。数值用 Decimal 比较，兼容数字文本和单值 wrapper。较高 `agent_revision` 须为有限合法整数才可判定 superseded。人工修复 blocked 请求须冻结写入者，核对原 sync 版本、哈希和目标 ID，保留旧失败审计后再用固定新请求恢复。人工恢复样本不计健康归档时效。
- 生产消息热路径从 COS 读取业务和来源事实，薄荷详情先提交 COS 后归档，LightRAG 读取已发布索引。迁移入口为 `migration.export_cos_baseline`／`import_cos_baseline`，独立核对含软删除 ID、日合计和幂等任务。已交付旧收件转换为 completed `receipt_state`，重投不重发。归档确认不删除 COS 当前事实。

## 上下文、预算与恢复日志

- 动态状态每次重建。完整请求预算按最终 HTTP body 计量，供应商 usage 独立保存。默认值见 `config.yaml`，编码身份见 `agent/encoding/version.json`。固定 `deadline_at/legacy_budget/model_steps` 与累计次数随执行持久，`execution_budget.model_decision/model_decisions_after_this/tool_calls_remaining` 从该计数投影。
- `journal.messages` 保存原生恢复交换，`model_steps[].request_body` 保存实际 HTTP 输入，响应及原请求供恢复使用。业务状态在 `session/prepared.session`，结果元数据在 `result`，重放用 `calls` 重建完整工具事实。`previous_session_message_id` 与会话 `last_message_id/last_execution_complete` 阻止旧任务回写。`offered_message_id` 绑定实际展示候选。
- `conversation.history(for_model=True)` 将历史轮次投影为含 `turn_id/reply_status/execution/dialogue/references` 的 SystemMessage 背景组，当前原话及本轮原生配对保持独立。`reply_status` 只依据确认的 dialogue-delivered 或 receipt_state 最终交付。`execution` 从原保存结果提取业务状态、原生调用和工具状态，明确无调用且无提交时标仅回复，缺执行依据标未知。仅为已展示草案保留对象引用，旧账本正文和数字不回填。已回复仅表示交付。分组不写回原来源。
- `conversation_context` 保存摘要身份、`fact_scope=dialogue`、覆盖游标和未覆盖轮次。 `dialogue_turn` 按固定用户消息 ID 保存清洁轮次，`agent_session.messages` 保留近期兼容视图。旧有效 dialogue 摘要及现有清洁轮次可作初次基准和后缀，无来源标记或错误占位不可作为基准。无 `fact_scope` 且无知识工具来源的旧正文不进入新输入。知识原文沿证据身份投影一份，规则见[知识契约](knowledge_schema.md)。
- 交付确认后将轮次标记 complete，并保存 `summary_request(user,chat)` 及 `summary_active(system)`。sync Journal 保存计算进度、连续前缀快照和候选。候选含 `task_id/user_id/chat_id/base_id/turn_ids/source_hash`。`calorie_summary` 控制事件由消息写入者验证基准及完整前缀，再按应用动作键原子去重。事件不经业务受理，不推进用户轮次、业务序号、展示状态或主预算。调度按[后台摘要规则](../CONVERSATION_EXPERIENCE_SPEC.md#5-后台话题摘要)。
