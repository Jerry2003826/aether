# F10 授权采集、解析、来源与文本版本

目标：HTTP/RSS/本地文档进入不可变 SourceVersion 和 Chunk。空正文失败。SSRF 约束实际连接。解析器有资源上限。

合同：`docs/05_knowledge.md`，`docs/06_security.md`。

拟改路径：`services/worker/aether_worker/ingest.py`、`tests/integration/test_ingest.py`。

测试：`AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/unit tests/integration`

进度：未开始。

阻塞：无。F01 仍 blocked，不使用随机向量。
