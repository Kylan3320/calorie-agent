# Trace v2

每次运行 stdout 输出一行 JSON，并按 TRACE_PATH 追加（默认 /tmp/traces.jsonl，TRACE_ENABLED=false 仅关文件）。不输出密钥。

字段：schema_version=calorie_agent_trace.v2、created_at、trace_id、message_id/user_id/chat_id/text/raw_text、preset、model、retriever、status、reply/reply_source、latency_ms、errors、guard_result、model_calls、native_calls、model_requests、tool_results。

native_calls 为按模型返回顺序固定的 `{call_id,tool,args,index,action_id}`；model_requests 保存本轮每次主决策当前轮实际消息序列、history_message_count及从注册表派生的 tools（相同tools_digest后续仅存工具名，完整较早历史在会话messages中保存一次），用于避免将整段历史重复写入Bitable；tools（name/description/schema/risk），消息包含此前 ToolMessage，供核对原生工具选择、结果回传与后续决策。

tool_results 为完整原生工具事实；query_knowledge.data.retriever 标记实际检索器，evidence 保存本轮原文。model_reply保存模型原始最终正文，reply是实际交付正文；response_adjustments记录来源引用替换或确认提示合成等局部调整。reply_source为agent/agent_adjusted/fallback。guard_result记录checked_scope、fallback_used及具体调整，不把自然语义作为已全面验证。

v1的plan、pending_route、policy_decision、planner_fallback移除。CLI、控制台/trace和DSH tool_events已改用原生轨迹，不保留旧协议转译。

model_requests的ToolMessage审计项用result_ref（call_id）和原content_sha256关联已持久化工具事实，不重复嵌入整份content。实际模型输入和压缩保留消息仍在journal.messages/session.messages中使用LangChain标准序列化完整保存。
