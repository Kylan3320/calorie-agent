# Calorie Agent 需求基线

产品范围、业务语义及已确认决策的变更需用户确认；事实勘误和实施细节维护不属于需求变更。

## 产品定位

飞书自然语言饮食记录助手，支持记录、查询、撤销、追问补全、知识问答及历史聚合。
营养事实来自食物库、摄入快照或知识原文，模型负责理解和规划。

业务行为见 [个人功能规格](PERSONAL_CALORIE_SPEC.md) 和 [交流体验规格](CONVERSATION_EXPERIENCE_SPEC.md)，存储与执行要求见 [COS 规格](LATENCY_OPTIMIZATION_SPEC.md)，接口见 [contracts/](contracts/)。验证规则见 [AGENTS.md](AGENTS.md)，当前部署及验收结果见 [交付记录](reports/LATENCY_DELIVERY.md)。

## 有效决策

| 编号 | 决定 |
|---|---|
| D1 | 采用飞书＋COS：COS 保存运行状态及已提交业务事实，机器人据此读取和回复；Bitable 保存长期业务账本，后台按版本归档。 |
| D2 | 部署腾讯云函数，Python 3.10、端口 9000；飞书 webhook 快速 ACK。 |
| D3/D4 | 不做多租户权限体系或家人代记词表拦截；账号别名按 D25。 |
| D5 | 采用基于 LangGraph 的 LangChain create_agent 和原生工具循环；模型统一选择语义行动，移除规则优先及固定全消息计划链路，不自建 checkpoint 后端。 |
| D8/D25 | 以本人使用为目标；同一人多个账号可通过 Bitable user_aliases 归一为主账号，env USER_ID_ALIASES 兜底，表行优先。未映射账号按自身 user_id 使用独立数据；追问在发起会话内完成，不增加权限系统。 |
| D9 | secrets、表 ID、COS 与部署绑定走环境变量；工程配置用 config.yaml。默认标准沿用既有来源并发布为 COS 快照，个人目标由机器人修改。 |
| D10 | 旧 V2/V3 包冻结；新实现归本仓库。 |
| D11 | 三预设：live 通过统一 COS 写入通道处理真实业务，后台归档 Bitable；shadow_fakewrite 使用 FakeStore、不访问真实存储；shadow_readonly 读已提交事实，对 COS、Bitable 和投递均零写入。后两者不发送飞书消息。 |
| D12 | 保留轻量 usage 指标，不做 feedback/retention 体系。 |
| D13/D14 | 历史数据走聚合工具；静态知识走 knowledge_retriever。生产检索引擎用 LightRAG，FAQ 可通过配置显式切换。 |
| D15 | Embedding 首选硅基流动 bge-m3，备选智谱、阿里；密钥走环境变量。 |
| D16 | 生产语料存 Bitable 知识表，起步目标为审校后的 50～100 条 FAQ；seed 用于初始化及评测。 |
| D17 | 模型依据工具事实自然表达，营养计算和业务写入由工具执行，验收核对数值、对象、日期及状态。知识定量结论使用实际依据；检索不足时继续给一般解释，简短指出影响结论的具体缺口；来源取自实际检索资料。默认记餐/改量回执为简短确认＋本餐热量，估算时说明，逐项营养按需展开。详见交流体验规格。 |
| D19 | DSH 上游作为依赖，不 fork 内核；本地 UI 用本仓库驱动直连 Python 后端。默认 live 接入统一业务执行通道，Web 通过 CONSOLE_USER_ID 绑定使用账号。FastAPI 保留配置、调试、轨迹及评测入口。 |
| D21 | 保留 /dsh、/calorie 双引擎；切换仅影响之后新建的会话。DSH 编码引擎需配置 API key，不做对话中途热切换或共享收件箱。 |
| D22/D23 | 薄荷提供搜索、详情及单位数据；语义明确匹配直接使用，不凭结果条数或排名判断无歧义。份量区分可食重和估算，优先保留来源热量；摄入事实经 COS 提交并归档 Bitable，不接薄荷饮食记录存储。 |
| D24 | 薄荷密钥不进浏览器；本地缺营养或份量依据时经 storage 查询并缓存，回复保留来源；权限不足可用已确认个人份量，不能猜换算或自动升级套餐。评测注入传输。 |
| D26 | 暖实例已知食物且无真实歧义的记餐／改量／今日查询，从入口收件至最终发送成功每条≤5秒，为 COS 接入速度发布门槛。知识10秒保留为目标，单列耗时差额，允许账本提速先上线；知识内容和来源正确性仍须通过。冷启动单列，现有DeepSeek型号、思考模式、自然表达及恢复标准继续有效。 |
| D27 | 摄入、个人目标、常用餐及其他个人设置通过机器人修改，Bitable 单向归档。同步频率及接入标准见 COS 规格。 |

## 当前实现能力

原生create_agent循环提供record_meal、revise_intake、query_today、query_food_nutrition、query_knowledge、query_history、calculate_meal、update_user_settings。支持整餐部分完成、按食物可食单位估算、命名常用餐及历史快照按比例复用、指定项纠错/软删除、长期/当日逐字段目标、偏好、份量与可开关提醒。

各入口共用业务运行时；会话、执行和当前业务事实经 COS 保存，Bitable 用于后台归档、来源维护及旧状态迁移。

## 范围外

多租户权限隔离、家人代记拦截、群聊协作、商业化、feedback/retention；
自动日报/周报、批量清空摄入记录；
薄荷饮食记录 API、拍照识别、NLU、食谱、体重和条码接入。

后续候选（未排期）：语料扩充、DSH settings 迁移、usage 看板、密钥治理。
