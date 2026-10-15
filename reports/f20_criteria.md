# F20 验收与测试

每条验收对应一个会真实执行的测试。没有 skip。未批准的脚本不会被启动。

| 验收 | 测试 |
|---|---|
| 元数据、正文、资源按需加载 | `tests/unit/test_skills.py::test_metadata_body_and_resources_load_on_demand` |
| 未审批脚本不执行 | `tests/unit/test_skills.py::test_unapproved_script_does_not_execute` |
| source trace 脱敏，且只能读 train/dev，不能读 holdout | `tests/unit/test_skills.py::test_trace_is_redacted_and_holdout_is_unreadable` |
| 候选不能请求自动升权，也不能改可信控制代码 | `tests/unit/test_skills.py::test_candidate_cannot_auto_promote_or_edit_control_code` |
