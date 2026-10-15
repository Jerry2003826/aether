# F27 验收与测试

报告数字来自一次真实的 JUnit。live、CodeGraph 产品开关和 Hermes 对照分开列，未运行项不写成通过。

| 验收 | 测试 |
|---|---|
| JUnit 的失败数进入报告；live_ready 为 false；未运行项列出 F26、Hermes 和 CodeGraph | `tests/unit/test_acceptance_report.py::test_report_counts_come_from_junit_and_live_stays_not_ready` |
