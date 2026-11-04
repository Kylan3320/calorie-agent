# Bitable链路提速历史记录（2026-11-01）

当日实施了连接池、请求作用域复用、今日查询列裁剪、固定动作恢复及分段计时；后续由COS生产链路替代。当前部署见 [生产交付记录](LATENCY_DELIVERY.md)。

## 实施与证据

| 项目 | 当时结果 |
|---|---|
| 构建 | 起点personal-eb3823498db6f04e，提速候选personal-248febd314d5d571，worker修正版personal-e9be6478adabcf35；Linux3.10／Layer6装配通过。 |
| 连接与读取 | 主模型、飞书／Bitable复用HTTP池，LightRAG回调跨asyncio.run可用；今日calories只读实际列，按字段类型过滤日期，保留全部匹配页及旧空用户判定。 |
| 恢复 | 固定批次、prepared、原日期及业务结果持久化；最终回复重用正文／UUID，旧任务不覆盖新会话。 |
| 离线与真实模型 | E01–E06有对应通过证据；固定替身HTTP结构计数16次。主要日志为latency-user-filter-recheck.log、latency-rag-loop-check.log、latency-final-recovery-recheck.log、latency-review-filter.log及latency-real-*.log/json。 |
| 异步承接 | 最初ACK后Web执行缺乏可靠完成保障；获授权后使用提醒Event函数作为消息worker，持久收件且异步Invoke接受后ACK，失败503。主函数角色绑定当时受身份校验阻碍，后续在COS正式切换中完成。 |
| 旧只读实测 | om_x100b61477b94f4a0cfd7d0f2873b9e0最终完成0kcal；T_service46696.168ms、T_user47427.294ms，5秒目标未通过。见latency-live/sample-extra-query.json。原定12条样本未执行。 |

旧只读样本可归属pending HTTP23157.077ms、模型3088.672ms、intake HTTP1761.180ms、发送559.263ms，其余18129.976ms未完整归属。当时只有分段HTTP超时，持续保活的整次墙钟限制后于2026-11-03实现，见 [交流体验验收记录](CONVERSATION_DELIVERY.md)。

旧摄入清理后仍0条、0kcal，设置保持，未完收件0，审计保留；见latency-live/cleanup.json。历史候选失败、恢复及各次复验原始日志保存在reports及生产交付记录所列归档中。
