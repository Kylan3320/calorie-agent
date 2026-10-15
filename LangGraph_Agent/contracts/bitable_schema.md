# 契约：Bitable 表结构与幂等键

唯一权威：改表字段/幂等键必须同交付更新本文件。字段名可用环境变量覆盖（V2 兼容），
以下写的是默认列名。沿用 V2 已验证的多维表格，不新建体系。

## 表与字段

### foods（食物库，BITABLE_FOOD_TABLE_ID）
`food_id`、`food_name`、`alias`（逗号/顿号分隔多别名）、`carbs_per_100g`、`protein_per_100g`、
`fat_per_100g`、`status`。有效 status：空 / active / 启用 / 正常 / 可用。缺名或缺任一
每 100g 营养的行跳过。
薄荷 M1 新增列：`source`（数据来源：boohee / 空=人工）、`boohee_id`（薄荷食物 code，
用于去重与追溯）。写入走 `write_food`（见 tool_protocol.md）。

### intake_records（摄入主表，BITABLE_INTAKE_TABLE_ID）
`date`（当天 0 点 epoch 毫秒；`BITABLE_FIELD_INTAKE_DATE_VALUE_TYPE=text` 时为 YYYY-MM-DD）、
`user_id`、`message_id`、`raw_text`、`meal_type`、`food_id`、`food_name_snapshot`、`grams`、
`carbs`、`protein`、`fat`、`kcal`、`status`（valid/deleted）、`created_at`（毫秒）。
营养为写入时快照；历史查询读快照，不按当前食物库重算。

### standards（每日标准，BITABLE_STANDARD_TABLE_ID）
`user_id`、`standard_type`（daily/default/默认）、`date`（空=长期有效）、`kstandard`、
`cstandard`、`pstandard`、`fstandard`。缺 kcal 时按 碳水×4+蛋白×4+脂肪×9 重算。
默认值：kcal 2230 / carbs 250 / protein 150 / fat 70（source=default）。

### pending_tasks（待补全，BITABLE_PENDING_TABLE_ID）
`pending_id`、`user_id`、`chat_id`、`source_message_id`、`intent`、`food_name`、`grams`、
`meal_type`、`raw_text`、`status`（pending/completed/cancelled）、`created_at`、
`expires_at`（默认 TTL 24h）。P0 的 intent：`missing_grams`、`missing_food`、`confirm_undo`。

### usage_events（轻量指标，BITABLE_USAGE_TABLE_ID）
`event_id`、`event_type`（message_processed）、`message_id`、`user_id`、`preset`、
`planner`、`status`（ok/error）、`error_type`、`action_count`、`reply_sent`、
`latency_ms`、`created_at`。

### user_aliases（账号别名映射，BITABLE_ALIASES_TABLE_ID；2026-09-07 方案 B）
`alias_open_id`（别名账号 open_id）、`canonical_user_id`（主账号 open_id）、
`status`（空/active 有效）。同一人多账号共享同一份数据：webhook 解析消息后按
`load_user_aliases()` 把别名 open_id 归一为主账号。不加行的飞书账号 = 独立数据
（天然按 user_id 区分，无需任何配置）。读取带 TTL 缓存，改表即生效无需重部署。

## 幂等键公式（不得另造）

`message_key = message_id`（为空时回退 `chat_id:text`）。
`stable_uuid(text) = sha256(text)[:16] 置 UUIDv4 版本位`。

| 用途 | 公式 |
|---|---|
| 摄入 batch_create client_token | `stable_uuid("calorie-agent:{message_key}:intake")` |
| 飞书回复 uuid | `stable_uuid("calorie-agent:{message_key}:reply")` |
| pending_id | `stable_uuid("calorie-agent:pending:{message_key}:{intent}:{food_name}")` |
| pending client_token | `stable_uuid("calorie-agent:pending-create:{message_key}:{intent}:{food_name}")` |
| food_id | `stable_uuid("calorie-agent:food:{normalized_name}")` |
| 建食物 client_token（薄荷 M1 已启用） | `stable_uuid("calorie-agent:food-create:{normalized_name}")` |
| usage event_id | `stable_uuid("calorie-agent:usage:{message_key}")` |

## 写入双层防重（必须同时保留）

1. 写前查重：按 message_id 列出 intake 表比对，命中 → `duplicate_skipped`。
2. client_token：同上表传入 batch_create，飞书服务端再拦一次。

## 状态过滤口径

- 有效摄入：status 为空或 ∈ {valid, active, normal, 启用, 正常, 有效, 已记录}。
- "今天"：date ∈ [当天 0 点, 次日 0 点)（按 `APP_TIMEZONE_OFFSET_HOURS`，默认 +8）。
- 撤销 = status 置 deleted（软删除），永远不调 Bitable DELETE API。

## API 注意

- 单次 batch_create 上限一批；列表接口 page_size=500 翻页拉全。
- 客户端对 429/5xx 做一次退避重试（V2 没有重试，V4 补上）。
