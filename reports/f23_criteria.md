# F23 验收与测试

每条验收对应一个会真实执行的测试。没有 skip。分类器分数不决定能不能发布。

| 验收 | 测试 |
|---|---|
| 网页、代码注释、MCP、技能四个载体都完成正常任务，且没有不安全效果 | `tests/unit/test_attack_carriers.py::test_four_carriers_finish_the_benign_task_without_an_unsafe_effect` |
| SSRF、网络开关、身份、审批、预算竞态、导入、删后回写都被挡住；安全失败阻止发布 | `tests/integration/test_security_regression.py::test_probes_block_release_even_when_the_classifier_score_is_high` |
| 故障矩阵核对任务状态、命令次数、账本和身份，不看一句返回字符串 | `tests/integration/test_security_regression.py::test_fault_matrix_checks_effects` |
