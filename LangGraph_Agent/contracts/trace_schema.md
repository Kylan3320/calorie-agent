# Trace 契约

每次图执行追加一行 JSONL，同时输出到 stdout。
文件由 TRACE_PATH 指定，默认 /tmp/traces.jsonl；TRACE_ENABLED=false 只关闭文件写入。
线上 usage_events 为独立指标，字段见 bitable_schema.md。

```json
{
  "schema_version": "calorie_agent_trace.v1",
  "created_at": "ISO8601 UTC",
  "trace_id": "v4_<uuid12>",
  "message_id": "", "user_id": "", "chat_id": "",
  "preset": "live", "planner": "deepseek",
  "raw_text": "", "normalized_text": "",
  "pending_route": {},
  "plan": {}, "planner_fallback": false,
  "policy_decision": {}, "tool_results": [], "observations": [],
  "final_reply": "", "reply_source": "agent|safe_fallback|pending",
  "guard": {"passed": true, "violations": []},
  "errors": [], "latency_ms": 0
}
```

latency_ms 是序列化时的实际耗时，与图返回值一致。
不写入密钥或完整 Authorization 头；业务原文和事实供追踪使用。
字段变更同步此契约和实际消费者；存在旧消费者时明确版本迁移，不无条件保留废弃字段。
