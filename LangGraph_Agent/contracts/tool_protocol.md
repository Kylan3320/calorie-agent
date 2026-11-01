# 契约：存储接口与工具注册

两个实现保持相同的业务输入输出语义：`BitableStore`（真实）与 `FakeStore`（内存，本地评测/假写预设）。
FakeStore 使用固定摄入日期、内存状态，不模拟远端传输或持久化。
改接口同步两个实现及本文档。

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
    # [{record_id, food_id, name, aliases, carbs_per_100g, protein_per_100g, fat_per_100g, source, boohee_id}]

load_knowledge() -> list[dict]
    # [{entry_id, question, answer, aliases, tags, source}]  status 为空/active 的有效条目
    # 未配置知识表时返回 []（优雅降级，不抛错）；见 contracts/knowledge_schema.md

sync_knowledge(entries: list[dict]) -> dict
    # entry_id/question/answer/aliases/tags/source；按 entry_id 新增或更新
    # 返回 {created_count, updated_count}，不删除未提供条目；创建 token 见 bitable_schema.md

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
    # items: [{food_id, matched_name, grams, nutrition{carbs,protein,fat,kcal}, date?, meal_type?}]
    # 返回 {status: "created"|"duplicate_skipped"|"nothing_to_write", created_count, records: [{record_id}]}
    # date 为可信执行快照中的 YYYY-MM-DD，重试保持原记录日期；创建响应缺行时报错。
    # 幂等：message_id 查重 + client_token（见 bitable_schema.md）

last_intake(user_id: str) -> dict | None
    # 读取最近有效记录，不修改；用于建立撤销目标快照。

undo_last(user_id: str, record_id: str = "") -> dict
    # {status: "deleted"|"nothing_to_undo", item}
    # 确认执行必须提供快照 record_id；重复软删除同一目标仍返回 deleted，不选择前一条。

create_pending(message: dict, intent: str, food_name: str, grams: float|None, meal_type: str, payload: dict|None = None) -> dict
    # 完整 task（含 record_id、payload、真实 pending/completed/cancelled 状态），未配置返回 not_configured。
    # 首次创建的快照胜出；已完成任务不重开。尚无候选的 pending 可补充首次候选快照。

get_pending(pending_id: str) -> dict | None
    # 按固定任务 ID 读取当前状态，包含已完成/已取消任务。

load_active_pending(user_id: str, chat_id: str) -> dict | None
    # 仅业务追问，排除内部 message_received/message_execution。

mark_pending_status(task: dict, status: str) -> dict

load_execution(message: dict) -> dict | None
begin_execution(message: dict, payload: dict) -> dict
save_execution(task: dict, payload: dict, *, completed: bool = False) -> None
load_unfinished_executions() -> list[dict]
    # 与业务 pending 共表，沿用 pending 创建幂等公式；payload 保存原消息、首份已验证计划、
    # intake_date、追问/候选/目标快照、成功工具事实及最终图结果。内部任务不过期。
    # 恢复列表按消息去重，优先 receipt；完成图结果重放直接返回原结果，不重新规划或写入。

append_usage_event(event: dict) -> None   # 失败只记日志，不抛出（指标不阻断主链路）
```

`message` dict 固定 4 键：`message_id`、`user_id`、`chat_id`、`text`（app.py 解析事件后构造）。

## 工具注册（agent/tools/registry.py）

- 每个工具：`{"safety": "read_only"|"write"|"destructive", "runner": fn}`。
- runner 签名：`run(message: dict, params: dict, ctx: ToolContext) -> ToolResult`。
- `ToolContext`：`store`（按预设绑定）、`config`、`context`（可信 pending_route、active_pending、intake_date）。
- pending_router 只恢复动作，取消/暂缓/补全同样经过 policy_gate；只有工具和执行器修改业务状态。
- 风险等级取注册表，模型参数不提供确认凭据；确认任务绑定固定目标，候选编号绑定已展示快照；未成功动作重放前重新核实原任务仍 pending 且未过期。
- 工具不得绕过 store 直接发 HTTP；不得在 ToolResult.data 之外私藏事实。

## 工具状态语义（composer/guard 依赖）

| status | 含义 | 回复口径 |
|---|---|---|
| ok | 工具执行成功 | 查询只陈述查询事实；已记录须有 created_count>0，或查询快照明确存在记录 |
| duplicate_skipped | 工具层创建防重命中 | 明确说"重复消息，已跳过"；已完成图的重放返回原事实和原回复 |
| pending_cancelled / pending_deferred | 待补全任务取消或保留 | 只陈述对应任务状态 |
| food_library_added | 确认候选后成功建库或发现已存在 | 可说明已加入食物库，不宣称已记录摄入 |
| pending_created | 信息缺失挂起 | 只能说"请补充"，禁止说已记录 |
| needs_confirmation | 食物歧义 | 列候选请用户确认 |
| needs_confirmation_plan | destructive 待确认（计划级） | 询问确认 |
| error / blocked_* | 失败/拦截 | 稳定解释，不泄露内部细节 |

## 工具清单

| tool | safety | 说明 |
|---|---|---|
| record_food | write | 匹配→写快照；缺克重/未知→pending；歧义→候选确认 |
| query_today | read_only | 今日汇总 + 剩余（读 store 缓存的 standard） |
| query_food_nutrition | read_only | 先当日摄入快照聚合，再食物库按克重计算 |
| undo_last | destructive | 撤销最近一条 valid（软删除） |
| query_knowledge | read_only | 知识问答：knowledge_retriever 接缝检索 knowledge_base（见 knowledge_schema.md） |
| query_history | read_only | 历史聚合：时间窗 + 可选食物名，返回每日明细/总计/超标日 |

## 本地 DSH 桥接

POST /api/agent：{text, session_id, message_id, planner?, preset?, store_kind?}。
session_id 来自 DSH 会话，message_id 在该会话内稳定；后端组合为图的 message_id。
chat_id 按 DSH 会话隔离；FakeStore 按会话保留。真实表格使用 CONSOLE_USER_ID，经账号别名归一。
返回图结果和 tool_events，不发送飞书消息。

## 回复与失败

回复由固定格式从工具事实生成；守卫核对完整渲染，字段、数值和单位不能互换。
知识答案须为 evidence 中的原文。任一工具失败进入图 errors，回复明确失败；成功事实可保留以便恢复。
执行器按固定计划顺序保存成功结果，失败后停止，不重跑已保存动作。

飞书 ACK 前持久化 message_received（原消息及收件日期）；图完成和回复发送分别确认。
每次投递最多尝试两次，发送失败沿用原回复及 reply uuid。
持续失败/进程退出后的收件任务由 `python app.py --retry-pending` 显式恢复；不增加后台调度。
