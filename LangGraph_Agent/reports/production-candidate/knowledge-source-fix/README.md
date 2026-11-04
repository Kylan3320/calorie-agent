# 两条主食知识来源修复记录

2026-11-02，米饭／主食两条回答及来源已更新并随 calorie-rag:7 发布，其余34条原文保留。实际生产状态见[交付记录](../../LATENCY_DELIVERY.md)。

修复采用中国营养学会的[八准则原页](https://dg.cnsoc.org/article/04/J4-AsD_DR3OLQMnHG0-jZA.html)、[准则一解读](https://dg.cnsoc.org/article/04/K7tlcs-UQh67DBC5XY1Jqw.html)及[官方工具包](https://www.cnsoc.org/activitykit2/6522002010.html)。核对范围见 [source-evidence.json](source-evidence.json)。

保留的输入与证据：

- knowledge-36-source-patched.json、documents-36-patched.json：完整来源和构建文档。
- bitable-update-plan.json：两条记录的原字段、目标字段及前后哈希。
- index-build.json、retrieval-check.json：完整索引、向量／图来源和实际检索结果。
- layer-artifact.json 及同目录最终 Layer ZIP：版本、依赖内容哈希和发布物。
- source-foods-readonly.json 与对应 audit：当次125条食物来源。

构建、检索检查和冻结依赖打包已合入现有 deploy 脚本，操作见[知识维护与发布](../../../README.md#知识维护与发布)。
