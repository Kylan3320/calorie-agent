# 知识库与检索契约

生产知识维护在 Bitable；LightRAG 索引是发布时构建的派生快照。
`agent/storage/knowledge_seed.py` 用于初始化和 FakeStore 评测，不能静默替代生产语料。

## knowledge_base（BITABLE_KNOWLEDGE_TABLE_ID）

| 字段 | 语义 |
|---|---|
| entry_id | stable_uuid("calorie-agent:knowledge:{normalized_question}")，问题归一化复用 knowledge_retriever.normalize_text。 |
| question / answer | 标准问题和答案原文。 |
| aliases / tags | 顿号分隔的别名和主题标签。 |
| source | 原文来源，随答案展示。 |
| status | 空或 active 有效；deleted 下架。读取兼容已有启用、正常、有效状态。 |

`sync_knowledge` 按 entry_id 新增或更新指定条目，不删除其他知识。
CosStore 的 FAQ 读取已发布的 `source:knowledge` 快照，表内编辑不会自动更新该快照；LightRAG 需构建并发布新索引。
完整快照在新空目录构建，旧索引保留，避免已下架内容残留在增量索引中。

## 检索接口

配置 kind 为 faq 或 lightrag；未知 kind 显式报错。
仓库默认 faq，生产 Layer 部署通过 KNOWLEDGE_RETRIEVER=lightrag 覆盖。

```python
retrieve(query: str, entries: list[dict]) -> dict
# {status: "ok"|"not_found", entries: [{entry_id, question, answer, source, score}], evidence: str}
```

- faq 消费 Store 的有效条目，评分和截断参数由实现维护。
- lightrag 消费已发布索引，用 aquery_data 返回 chunks 原文，不生成模型答案。
- 错误由工具执行层记录为 error，不能伪装成未命中。
- 检索层提供本轮原文证据及来源；回复由上层运行时依据证据综合组织，本轮来源标识按动作序号唯一分配，回复可引用来源并自然解释，原文按需提供。未命中返回not_found，上层继续一般解释并指出影响结论的具体缺口。
- 交流体验行为见 [规格](../CONVERSATION_EXPERIENCE_SPEC.md)。来源字段取自实际原文；需核实权威归属时保留可定位的出处。
- 记录、今日查询和食物营养查询按各自意图处理；历史聚合走 query_history。

构建、依赖和 Layer 发布步骤见 README.md 及 deploy 脚本。
