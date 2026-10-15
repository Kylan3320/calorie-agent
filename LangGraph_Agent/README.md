# Calorie Agent V4（LangGraph 重构版）

单用户飞书饮食记录 Agent：自然语言记录 / 今日查询 / 撤销 / 缺克重追问补全，
存储用飞书多维表格（Bitable），部署在腾讯云函数。治理与需求见 [AGENTS.md](AGENTS.md)
与 [REQUIREMENTS.md](REQUIREMENTS.md)，表结构与幂等键见 [contracts/](contracts/)。

## 本地开发

```bash
python3.12 -m venv .venv && ./.venv/bin/pip install -r requirements.txt

# 零配置冒烟（FakeStore 内存存储，不联网）
./.venv/bin/python cli.py "米饭200g"
./.venv/bin/python harness/eval/runner.py --smoke   # 冒烟评测
./.venv/bin/python harness/eval/runner.py           # 全量评测（10 场景）
./.venv/bin/python harness/shadow.py                # 影子双跑对比（fakewrite vs readonly）
```

改动代码前先跑一次基线评测，交付前跑全量（见 AGENTS.md Worker loop）。

## 本地控制台（DSH 化，D19）

生产入口 = DeepSeek Harness Web UI + `calorie-agent-driver` 桥接插件（直接实现 Agent
接口替换默认驱动，消息直达本地 Python 后端，零中转 LLM）：

```bash
# 1) Python 桥接后端（/api/agent，含 config/playground/trace/eval 过渡页面）
./.venv/bin/python -m console.server          # http://127.0.0.1:8765

# 2) DSH web（依赖本地 DSH 仓库 ~/Documents/DSH 已 pnpm install + build）
export DSH_HOME=$PWD/console/dsh-home
cd ~/Documents/DSH && pnpm dsh --profile calorie   # 输出带 token 的 URL，浏览器打开
```

- 插件源码：`console/dsh-plugin/calorie-agent-driver/`（index.js + cordis.patch.yml +
  setup-links.sh，符号链接到 DSH 工作区构建产物保证类同一性）。
- profile：`console/dsh-home/profiles/calorie/`（bundles = dsh-base + dsh-web-app +
  calorie-agent-driver；patch 禁用默认 agent-loop 行并插入本驱动，webserver 端口 3100）。
- **双引擎**：默认热量助手（Python 桥接）；指令菜单或输入框 `/dsh` 切到 DSH 编码助手
  （需已在设置页配置 DeepSeek API key）、`/calorie` 切回。切换仅对之后新建的会话生效，
  进行中对话不变。dsh 引擎复用本地 agent-loop 的 ReactLoopAgent（本地仓库加了一行
  `export { ReactLoopAgent }`，重建 lib 后仍在）。
- 聊天默认 preset=live 写真实多维表格（与飞书同口径）；改口径编辑 bundle patch 的
  `preset` 后重启（patchReload 支持热改）。
- 过渡页面（配置编辑/Playground/轨迹/评测按钮）仍由 console.server 提供，逐步迁入
  DSH 原生 settings 卡片。

## 部署（腾讯云函数）

```bash
USE_DOCKER=1 ./deploy/build_zip.sh   # 推荐：python:3.10-slim 内装依赖，ABI 与 SCF 一致
./deploy/build_zip.sh                # 或本机 python3（pydantic-core ABI 可能不匹配，见脚本内警告）
```

云函数配置：Python 3.10 / Web 函数 / 端口 9000 / bootstrap 文件 `scf_bootstrap`，
上传 `deploy/calorie-agent-v4.zip`。环境变量：

```env
FEISHU_APP_ID=            FEISHU_APP_SECRET=       FEISHU_VERIFICATION_TOKEN=
BITABLE_APP_TOKEN=        BITABLE_FOOD_TABLE_ID=   BITABLE_INTAKE_TABLE_ID=
BITABLE_STANDARD_TABLE_ID= BITABLE_PENDING_TABLE_ID= BITABLE_USAGE_TABLE_ID=
BITABLE_KNOWLEDGE_TABLE_ID= BITABLE_ALIASES_TABLE_ID= BOOHEE_API_KEY=   PRESET=live
# 可选：同一人多账号别名兜底（Bitable user_aliases 表为主，env 覆盖场景用）
# USER_ID_ALIASES=别名open_id:主账号open_id
```

多维表格沿用 V2 已有的表即可（列名见 contracts/bitable_schema.md），
`PRESET` 环境变量可不重打包直接切换预设做灰度演练。

**部署坑：SCF 旧实例惰性回收**。`UpdateFunctionConfiguration` 更新环境变量后，
旧实例可能继续服务数分钟——新代码读到旧 env（踩过两次：知识表 ID、薄荷 key）。
部署后必须重放一条真实消息并用落表事实（usage/foods/intake 行）验证，出现旧行为
就再提交一次 `UpdateFunctionCode` 强制全量滚动。另：大 base64 上传（30MB+）在
客户端 120s 会超时但服务端可能已生效，`api_deploy.py` 超时已提至 300s。

## RAG 知识检索（Layer 部署，M3）

生产默认 `knowledge_retriever: lightrag`（Layer 挂载 LightRAG 依赖 + 预构建索引；
`faq` 字面匹配作为配置可切的兜底实现）。语料变更后的更新流程：

```bash
./.venv/bin/python deploy/ingest_knowledge.py        # 重建索引（幂等，LLM 有缓存）
./deploy/build_rag_layer.sh                          # 依赖+索引 打成 Layer（精简 ~35MB）
TENCENTCLOUD_SECRET_ID=... TENCENTCLOUD_SECRET_KEY=... \
  .venv/bin/python deploy/deploy_rag_layer.py deploy/rag-layer.zip   # 发布 vN 并挂载
# 再重部署一次主函数 zip 强刷旧实例
```

Layer 要点（踩坑实录）：腾讯 SCF 的 `PYTHONPATH=/var/user:/opt`——依赖必须在 zip
**根目录**（AWS 式 python/ 子目录无效）；lightrag 包内部硬引用 lightrag/parser 不可删
（api/pandas/google 可删）；/opt 只读而 LightRAG 查询后会 flush——运行时自动镜像到
/tmp（INDEX_VERSION 标记变化时重拷）；tiktoken BPE 词表预烘进 Layer；检索走
aquery_data 原文证据，查询路径零 LLM token。

## 目录

```text
agent/            产品链路（graph + nodes + planners + tools + policy + storage）
harness/eval/     评测集与 runner（唯一测试套件）
harness/shadow.py 影子双跑对比
console/           本地网页配置台（127.0.0.1:8765，不进云函数包）
deploy/           打包脚本
reports/          trace JSONL 与影子对比报告输出
```
