# 知识库与检索契约

生产知识维护在 Bitable；LightRAG 索引是发布时构建的派生快照。
`agent/storage/knowledge_seed.py` 用于初始化和 FakeStore 评测，不能静默替代生产语料。

## knowledge_base（BITABLE_KNOWLEDGE_TABLE_ID）

| 字段 | 语义 |
|---|---|
| entry_id | stable_uuid("calorie-agent:knowledge:{normalized_question}")，问题归一化复用 knowledge_retriever.normalize_text。 |
| question / answer | 标准问题和答案原文。 |
| aliases / tags | 顿号分隔的别名和主题标签。 |
| source | 条目标注的出处，随答案展示；仅有该字段不证明已获取外部全文。 |
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
- 检索层提供本轮原文证据及来源；回复由上层运行时依据证据综合组织，来源标识按会话、固定检索动作与原文/出处身份稳定分配。answer为知识库条目原文，source为条目标注的出处；未实际取回外部全文时，不把条目转述称为外部文献逐字原话。未命中返回not_found，说明与所问相关的具体缺口。
- 通用知识问题已有直接依据即结束，不因会话有个人目标或菜单追加个体评价／改餐建议。用户明确问本人时，用相应业务工具核对所问日期的目标／摄入，并与检索可支持的知识判断分开；设置值不证明个人适宜性，缺适用依据须明说。保留有据的混合任务及跨轮追问，详见交流体验规格4.2。
- 交流体验行为见 [规格](../CONVERSATION_EXPERIENCE_SPEC.md)。来源字段取自实际原文；需核实权威归属时保留可定位的出处。
- 记录、今日查询和食物营养查询按各自意图处理；历史聚合走 query_history。

构建、依赖和 Layer 发布步骤见 README.md 及 deploy 脚本。

### 跨轮证据身份与可见性

工具层为每条entry增加source_id（K加身份哈希前96bit十进制）、evidence_id（完整hash），legacy_source_id仅为旧局部编号兼容别名。重放不变，不同检索/原文不覆盖。同条证据只投影一份原文。

渲染只使用最后实际模型请求中可见的真实ToolMessage，或由真实知识工具消息转换、携带tool_evidence_origin=query_knowledge的清洁AI证据JSON。标记仅验证内部来源资格，answer/source原文仍须存在于实际wire消息正文。模型输出的普通JSON、摘要或不可见历史不能冒充工具原文。

旧原生工具结果可按所属轮次/动作唯一重绑K101等编号；无原文或多义的旧断言收缩并说明重检缺口，不能只删编号保留无据数字。journal.visible_evidence保存当次实际可见映射，最终渲染不借隐藏完整工具事实填补缺口。
