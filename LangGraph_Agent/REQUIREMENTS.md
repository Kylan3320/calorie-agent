# REQUIREMENTS.md — 需求基线 v1

需求来源：2026-09 与客户的两轮方案对齐。语义歧义以本文档为唯一仲裁。
文档状态：生效。改动本文档 = 需求变更，需客户确认。

## 1. 产品定位

单用户飞书饮食记录 Agent：用户在飞书机器人会话里用自然语言完成"记录 / 查询今日 /
撤销 / 缺失信息追问补全"。数值事实（kcal、碳水、蛋白质、脂肪）一律来自食物库与摄入
快照，不来自模型生成。产品原则：**模型做规划，工具做事实，评测做门禁**。

## 2. 客户拍板记录（2026-09-04/05）

| # | 决定 |
|---|---|
| D1 | 存储用飞书多维表格（Bitable，同 V2 路线），不引入 MySQL 等外部数据库 |
| D2 | 继续部署在腾讯云函数（Python 3.10，端口 9000，webhook 快速 ACK） |
| D3 | 多租户隔离彻底砍掉（tenant_id、身份映射、权限分级全部不做） |
| D4 | 家人代记词表拦截一并砍掉（"妈妈/爸爸"等文本不做特殊拦截） |
| D5 | 保留 LangGraph 但克制：直线图 + 少量条件边，不用循环/重规划/checkpoint |
| D6 | RAG 整目录砍掉，后续有需要再排期（优先级靠后）→ 2026-09-06 变更，见 D13-D17 |
| D7 | 参考 DeepSeek Harness 做网页配置台（P2）→ 2026-09-06 二次变更：控制台直接基于 DSH 改造，见 D19 |
| D8 | 单用户：只服务客户本人的飞书账号 |
| D9 | 配置三层：secrets=环境变量；工程配置=config.yaml（改后重新部署）；每日标准=Bitable 标准表 |
| D10 | 旧 V2/V3 云函数包冻结不动（作品集+参考源），新工作全部在 LangGraph_Agent/ |
| D11 | 旧候选体系（v2 readonly / v3.1 write / v4.2 core / v4.3 single-writer）塌缩为三个预设 |
| D12 | 保留 usage 指标最轻版（一张 Bitable 表），feedback/retention 体系不做 |
| D13 | RAG 回归为"知识问答能力"，按问题分流：结构化历史查询用聚合工具（纳入范围）；静态营养知识/个人文档用检索 |
| D14 | 检索器为可插拔接缝 `knowledge_retriever`；生产检索引擎用 LightRAG（不做本地-only 阶段，直接上实战），R0 FAQ 检索作兜底/降级实现 |
| D15 | Embedding 供应商：首选硅基流动 bge-m3，次选智谱、阿里（OpenAI 兼容接口，secret 走环境变量） |
| D16 | 知识语料起步：共建营养 FAQ（50~100 条，存 Bitable 知识表）；客户暂无现成个人文档 |
| D17 | 知识问答同样过 reply_guard 引用模式：回答中的数字必须出现在检索原文中，否则切安全回复 |
| D18 | 交付顺序：P2 网页配置台先行，RAG 随后 |
| D19 | 控制台直接基于 DSH：DSH 上游作为依赖（不 fork 内核），经 profile/patch + 桥接插件接入；会话 UI 用 DSH 原生（Web 直接向 calorie agent 发消息）。接入方式（2026-09-06 客户拍板）：直接实现 `Agent` 接口替换默认驱动，消息直达本地 Python 后端、零中转 LLM；聊天默认 preset=live 写真实表格（与飞书同口径）。桥接插件源码归属本仓库 `console/dsh-plugin/`，经 `dsh plugin` 装入 profile；原 FastAPI 页面降级为桥接后端 + 过渡界面 |
| D20 | RAG（P3）排在 DSH 化控制台完成之后 |
| D21 | DSH 控制台双引擎：插件注册 `/dsh`、`/calorie` 命令（指令菜单可点/可键入），切换仅对之后新建的会话生效，进行中对话不变；默认热量助手引擎；`/dsh` 前提是已在 DSH 设置配置 DeepSeek API key。不做全局设置卡、对话中途热切换、双引擎共享收件箱。实现：驱动是唯一 factory，dsh 模式复用本地 agent-loop 的 ReactLoopAgent（本地仓库加一行导出），resume 按 requestHeader 启发式判引擎 |
| D22 | 薄荷开放平台定位（2026-09-06）：数据与识别源（工具层/数据源），饮食记录事实仍归 Bitable 单一真源。第六组"饮食记录 API"不接——双真源冲突、数据迁出掌控、与既有幂等/撤销/追问闭环重复建设；如需薄荷 App 看数，只做 Bitable→薄荷 单向镜像。体重记录优先自建 Bitable 体重表，薄荷侧体重备选 |
| D23 | 薄荷分阶段接入 → **2026-09-06 收敛：免费版额度有限，只做 M1**。M1 = 食物搜索兜底 + 确认入库：本地 foods 未命中→薄荷搜索→候选需确认→`write_food` 连营养写回 foods 表（加 `source=boohee`、`boohee_id` 列，幂等沿用契约既有的"建食物"公式），承接原"缺食物自动建库"；单位/口径必须归一为每100g+kcal，换算不可靠的条目不缓存；上线后用薄荷数据修正 foods 表异常行。~~M2 拍照识别 / M3 NLU 影子 / P4 食谱·体重·条码~~ 全部不做（额度限制） |
| D24 | 薄荷护栏：`BOOHEE_API_KEY` 只走环境变量（云函数 + 本地桥接后端，绝不进浏览器端/仓库）；`boohee_client` 收在 agent/storage/ 是唯一外呼点（硬规则口径）；仅本地未命中才外呼 + 进程内短 TTL 缓存，确认后永久缓存进 Bitable，调用量随使用衰减；营养数字经 tool_results 进回复照常过 reply_guard，回复标注"数据来源：薄荷健康"；eval 用 mock 传输不依赖外网；注册时确认条款允许结果缓存入自建库；接入交付内同步更新 contracts（foods 加列、Store 接口 write_food、fake_store 同步、幂等公式） |
| D25 | 第二账号接入（2026-09-07，方案 B 同一份数据）：webhook 解析消息后按 `load_user_aliases()` 归一 user_id——Bitable `user_aliases` 表（alias_open_id/canonical_user_id/status，改表即生效无需重部署）为主，env `USER_ID_ALIASES` 兜底。主账号锚定 `ou_ac067b...`（热量agent-v3 应用下的 open_id）。不加行的账号 = 独立数据（天然按 user_id 区分，零配置）。边界：追问确认需在发起会话内完成。不做：身份系统、权限分级、独立数据账号的任何配置（非多用户体系，D8 语义不破） |

## 3. P0 能力清单（本次交付）

| 能力 | 说明 | 验收 |
|---|---|---|
| record_food | 自然语言记录，单/多食物，营养来自食物库计算并写快照 | "鸡胸肉50g"→回复已记录，Bitable 有行 |
| query_today | 今日摄入、剩余目标、进度条 | 数字与 Bitable 表聚合一致 |
| undo_last | 撤销最近一条有效记录（软删除 status=deleted） | 撤销后今日查询不再计入 |
| query_food_nutrition | 先查当日摄入快照，再查食物库 | 数字来自快照/食物库 |
| 缺克重追问 | "米饭"→创建 pending→追问→回答"200g"→自动记录 | pending 表状态流转 pending→completed |
| 重复幂等 | 同一 message_id 重发：不重复写入、不重复回复 | 表内只有一行，client_token 拦截 |
| 食物歧义/未找到 | needs_confirmation 返回候选请用户确认；not_found 进 pending | 不允许模型猜营养 |

## 4. 三预设（替代旧候选体系）

| 预设 | 存储绑定 | 写允许 | 回复发送 | 用途 |
|---|---|---|---|---|
| `live` | bitable_adapter | 是 | 是 | 生产 |
| `shadow_fakewrite` | fake_store | 是（内存） | 否 | 本地/线上演练 |
| `shadow_readonly` | bitable_adapter | 否（只读工具白名单） | 否 | 线上只读影子 |

预设由 `config.yaml` 的 `preset` 选择，代码内禁止按预设写 if 分支（见 AGENTS.md）。

## 5. 配置分层（D9）

- 环境变量（secrets/部署）：`FEISHU_APP_ID`、`FEISHU_APP_SECRET`、`FEISHU_VERIFICATION_TOKEN`、
  `DEEPSEEK_API_KEY`、`BITABLE_APP_TOKEN`、`BITABLE_FOOD_TABLE_ID`、`BITABLE_INTAKE_TABLE_ID`、
  `BITABLE_STANDARD_TABLE_ID`、`BITABLE_PENDING_TABLE_ID`、`BITABLE_USAGE_TABLE_ID`。
- config.yaml（工程）：`preset`、`planner`（deterministic|deepseek）、`model`、
  `allowed_tools`、`timezone_offset_hours`。
- Bitable 标准表（用户数据）：每日目标 kcal/carbs/protein/fat，默认 2230/250/150/70。

## 6. 验收标准

- 本地：`python3 harness/eval/runner.py` 全绿（FakeStore，9 场景，见 cases.yaml）。
- 线上（客户飞书账号）：第 3 节每行验收各过一遍。
- 治理：AGENTS.md ≤80 行；测试套件仅 eval 一套（smoke/full 两档）。

## 7. OUT OF SCOPE（明确不做）

多租户与权限隔离；家人代记拦截；日报/周报/提醒/
rollover 定时任务（P1+）；语义撤销（撤销指定食物/第 N 条，P1+）；修改每日标准对话流
（直接在 Bitable 表改）；feedback/retention 指标体系；群聊协作；商业化功能；
薄荷拍照识别、薄荷 NLU 规划后端、薄荷食谱/体重/条码（D23 收敛，免费额度限制）；
薄荷"饮食记录 API"（D22，双真源）。
（缺食物自动建库已由 D23 M1 薄荷搜索兜底承接。）

## 8. 后续排期与自动推进（2026-09-07 更新，D18-D24）

**推进协议（客户 2026-09-06 拍板）**：批次按序自动推进——每批完成后简短返回交付清单
（改动文件 / eval 通过数 / 验收事实 / 剩余风险）即自动进入下一批；仅需求级歧义或破坏性
变更时暂停对齐。

**下轮工作单（RAG M3 收官 + token 优化，按优先级）**：

1. **[P0] 规划器改"确定性优先、LLM 兜底"**：deterministic 先跑，产出有效意图即用，
   goal=unknown 才调 DeepSeek（现有回落方向装反）。预期 LLM 调用降 80%+、每条消息
   快 1~3s；planner_node 单点改动 + eval 全量回归。
2. **[P0] 增量补跑 LightRAG ingest**：DeepSeek 已充值，只重抽 12 篇失败文档（LLM 缓存
   使已成功 24 篇近零成本）；纪律：真索引禁止 rm -rf，stub 调试一律临时目录。
3. **[P0] Layer 发布与切换**：真索引质量验证 → build_rag_layer.sh + deploy_rag_layer.py
   （Layer 挂载 + RAG_INDEX_DIR=/opt/rag_index + knowledge_retriever=lightrag）→
   UpdateFunctionCode 强刷旧实例。
4. **[P0] 飞书实测 lightrag 检索路径** + M3 交付清单。
5. **[P1] aquery_data 答案 LLM 白烧实测**：确认检索路径不触发答案生成调用，触发则显式关闭。
6. **[P1] foods 表异常数据修正**：米饭（4822.8 kcal/200g）等异常行，用薄荷数据源替换并标注。
7. **[P1] DeepSeek 402 可观测性**：planner 失败写 usage_events.error_type，余额耗尽不再静默降级。
8. **[P2] 远期**：FAQ 语料审校扩充、DSH settings 卡片迁移过渡页、usage 看板、
   密钥治理（腾讯云 key 对轮换停用等）。

已完成批次：RAG M1 ✅、薄荷 M1 ✅、RAG M2 ✅、RAG M3 ✅（2026-09-07 上线：
LightRAG Layer 挂载 + bge-m3 索引 + 检索路径零 LLM token；规划器改确定性优先，
LLM 调用仅剩模糊输入升级场景）。P1 残留（工作单 5/6/7）随 M3 同批完成 ✅。
后续仅 P2 远期池（语料扩充、DSH settings 卡片、usage 看板、密钥治理）。
