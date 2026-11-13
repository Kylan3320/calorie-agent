# Trace v2

运行时每次输出一行 JSON 到 stdout，并追加到 `TRACE_PATH`（默认 `/tmp/traces.jsonl`）。`TRACE_ENABLED=false` 仅关闭文件写入。凭据及凭据哈希不入 trace。

## 运行结果与原生调用

- 基础字段包括 `schema_version=calorie_agent_trace.v2`、`created_at/trace_id/message_id/user_id/chat_id/text/raw_text`、`preset/model/retriever/status/errors/latency_ms/storage_revision`。`storage_revision` 是可见提交版本，`latency_ms` 保留运行时计时口径。
- `native_calls` 按模型返回顺序保存 `call_id/tool/args/index/action_id/batch_id/finish_after_tools/action_note`。两项控制字段与业务 `args` 分离。`completion` 保存末批声明和 `adopted`。未执行的超限项保留 `terminated=tool_call_limit` 和结构化 `limit` 结果，计入 `journal.termination.unexecuted_action_ids`，业务执行数不增加。旧框架错误原文留审计，清洁历史仅带错误身份和终止说明。
- `tool_results` 保存完整工具事实。`query_food_nutrition.data.queries` 逐项保存 `query/status/data`，外部 RPC 次数和耗时单独计量。`query_knowledge.data.retriever/evidence` 标识实际检索器和原文。条目的 `evidence_id/legacy_source_id` 及 `journal.visible_evidence` 的含义见[知识契约](knowledge_schema.md)。
- `model_reply` 是原始模型终稿，`reply` 是运行时最终回执，实际投递见[收件与投递](#收件与投递)。`response_adjustments` 记录引用替换、确认提示、`canonical_presentation` 和 `quantitative_plan_note_removed` 等调整。`reply_source` 为 `agent/agent_adjusted/tool_facts/fallback`。事实回执的 `model_reply` 为空，`guard_result.checked_scope=committed_tool_facts_and_batch_completion` 且 `fallback_used=false`。
- `terminal/termination_reason/completion_status/business_status` 分别保存终结标记、原因、完成状态和业务提交状态。完成状态区分 `completed/awaiting_user/needs_reconciliation/terminated`。已解决的营养待选项不使已展示完整草案继续标为待用户，其他未完事项仍有效。预算确定终结时 `completion_status=terminated`，交付后 `work_status=terminated`。

## 模型请求、预算与摘要

- `model_requests` 保存主决策的 `step_id/projection_ms/history_message_count`、当前消息和注册工具。相同 `tools_digest` 后续仅存工具名。工具消息审计用 `result_ref`（call_id）和 `content_sha256` 关联持久工具事实。
- `journal.messages` 保存原生恢复交换，`session.messages` 为近期清洁视图。实际请求和已接收响应保留在恢复日志的 `model_steps[].request_body/response`。公共 trace 保留步骤字段和尝试审计。历史 SystemMessage 背景组的字段见[工具契约](tool_protocol.md#上下文预算与恢复日志)，预算归入 `parts.history`。定位信息标记 `historical`，助手带 `fact_scope=business/knowledge/dialogue`，摘要另带 `lc_source=summarization`。当前用户消息 ID 为 `calorie-user:{message_key}`。
- `model_requests[].budget` 与 `model_http` 记录 `purpose/encoding/wire_encoding/input_tokens/input_target/output_reserve/protocol_margin/hard_input_limit/removed_turns/over_target_reason` 和分项 `parts(system/state/summary/history/current/reasoning/calls/tools/protocol)`。HTTP 返回后记录供应商 `usage/finish_reason`，本地 wire 计量独立。超限拒绝记录 `budget_exceeded`。请求状态的 `execution_budget` 由持久主决策与固定调用计数构造，字段见[工具契约](tool_protocol.md#上下文预算与恢复日志)。
- `deadline_at` 是消息固定截止 epoch，`legacy_budget` 标识缺原接收时间的旧收件一次性接管。`model_calls` 只计语义步骤。`model_steps` 含 `id/started_at/deadline_at/status/attempts`，逐次尝试保存 `reserved_at/started_at/ended_at/state/status_code/provider_request_id/response_headers_ms/first_nonempty_response_byte_ms/completed_ms/usage/finish_reason`、脱敏错误和 `retry_delay_seconds/retry_not_before`。空白保活不计首个非空字节。
- `summary_task` 记录 `task_id/status/attempts/duration_ms/measurement`，`summary_application` 记录 `task_id/status`。候选身份见[工具契约](tool_protocol.md#上下文预算与恢复日志)。后台摘要费用独立于主模型调用，`dialogue_turn_id` 关联交付与清洁轮次。

## 收件与投递

- `scf_handoff` 记录入口到消息函数的 Invoke。事件引用 `receipt_id/ingress_revision` 及原消息、用户和会话身份。飞书 `latency_webhook` 含 ACK 前事件，`latency_worker` 记录消息函数自身事件，`latency_delivery` 另带 `build_id/sequence/source` 和投递结果。按 `receipt_id` 或 `message_id` 关联两端记录。累计 trace 不进入派发参数。归档网络等待在独立函数内。
- `receipt_state` 保存 `receipt_id/sequence/attempts/queue_wait_ms/budget_started_at/deadline_at/legacy_budget/result/work_status`。其 `delivery` 保存 `uuid/result_digest/presented/sent_at/send_result/message_id/cos_revision/t_service_ms/send_duration_ms/slow_request`，进度升级另有 `progress_reply/update_result/recovery_attempts`。新结果和正文摘要在发送前同次保存。CLI／DSH 回报展示及结果摘要，旧事件记为 `presentation_rejected`。
- `t_service_ms` 从原 `received_at` 到发送 API 成功，旧收件缺该时间则为空。大于 10 秒只标慢。存在有效 `create_time` 时另列 `t_user_ms`。重投保留原执行的 `latency_events`，本次发送看入口／投递事件，恢复和重投不产生新的正常性能样本。
- `worker_started_at` 在 Pipeline.worker 内部 COS 读取前捕获。首次进入 worker 与原收件的差值包含受理端 COS、派发及可能的实例构造。旧证据的 `function_queue_ms` 沿用这一口径。`queue_wait_ms`、原收件至 worker 和未直接观测的纯平台排队应分开，缺冷启动或排队观测时标未知。

## 行动说明与草案审计

- `result/journal.action_note_opportunities` 用 `first/supplement` 标记首批或后续任一未执行批次的 `batch_id/status/call_id/text`。`action_notice` 与 `receipt_state.action_notice` 保存 `batch_id/call_id/opportunity/text/status/attempted_at/confirmed_at/message_id/uuid` 和脱敏错误类别。每消息最多尝试一次。说明确认与 `delivery.mode=send/action_note_edit`、终稿展示分别记录。
- `journal.model_steps[].review_sources` 记录私有复盘标记的归一引用和理由，实际交付后才可入队。公开正文和 `result.model_steps` 不包含该控制区。网页请求按 `calls[].prepared.web_requests` 的预留计数，重投不重复请求。
- 按对应批次核对 `model_steps.started_at`、`model_http` 响应结束、`execution_save.phase=response_batch_saved` 结束、`action_note_send` 确认、首个 `tool_commit` 及 `delivery.sent_at`。批次落盘包含原始响应和固定调用，补发按自身批次计时。终稿编辑计入 `send_reply` 和 `T_service`。
- `result.presented_candidates` 记录实际附入终稿的 `food_offer_candidate/meal_draft_candidate`，交付确认后提升。终止回执可带已核算草案，仍保留 `error/terminal`。确认按原消息及精确候选 ID。`source_ref/offer_id/draft_id/history_scope/reminder_pending` 用于接续。
- `result.goal_delivery_ref` 仅在实际回复包含最新委托计划时记录 `{id,version}`，收到对应投递确认后才推进委托已交付版本。目标后台运行记录由 `diet_goal_run` 和 `diet-goal-compute:<run_id>` 关联，不并入前台消息的 `model_calls`。
- `calculate_meal.data` 的 `fixed_subtotal/fixed_complete/supplement_gap/selection` 保存固定餐和选版。`selection.candidates` 保留各版来源、份量和营养，`selected/reason` 对应最终草案。模型投影仅保留比较指标，完整事实含全部审计和原 `assumption`。
- `selection.candidates` 可包含 `variant=portion_adjustment`。`selection.calibration` 保存 `status/eligible_item_ids/max_changed_items/gram_step/search_scope/limits`，执行校正时另有 `starting_variant/searched_items/changes/original_budget_difference/selected_budget_difference`。`changes` 保存 `item_id/original_grams/selected_grams/original_assumption/selected_assumption`，有来源单位时另有 `quantity/unit`。未采用、已有版本达标、缺容差分别标 `kept_original/already_within_tolerance/no_tolerance`。

## COS 读取视图与归档

- `latency_events` 使用 `kind/id/parent/instance_id/message_id/started_epoch_ms/started_monotonic/duration_ms/outcome` 及各阶段字段。按实际消息身份筛选 Runtime 和投递事件，入口聚合保留分消息事件。旧任务按各自消息 ID 计时。嵌套耗时取有效区间，避免重复相加。
- `model_http` 区分 main／summary 并保留供应商用量，标准 `AIMessage.usage_metadata/model_provider` 随适配器。`feishu_http` 记方法、资源、尝试、请求及响应字节。RAG 抽词和 embedding 分开计时。
- `cos_pipeline_view` 记录 `function/bucket/region/prefix/rebuilt/reason/previous_view_id/previous_revision/authorization_scope`。原因是 `first_use/confirmed_view/storage_scope_changed/transport_credentials_changed`。仅传输凭据变化时授权范围为 `unknown`。
- `cos_refresh` 记录 `view_id/prefix/readonly/start_revision/minimum_revision/confirm/end_revision/reason`，原因是 `first_use/confirmed_view/uncertain_commit`。`cos_catchup` 用同一视图 ID 记录 `start_revision/requested_revision/checkpoint_revision/read_from_revision/read_to_revision/end_revision`。空读段表示未追读旧提交。非空检查点表示从该检查点及尾段重建。失败看 `outcome=error` 与实际水位。视图 ID 仅关联进程内读视图。
- COS HTTP 等待只取 `cos_get/cos_create/cos_list/cos_versioning` 实际区间并集。这些跨度记录键及适用的字节数。读视图跨度另含本地核对和嵌套 RPC。首轮前区间截断在原 `model_steps.started_at`。
- Journal 关联收件 `ingress_revision`、消息提交版本及独立 `sync_revision`，业务动作的 `action_id` 固定。归档确认事件含稳定业务键、业务版本和飞书 `record_id`。失败事件含 `ack_revision/inflight_revision`。旧 inflight 保留原 token。人工修复独立保留旧请求、版本、哈希、服务端错误码、目的地和新请求哈希。读回字段和整数版本确认水位，规则见[归档契约](tool_protocol.md#cos-提交与归档)。

## 阶段 C 候选补充

- v2 `cos_catchup.format=2` 的数据根随选定提交读取，v2 的 `checkpoint_revision/read_from_revision/read_to_revision` 为所选根提交；v1 仍表示原检查点和尾段。判断格式以提交的 `schema=calorie.cos.v1/v2` 为准，不能拿 v1 的 128 间隔推导 v2 成本。`cos_get.bytes`、`cos_list.marker/keys`、`cos_create.bytes` 和 `cos_delete` 计入实际传输；增长验收额外保留完整请求／字节／枚举键统计，包括索引页和冷根。
- 通用任务通过 task／cycle／worker／attempt 和委托 revision 关联。运行时 `usage.decisions/tools/web` 来自计数器，源引用独立保存；候选摘要与固定键用于幂等。能力令牌、预签名 URL、传输凭据及其哈希不进 trace 或模型上下文。
- 终态瘦身返回 `audit_status=unavailable`、`audit_reason=retention_expired`，`tool_results/native_calls/model_requests/tool_events` 为空；这表示过程已到保留期，不表示原动作未发生。原终态、最终正文、交付 UUID 及业务映射保留。CLI／DSH／控制台沿结果字段显示，不制造旧工具卡片或重新运行。
- 代切换／删除证据记录源／目标前缀、三域冻结版本、唯一决议、暂停起止、清单摘要及恢复进度；真实 COS 403 与注入传输的拒绝分开。实现候选和外部验收缺口见 [阶段 C 交付](../reports/STAGE_C_DELIVERY.md)。

`worker_compute` 只记录 task／worker／attempt 身份、状态和耗时；用量保存在运行时生成的候选中。计算 trace 不打印输入、能力令牌、签名或完整模型交换。
