# 契约：知识库表结构与检索接缝（RAG M1，D13-D17）

知识问答 = 分流后的"静态营养知识"链路（结构化历史查询走 query_history，不在此契约）。
原则沿袭主链路：模型只做规划，检索只给事实，回复中的数字必须能溯源到检索原文。

## knowledge_base 表（BITABLE_KNOWLEDGE_TABLE_ID）

| 列 | 类型 | 说明 |
|---|---|---|
| entry_id | 文本 | `stable_uuid("calorie-agent:knowledge:{normalized_question}")` |
| question | 文本 | 标准问法 |
| answer | 文本 | 答案原文（检索后原样进回复，不做改写；可含数字，guard 据此溯源） |
| aliases | 文本 | 别名，顿号分隔（检索匹配用） |
| tags | 文本 | 主题标签（如 减脂、主食、水果），检索加分用 |
| source | 文本 | 来源标注（如 中国居民膳食指南2022），回复尾部展示 |
| status | 文本 | 空/active 有效；deleted 下架（软删，不物理删） |

种子语料权威源：`agent/storage/knowledge_seed.py`（FakeStore 种子与 Bitable 导入脚本
`deploy/seed_knowledge.py` 共用；改语料 = 改此文件 + 重跑导入 + 重新部署）。

## knowledge_retriever 接缝（config.yaml `knowledge_retriever:`）

| 实现 | kind | 数据源 | 状态 |
|---|---|---|---|
| FAQ 检索器 | `faq` | store.load_knowledge() + food_match 式模糊评分 | M1（生产默认） |
| LightRAG | `lightrag` | 预构建索引产物（云函数 Layer 挂载 /tmp 或包内路径） | M3 |

```python
retrieve(query: str, entries: list[dict]) -> dict
# {"status": "ok"|"not_found", "entries": [{entry_id, question, answer, source, score}],
#  "evidence": "<命中条目原文拼接，guard 数字溯源的依据>"}
```

faq 评分沿用 food_match 阈值思想：exact/别名 1.0、前缀 0.9、包含 0.82、
tags 命中 0.3 加成；CONFIRM 阈值 0.5，取 top2，去重后按分数排序。
kind 未知 → 工具返回 error（配置错误必须显性暴露，不许静默降级）。

### lightrag 实现细节（M3）

- **懒加载**：`import lightrag` 只发生在 kind=lightrag 的检索调用内（其他意图不付冷启动成本）。
- **事实优先**：用 `aquery_data()` 取 chunks 原文作为 evidence/entries（answer 字段=chunk 文本），
  **不使用** LightRAG 的 LLM 生成答案——保证回复数字可溯源（D17）。
- **索引产物**：`deploy/rag_index/`（LightRAG working_dir），由 `deploy/ingest_knowledge.py`
  从 knowledge_seed + Bitable knowledge_base 构建；随 Layer 发布。
- **注入**：llm_model_func=DeepSeek 裸 HTTP；embedding_func=硅基流动 bge-m3（SILICONFLOW_API_KEY）。
- **部署形态**：RAG 依赖（精简 site-packages：删 pandas/xlsxwriter/google/genai/api/parser）+ 索引产物
  打成云函数 Layer（≤200MB），主函数 zip 保持精简；`knowledge_retriever: lightrag` 切换即启用。

## 回复与守卫口径（reply_composer / reply_guard）

- ok：回复 = 答案原文（不加模型改写）+ 尾行 `来源：<source>`。
- not_found：固定文案「知识库里还没有收录这个问题…」，禁止编造、禁止出现任何营养数字。
- guard 增量：回复含「根据知识库」「知识库显示」但无 query_knowledge ok 事实 →
  violation `knowledge_claimed_sourced`；数字溯源沿用既有机制（entries 原文在
  tool_results.data 内，数字自动进入白名单）。

## 工具与意图

- 工具 `query_knowledge`：read_only；parameters `{query}`；状态 ok / not_found / error。
- 意图 `ask_knowledge`：deterministic 关键词规则与 deepseek prompt 双后端一致；
  不得劫持记录/今日查询/营养数值查询（后者优先级更高）。
