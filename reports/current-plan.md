# F11 真实混合检索

目标：BM25、pgvector、BGE、RRF/rerank 的分组对照。授权过滤在检索前。索引用原子切换。

合同：`docs/05_knowledge.md`。

进度：blocked。`reports/f01-probe.json` 里 `dimension_probed` 仍是 null。不能用随机向量充当 BGE。

恢复：加载 BAAI/bge-m3 权重，记下实测维度和 revision，再做 F11。

F15 仍在 F14 之后，不从计划里去掉。
