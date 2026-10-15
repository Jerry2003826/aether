# 审计一轮修复

基线 `b570f9197c650212a693f34a18d3729a04772f0d`。修 D01–D04 和 C01，不改 STATUS，不放宽已有测试。

- D01：上传指纹对原始字节做 SHA-256。非法 UTF-8 文本仍由媒体检查返回 422。
- D02/D03：数据库提交是产物日志的权威。尝试 id 和规范 id 分开。未知结果对账，回滚和 tombstone 不重新公开。
- D04：归档先解到临时目录，校验后改名发布。完成标记在 `snapshot-meta/`。
- C01：`python -m aether_worker` 轮询现有 outbox，任务命令进 Temporal，其他命令走已有应用函数。

回归：`tests/integration/test_audit_artifact_regressions.py`、`tests/integration/test_audit_artifact_durability.py`、`tests/integration/test_worker_process.py`。
