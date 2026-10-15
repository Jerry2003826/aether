# F24 验收与测试

每条验收对应一个会真实执行的测试。没有 skip。没有 Hermes 运行结果时不写领先。

| 验收 | 测试 |
|---|---|
| 按 `config/hermes.lock.json` 安装 Hermes，探针输出真实的 `Hermes Agent v0.22.1` 和提交前缀 | `tests/integration/test_hermes_probe.py::test_pinned_hermes_cli_prints_its_real_version` |
| 没有 Hermes CLI 时探针是 not_run，不编版本号 | `tests/unit/test_hermes_baseline.py::test_missing_hermes_cli_is_not_a_version` |
| 开发集和 holdout 分开；缺 `AETHER_LIVE_MODEL_KEY` 时 live 是 not_run；unknown 费用不从均值里删掉 | `tests/unit/test_hermes_baseline.py::test_plan_keeps_dev_and_holdout_apart_and_does_not_claim_a_winner` |
