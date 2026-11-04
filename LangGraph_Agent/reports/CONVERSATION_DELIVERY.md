# 交流体验验收记录（2026-11-03）

本记录保存两次隔离候选及对应验证。当前部署和未解决项统一见 [生产交付记录](LATENCY_DELIVERY.md)。

## 事实回执与固定等待预算候选

构建 cos-production-8794be0e62e0015b，业务包SHA256 3eeac2319b2d3e7df6d5e564061d89862fec637379959aa3bb4e26d960bca30c，复用冻结Layer7。候选来源为完整125食物，SHA256 457c1ff3da5a9c14447a6a553dae1ac1b54e8b26ecc837359268a929c8f77f19；独立用户／前缀 facts-20261103-210440。三个候选函数实际下载代码匹配。

四个账本工具的整批完成声明、直接事实回执、消息固定90秒／步骤40秒预算、最多一次模型传输重试、未知提交核对及同UUID交付恢复已实现。数字按冻结来源用Decimal ROUND_HALF_UP独立核对。

| 请求 | 事实结果 | ACK ms | T_service ms | 主模型HTTP ms | 判定 |
|---|---|---:|---:|---:|---|
| 米饭100g＋鸡蛋100g | 本餐255kcal，两项营养正确 | 2317.928 | 7898.710 | 1427.891 | 冷暖未定，缺暖样本 |
| 米饭改50g | 原午餐197kcal，两项营养正确 | 1360.802 | 5107.432 | 1737.907 | 同worker暖，超过5秒 |
| 今日合计 | 2项、197kcal | 1843.592 | 4940.651 | 1176.262 | 同worker暖，通过 |

三条均1次主决策、1工具、reply_source=tool_facts、model_reply为空，未发生模型重试；候选整体未通过。改量含COS37写／25读／4次LIST、8次execution_save合计715.940ms，持久化链路是剩余优化方向；嵌套耗时不重复相加。两行归档独立读回完整营养及版本，提交至确认187899.379ms。详见 [facts-candidate-evidence.json](facts-candidate-evidence.json)。

### 验证证据

| 范围 | 结果与材料 |
|---|---|
| 定向离线 | 53项组合52通过，CX06旧夹具失败；修正后CX06/FN02/P05通过，另补CB07通过，最终54个不同用例有通过证据。原日志：facts-offline.log、facts-final-recheck.log、facts-legacy-check.log。 |
| 恢复与截止 | 独立审查发现的RPC墙钟、终态来源、最终正文digest及展示绑定问题已修复；RP01–05/ST01另6项通过。 |
| Linux真实传输 | Python3.10.22 loopback在主／工作线程覆盖Feishu和COS阻塞头、停滞body、持续保活，单次截止0.2秒。见 [facts-rpc-wallclock.json](facts-rpc-wallclock.json)。 |
| 实际装配 | 最终ZIP＋冻结Layer隔离加载Agent、LightRAG、COS、编码资源及health通过，42个运行时文件匹配。见 [包检查](facts-package-check.json)、[源码检查](facts-source-check.json)。 |

### 候选清理

三临时函数及URL／触发已删除；精确COS前缀315对象、两条固定Bitable行删除后读回0，原8行逐ID／哈希保持。三条机器人回复DELETE均code0，独立GET受既有应用权限限制。云端证据在facts-candidate-evidence.json；本机清理及剩余临时文件见 [facts-local-cleanup.json](facts-local-cleanup.json)。

## 上下文治理候选（2026-11-03）

构建 cos-production-a0c20e95eb79b5cc，ZIP SHA256 cf05b8597183514b25320494b5a31badb5152c8529d8072a39de9fb259ebe064，复用Layer7。该候选完成清洁对话来源、完整wire计量、动态状态、历史查询分级及后台摘要控制事件，但云端速度未通过；这些实现随后随事实回执构建发布。

| 范围 | 结果 |
|---|---|
| 定向离线 | E01–E06、CX01–CX09及相关U/LT/PS/P/ST场景有通过证据。组合27/28时P04出现空断言，随后独立及相同前置序列通过，原失败保留于context-offline.log，复验见context-p04-recheck.log。 |
| 真实记忆 | 首次摘要20.162秒失败；定向复验2.546秒，两个分支均保留周四纠正、鱼缺数量，当前合计工具查得0，各2次主决策。见 [context-memory-evidence.json](context-memory-evidence.json)。 |
| Linux包与墙钟 | 实际ZIP／Layer装配通过；50ms截止实测50.193ms中断sleep。见 [包检查](context-package-check.json)、[墙钟检查](context-wallclock-check.json)。 |
| 记餐／改量 | 事实255→197kcal正确，均1工具／2主决策；T_service60976.144／26377.773ms，主要耗在模型HTTP。初始二进制round夹具错误经独立Decimal复核修正，原失败保留。 |
| 今日查询 | DeepSeek503，经当时3次自动尝试后交付异常回执，T_service166899.233ms，未判成功。 |
| 后台及归档 | 摘要首次503保留retry，归档读取超时时后台跳过摘要；两行最终确认版本2／1，提交至确认约266／540秒。 |

完整云端结果见 [context-candidate-evidence.json](context-candidate-evidence.json)。三临时函数、339个COS对象、两条候选行及三条机器人测试回复均已清理，详见该证据与 [本机清理](context-local-cleanup.json)。

当次绑定api.deepseek.com、deepseek-v4-flash请求名，窗口1048576；主输出8192、摘要max_tokens4096／正文1024、协议余量2048、近期2轮／触发4000。编码身份见../agent/encoding/version.json。摘要本地4690／供应商4653，业务请求供应商比本地多539–600tokens。
