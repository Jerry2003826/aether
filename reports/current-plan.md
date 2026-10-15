# F17 MCP

目标：用官方 `mcp` Python SDK 连接本地夹具服务器。argv 不经过 shell。schema 漂移隔离。annotations 不能授权。

合同：`docs/06_security.md`，`config/tools.example.yaml`。

测试：`uv run --locked --no-build pytest -q tests/unit/test_mcp.py`

进度：本地 4 passed。还没标 done。

阻塞：无。
