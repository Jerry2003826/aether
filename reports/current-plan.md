# F24–F27

目标：Hermes 对照驱动、SBOM 和恢复、live 探针、从 JUnit 生成的交付报告。

缺 `AETHER_LIVE_MODEL_KEY`、`AETHER_LIVE_MODEL_BASE` 或 `AETHER_LIVE_MODEL_ALIAS` 时 live 记 blocked。没有 Hermes 运行结果时不写领先。

测试：

```text
uv run pytest -q tests/unit/test_hermes_baseline.py tests/unit/test_supply_chain.py tests/unit/test_live_harness.py tests/unit/test_acceptance_report.py
AETHER_REQUIRE_SERVICES=1 uv run pytest -q tests/integration/test_backup_restore.py
```

进度：本地这些测试已通过。状态 in_progress。推送被过期的 GitHub 安装令牌挡住，所以还不能用 PR CI 标 done。

阻塞：`GH_TOKEN` 在 2026-10-15T06:19:56Z 过期。
