# F09 首个纵向闭环与内核故障验收

目标：本地资料进入研究候选，并由独立验收拒绝过期候选。覆盖 worker 重启、命令重投和审批重放。

合同：`docs/11_delivery.md`，`docs/14_BOUNDARIES_AND_FAILURES.md`。

拟改路径：`services/worker/aether_worker/`、`tests/integration/test_kernel.py`。

测试：`AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/unit tests/integration`

进度：F08 已完成。F09 未开始。

阻塞：无。F01 仍 blocked。
