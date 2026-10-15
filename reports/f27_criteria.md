# F27 验收与测试

报告数字来自一次真实的 JUnit。live、CodeGraph 产品开关和 Hermes 对照分开列，未运行项不写成通过。

| 验收 | 测试 |
|---|---|
| JUnit 的失败数进入报告；F26 未完成时 live_ready 为 false，未运行项列出 F26、Hermes 和 CodeGraph | `tests/unit/test_acceptance_report.py::test_report_counts_come_from_junit_and_live_stays_not_ready` |
| STATUS 里 live_ready 为 passed 时报告跟着为 true；Hermes 和 CodeGraph 仍是未运行 | `tests/unit/test_acceptance_report.py::test_live_ready_follows_status_and_leaves_hermes_and_codegraph_unrun` |
