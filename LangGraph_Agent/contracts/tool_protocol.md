# 契约：存储接口与工具注册

两个实现必须行为一致：`BitableStore`（真实）与 `FakeStore`（内存，本地评测/假写预设）。
改接口必须同交付更新本文件并同步两个实现。

## Store 接口

所有方法同步阻塞；时间/时区由 store 依 `timezone_offset_hours` 自行计算。

```python
today_string() -> str
    # store 的"今天"（YYYY-MM-DD）；query_history 的时段解析以此锚定，
    # 保证工具窗口与记录/查询的日期口径一致（评测可注入固定日期）

load_user_aliases() -> dict[str, str]
    # {别名 open_id: 主账号 open_id}；app.py 解析消息后归一 user_id（方案 B，
    # 同一人多账号共享数据）。来源 = env USER_ID_ALIASES 兜底 + user_aliases 表
    # （表行优先，BITABLE_ALIASES_TABLE_ID，TTL 缓存）。未配置返回 {}，天然独立数据。

load_foods() -> list[dict]
    # [{record_id, food_id, name, aliases, carbs_per_100g, protein_per_100g, fat_per_100g}]

load_knowledge() -> list[dict]
    # [{entry_id, question, answer, aliases, tags, source}]  status 为空/active 的有效条目
    # 未配置知识表时返回 []（优雅降级，不抛错）；见 contracts/knowledge_schema.md

write_food(food: dict) -> dict
    # food: {name, aliases?, carbs_per_100g, protein_per_100g, fat_per_100g, source?, boohee_id?}
    # 写入食物库一行（薄荷 M1 确认入库）；返回 {status: "created"|"duplicate", food_id, name}
    # 幂等：写前按 food_id 查重 + client_token（bitable_schema.md"建食物"公式）；
    # 写成后必须失效进程内 foods 缓存

today_summary(user_id: str) -> dict
    # {date, count, items, totals{carbs,protein,fat,kcal}, source}
    # items: [{record_id, message_id, meal_type, food_name, grams, created_at, nutrition{...}}]

summarize_history(user_id: str, start_date: str, end_date: str, food_query: str = "") -> dict
    # 历史 [start_date, end_date) 聚合，日期 YYYY-MM-DD，含头不含尾；RAG M2 query_history 用
    # 返回 {start_date, end_date, food_query, total{count,grams,kcal,carbs,protein,fat},
    #       days: [{date, count, grams, kcal, carbs, protein, fat}]（升序）,
    #       over_budget_days: [date]}  # kcal 超出当前生效标准的日期（按食物过滤时不计）
    # 过滤：status 有效 + user_id + food_name 包含匹配（normalize_food_name）

get_standard(user_id: str) -> dict
    # {carbs, protein, fat, kcal, source: "default"|"bitable"}

write_intake(message: dict, items: list[dict], meal_type: str) -> dict
    # items: [{food_id, matched_name, grams, nutrition{carbs,protein,fat,kcal}}]
    # 返回 {status: "created"|"duplicate_skipped"|"nothing_to_write", created_count}
    # 幂等：message_id 查重 + client_token（见 bitable_schema.md）

undo_last(user_id: str) -> dict
    # {status: "deleted"|"nothing_to_undo", item}

create_pending(message: dict, intent: str, food_name: str, grams: float|None, meal_type: str) -> dict
    # {status: "created"|"not_configured", pending_id, intent, food_name, grams}

load_active_pending(user_id: str, chat_id: str) -> dict | None

mark_pending_status(task: dict, status: str) -> dict

append_usage_event(event: dict) -> None   # 失败只记日志，不抛出（指标不阻断主链路）
```

`message` dict 固定 4 键：`message_id`、`user_id`、`chat_id`、`text`（app.py 解析事件后构造）。

## 工具注册（agent/tools/registry.py）

- 每个工具：`{"safety": "read_only"|"write"|"destructive", "runner": fn}`。
- runner 签名：`run(message: dict, params: dict, ctx: ToolContext) -> ToolResult`。
- `ToolContext`：`store`（按预设绑定）、`config`、`context`（图内已取的 pending/汇总）。
- 工具不得绕过 store 直接发 HTTP；不得在 ToolResult.data 之外私藏事实。

## 工具状态语义（composer/guard 依赖）

| status | 含义 | 回复口径 |
|---|---|---|
| ok | 已写入/已查询 | 可宣称已记录/给出数字 |
| duplicate_skipped | 幂等命中跳过 | 明确说"重复消息，已跳过" |
| pending_created | 信息缺失挂起 | 只能说"请补充"，禁止说已记录 |
| needs_confirmation | 食物歧义 | 列候选请用户确认 |
| needs_confirmation_plan | destructive 待确认（计划级） | 询问确认 |
| error / blocked_* | 失败/拦截 | 稳定解释，不泄露内部细节 |

## 工具清单（P0）

| tool | safety | 说明 |
|---|---|---|
| record_food | write | 匹配→写快照；缺克重/未知→pending；歧义→候选确认 |
| query_today | read_only | 今日汇总 + 剩余（读 store 缓存的 standard） |
| query_food_nutrition | read_only | 先当日摄入快照聚合，再食物库按克重计算 |
| undo_last | destructive | 撤销最近一条 valid（软删除） |
| query_knowledge | read_only | 知识问答：knowledge_retriever 接缝检索 knowledge_base（M1，见 knowledge_schema.md） |
| query_history | read_only | 历史聚合（RAG M2）：时间窗 + 可选食物名，返回每日明细/总计/超标日 |
