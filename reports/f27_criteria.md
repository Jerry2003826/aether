# F27 验收与测试

报告数字来自一次真实的 JUnit。live、CodeGraph 产品开关和 Hermes 对照分开列，未运行项不写成通过。

| 验收 | 测试 |
|---|---|
| JUnit 的失败数进入报告；F26 未完成时 live_ready 为 false，未运行项列出 F26、Hermes 和 CodeGraph | `tests/unit/test_acceptance_report.py::test_report_counts_come_from_junit_and_live_stays_not_ready` |
| 声明了测试却没有用例，或没有绑定当前 commit、环境、模式、配置和产物哈希时，live_ready 不为 true；Hermes 和 CodeGraph 仍按各自字段列为未运行 | `tests/unit/test_acceptance_report.py::test_live_ready_follows_status_and_leaves_hermes_and_codegraph_unrun` |
