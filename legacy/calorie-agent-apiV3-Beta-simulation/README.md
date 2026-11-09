# Calorie Agent Tencent SCF Package

Tencent Cloud Function package for the Feishu calorie tracking agent.

The function receives Feishu bot events, quickly acknowledges each text message
with `status=accepted`, then continues processing inside the same Tencent SCF
invocation. Message processing calls DeepSeek for an action plan, executes only
backend whitelisted tools, and replies to the user in Feishu with facts from
tool execution. Cloud logs include `post_ack_processing_started`, the final
message diagnostics, and `post_ack_processing_completed` for troubleshooting.

## Endpoints

- `GET /health`
- `POST /webhook/feishu/events`
- `POST /tasks/daily-cleanup?token=<ROLLOVER_TASK_TOKEN>`
- `POST /tasks/reminders?token=<ROLLOVER_TASK_TOKEN>`

## Tencent Cloud Settings

- Runtime: Python 3.10
- Function type: HTTP function / Web function
- Listen port: `9000`
- Bootstrap file: `scf_bootstrap`

Upload package:

```text
D:\calorie-agent\backend\dist\calorie-agent-api-tencent-scf.zip
```

Build the package from `D:\calorie-agent\backend\tencent_scf` with:

```powershell
& 'D:\calorie-agent\backend\.venv\Scripts\python.exe' 'D:\calorie-agent\backend\scripts\build_scf_zip.py'
```

Do not use Windows right-click compression or PowerShell `Compress-Archive`.
The zip root must directly contain `app.py`, `scf_bootstrap`,
`requirements.txt`, `README.md`, and `calorie_agent/`. Zip entry paths must use
Linux-style `/`, for example `calorie_agent/agent/runtime.py`.

## Package Layout

`app.py` is the thin SCF entrypoint. Runtime code lives in
`calorie_agent/`. The current stable message processor is kept in
`calorie_agent/legacy_app.py`; AgentRuntime modules live under
`calorie_agent/agent/`. With `AGENT_RUNTIME_ENABLED=true`, normal Feishu
messages go through AgentRuntime. The old intent parser is used only if
AgentRuntime raises and `AGENT_FALLBACK_TO_LEGACY=true`.

## Feishu Setup

1. Create or use a Feishu app with bot ability enabled.
2. Configure event subscription URL:

```text
https://<your-cloud-domain>/webhook/feishu/events
```

3. Subscribe to text message events.
4. Grant the app access to the Bitable document.
5. Grant permissions needed for Bitable record read/write and bot message send.
6. Keep event encryption disabled unless encryption support is added later.

## Environment Variables

Required Tencent SCF variables:

```env
PORT=9000
FEISHU_APP_ID=
FEISHU_APP_SECRET=
FEISHU_VERIFICATION_TOKEN=
DEEPSEEK_API_KEY=
ROLLOVER_TASK_TOKEN=
BITABLE_APP_TOKEN=
BITABLE_FOOD_TABLE_ID=
BITABLE_INTAKE_TABLE_ID=
BITABLE_PENDING_TABLE_ID=
```

Optional table IDs:

```env
BITABLE_STANDARD_TABLE_ID=
BITABLE_AGENT_TRACE_TABLE_ID=
BITABLE_REPORT_TABLE_ID=
BITABLE_MEMORY_TABLE_ID=
BITABLE_COMBO_TABLE_ID=
BITABLE_PREFERENCE_TABLE_ID=
BITABLE_REMINDER_RULE_TABLE_ID=
BITABLE_REMINDER_EVENT_TABLE_ID=
BITABLE_ANOMALY_TABLE_ID=
```

Field mappings, feature toggles, thresholds, default model/base URLs, time
zones, and TTLs have code defaults in `calorie_agent/legacy_app.py`
`DEFAULT_SETTINGS`. Keep them out of Tencent SCF environment variables unless
your Bitable columns or runtime behavior intentionally differ from defaults.

Optional override examples are documented in:

```text
D:\calorie-agent\backend\docs\bitable-schema.md
```

## Bitable Tables

The current version uses four required tables and optional Agent tables:

| Table | Purpose |
| --- | --- |
| Food table | Per-100g food nutrition database |
| Daily intake table | User intake records and soft deletes |
| Daily standard table | Per-user macro targets |
| Pending task table | Multi-turn missing-info completion |
| Agent trace table | Optional runtime traces |
| Report cache table | Optional daily/weekly/monthly report cache |
| User memory table | Optional aliases and default grams |
| User combo table | Optional saved food combos |
| User preference table | Optional low-risk user preferences |
| Reminder rule table | Optional proactive reminder settings |
| Reminder event table | Optional reminder audit and frequency control |
| Anomaly event table | Optional anomaly fact persistence |

Full schema:

```text
D:\calorie-agent\backend\docs\bitable-schema.md
```

## Local Verification

Run the regression suite before packaging or deploying:

```powershell
& 'D:\calorie-agent\backend\.venv\Scripts\python.exe' 'D:\calorie-agent\backend\tests\run_regression.py'
& 'D:\calorie-agent\backend\.venv\Scripts\python.exe' 'D:\calorie-agent\backend\tests\run_agent_eval.py'
& 'D:\calorie-agent\backend\.venv\Scripts\python.exe' 'D:\calorie-agent\backend\tests\run_reminder_eval.py'
& 'D:\calorie-agent\backend\.venv\Scripts\python.exe' 'D:\calorie-agent\backend\tests\run_skill_registry_eval.py'
& 'D:\calorie-agent\backend\.venv\Scripts\python.exe' 'D:\calorie-agent\backend\tests\run_context_loader_eval.py'
& 'D:\calorie-agent\backend\.venv\Scripts\python.exe' 'D:\calorie-agent\backend\tests\run_planner_profile_eval.py'
& 'D:\calorie-agent\backend\.venv\Scripts\python.exe' 'D:\calorie-agent\backend\tests\run_memory_learner_eval.py'
& 'D:\calorie-agent\backend\.venv\Scripts\python.exe' 'D:\calorie-agent\backend\tests\run_reply_guard_v2_eval.py'
& 'D:\calorie-agent\backend\.venv\Scripts\python.exe' 'D:\calorie-agent\backend\tests\run_v2_agent_eval.py'
& 'D:\calorie-agent\backend\.venv\Scripts\python.exe' 'D:\calorie-agent\backend\tests\run_dynamic_planner_eval.py'
& 'D:\calorie-agent\backend\.venv\Scripts\python.exe' 'D:\calorie-agent\backend\tests\run_intake_nutrition_query_eval.py'
```

The suite uses fake in-memory tables, offline V2 fixtures, and fake V2 planner
responses. Planner Profile, Memory Learner, and Reply Guard v2 evals use
offline fixtures. The V2.8 Agent eval verifies shadow/read-only rollout gates.
The V2.4 intake nutrition query eval uses stored intake snapshots only. It does
not call Feishu, DeepSeek, Tencent Cloud, or real Bitable APIs.

Agent trace and eval docs:

```text
D:\calorie-agent\backend\docs\agent-trace.md
D:\calorie-agent\backend\docs\agent-eval.md
D:\calorie-agent\backend\docs\reminders.md
```

## Cloud Smoke Test

After deployment, send these Feishu bot messages:

1. `米饭200g`
2. `今天吃了什么`
3. `撤销上一条`
4. `修改标准 碳水200 蛋白160 脂肪60`
5. `米饭`, then reply `200g`
6. `沙拉汁30g`, then reply `碳水10 蛋白1 脂肪20`

AgentRuntime composite smoke message:

```text
早餐鸡蛋100g，中午米饭200g鸡胸肉150g，查下今天还剩多少
```

Expected: three `record_food` actions run first, then `query_today`; the final
reply is natural language and all totals come from tool execution facts.

Detailed cases are in:

```text
D:\calorie-agent\backend\docs\test-cases.md
```

## Daily Cleanup

Configure a Tencent scheduled trigger or any trusted scheduler to call:

```text
POST https://<your-cloud-domain>/tasks/daily-cleanup?token=<ROLLOVER_TASK_TOKEN>
```

The cleanup performs a hard delete on daily intake records where `date < today`.
It does not delete today's rows and does not back up deleted rows.

## Proactive Reminders

Configure a Tencent scheduled trigger or any trusted scheduler to call:

```text
POST https://<your-cloud-domain>/tasks/reminders?token=<ROLLOVER_TASK_TOKEN>
```

Reminders are conservative by default. If the reminder rule table is missing,
or no rule is enabled, the task does not send messages. See:

```text
D:\calorie-agent\backend\docs\reminders.md
```

## Logs And Metrics

Each processed message logs a `metrics` object with:

- `event_status`
- `intent`
- `parse_success`
- `food_match_count`
- `food_missing_count`
- `food_match_success`
- `pending_status`
- `pending_completed`
- `intake_write_status`
- `intake_write_success`
- `duplicate_skipped`
- `bitable_error`
- `deepseek_error`
- `reply_latency_ms`
- `reporting_enabled`
- `report_type`
- `report_cache_hit`
- `report_generated`
- `report_period_days`
- `trend_query_used`
- `target_hit_rate`
- `report_reply_guard_passed`
- `report_write_failed`
- `reply_guard_v2_enabled`
- `reply_guard_violation_count`
- `reply_guard_violation_types`
- `unsupported_number_count`
- `unsupported_status_count`
- `privacy_risk_blocked`
- `reminder_enabled`
- `reminder_candidates_count`
- `reminder_sent_count`
- `reminder_skipped_count`
- `reminder_skip_reason`
- `anomaly_detected_count`
- `pending_reminder_count`
- `target_gap_reminder_count`
- `daily_summary_sent`
- `reminder_rule_updated`
- `reminder_send_failed`

Use these fields to track parsing success, food match rate, pending completion,
write success, duplicate events, Bitable errors, DeepSeek errors, and latency.

Each final message log also includes a `diagnostics` object for step-by-step
troubleshooting. The object is informational only and does not change matching,
pending, write, undo, or cleanup behavior.

Key fields:

- `diagnostics.version`: currently `processing_diagnostics.v1`.
- `diagnostics.route`: legacy parser, agent runtime, pending, or unknown route.
- `diagnostics.steps`: ordered checkpoints for message receipt, config, plan or
  intent parsing, legacy food lookup, agent tool results, pending, intake write,
  summary loading, and reply guard.
- `diagnostics.steps[].deepseek`: safe DeepSeek config status, including whether
  the key is configured, the model, API base, and whether model/base came from
  env or defaults.
- `diagnostics.parse_error`: parser failure stage, exception type, sanitized
  message, HTTP status when available, and category.
- `diagnostics.food_lookup.legacy_lookup`: legacy add-food matches, missing
  items, and per-item `match_diagnostics`.
- `diagnostics.food_lookup.agent_tools`: agent tool food matching details,
  including query, selected candidate, ranked candidates, missing items, and
  status.
- `diagnostics.food_lookup.recorded_count`,
  `diagnostics.food_lookup.pending_count`, and
  `diagnostics.food_lookup.error_count`: quick counters for cloud filtering.

Use `match_diagnostics.candidates` and `agent_tools[].food_match_candidates` to
check whether a food was absent from the table, normalized differently, matched
by alias, or rejected because the candidate confidence was insufficient.

For a user-visible parser failure, first check `diagnostics.parse_error` and the
`config_loaded.deepseek` step. Common categories are `missing_api_key`,
`auth_error`, `invalid_request`, `rate_limited`, `timeout`, and
`network_error`.

If cloud logs show `Request was canceled by client` or Tencent status `499`,
first confirm the Feishu callback response body is `status=accepted`. Then
check whether later logs include `post_ack_processing_started`, the final
diagnostics payload, and `post_ack_processing_completed`. A retry that arrives
while the previous attempt is still fresh logs
`post_ack_processing_skipped_duplicate`; if the previous attempt stays in
`processing` for more than 15 seconds, the next retry takes over and processes
the message again. Intake writes still use `message_id` duplicate protection.

## Troubleshooting

No bot reply:

- Check `FEISHU_APP_ID` and `FEISHU_APP_SECRET`.
- Check bot message-send permission.
- Check Tencent logs for `send_result` or `message_processing_failed`.

Repeated replies:

- Check Feishu callback logs and Tencent timeout behavior.
- The function acknowledges callbacks quickly, continues processing after ACK,
  and uses `message_id` plus stable UUIDs for duplicate protection.

Bitable HTTP 400 or write failure:

- Check `BITABLE_APP_TOKEN`, table IDs, field mappings, and app permissions.
- For `batch_create`, the function uses stable uuidv4-format `client_token`.

Pending completion does not work:

- Check `BITABLE_PENDING_TABLE_ID`.
- Confirm pending table fields match `docs/bitable-schema.md`.
- Check the pending row has `status=pending` and `expires_at` is still in the
  future.

Today's summary looks wrong:

- Check `APP_TIMEZONE_OFFSET_HOURS`.
- Check `BITABLE_FIELD_INTAKE_DATE_VALUE_TYPE`.
- Check that active records use `status=valid` and revoked records use
  `status=deleted`.

Report looks wrong:

- Check that the intake table stores kcal/carbs/protein/fat snapshots.
- Check `REPORT_TIMEZONE_OFFSET_HOURS` and the intake `date` value type.
- Check the daily standard table if target-hit rates look wrong.
- If cache seems stale, ask the bot to regenerate the report or clear the
  optional report cache row.

Reminder did not send:

- Check `BITABLE_REMINDER_RULE_TABLE_ID` and `BITABLE_REMINDER_EVENT_TABLE_ID`.
- Check the rule has `enabled=true`, `status=active`, a non-empty `chat_id`,
  and a schedule time that has already passed.
- Check `reminder_skip_reason` for `quiet_hours`, `max_per_day_reached`,
  `group_chat_not_authorized`, or `reminder_event_table_not_configured`.
