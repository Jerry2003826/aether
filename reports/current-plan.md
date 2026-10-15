# F07 Temporal 唯一任务循环与命令发件箱

目标：TaskWorkflow、Activity、command dispatcher、typed DAG。任务和命令同一事务提交。Workflow 不做外部 I/O。actor 不能直接 mark_success。32 节点长链可通过，环和 33 节点拒绝。

合同：`docs/04_execution.md`，`command_outbox`。

拟改路径：`services/worker/aether_worker/`、`tests/integration/test_workflow.py`。

测试：`AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/integration/test_workflow.py`

进度：未开始。

阻塞：无。Temporal dev server 已在本机 127.0.0.1:7233。
