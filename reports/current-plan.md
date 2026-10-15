# F15 CodeGraph

目标：接入固定的 CodeGraph 1.6.2 CLI。产品开关保持关闭。快照、失效、取消、降级和断网都要实测。

合同：`docs/13_codegraph.md`，`contracts/codegraph.schema.json`，`config/codegraph.lock.json`。

拟改：`services/worker/aether_worker/codegraph_adapter.py`，`tests/integration/test_codegraph.py`，`config/codegraph.lock.json`，`.github/workflows/ci.yml`。

测试：`uv run --locked --no-build pytest -q tests/integration/test_codegraph.py`

进度：适配器已按锁定的 sha512 安装，并在无网络命名空间里调用 CLI。

阻塞：无。live 模型不在这项里。
