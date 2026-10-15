# 契约：trace 格式

一次消息处理 = JSONL 一行，文件 `reports/traces.jsonl`（追加写）。本地与线上同构；
线上另写 usage_events 表（真实 store 时），字段见 bitable_schema.md。

```json
{
  "schema_version": "calorie_agent_trace.v1",
  "created_at": "2026-09-05T00:00:00+00:00",
  "trace_id": "v4_<uuid12>",
  "message_id": "", "user_id": "", "chat_id": "",
  "preset": "live", "planner": "deepseek",
  "raw_text": "", "normalized_text": "",
  "pending_route": "",
  "plan": {}, "planner_fallback": false,
  "policy_decision": {},
  "tool_results": [], "observations": [],
  "final_reply": "", "reply_source": "agent|safe_fallback|pending",
  "guard": {"passed": true, "violations": []},
  "errors": [], "latency_ms": 0
}
```

规则：
- `trace_id` 前缀固定 `v4_`；`reply_source` 记录回复最终来源（guard 拦截后为 safe_fallback）。
- 只追加不修改；行内禁止出现 app_secret / api_key / 完整 Authorization 头。
- 改字段必须同交付更新本文件；字段只增不改名，废弃字段置 null 保留一个版本。
