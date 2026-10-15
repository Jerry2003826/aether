# F20 验收与测试

每条验收对应一个会真实执行的测试。没有 skip。未批准的脚本不会被启动。

| 验收 | 测试 |
|---|---|
| 元数据、正文、资源按需加载 | `tests/unit/test_skills.py::test_metadata_body_and_resources_load_on_demand` |
| 未审批脚本不执行 | `tests/unit/test_skills.py::test_unapproved_script_does_not_execute`；`tests/integration/test_skill_sandbox.py::test_unapproved_script_does_not_execute` |
| 密钥整段替换，raw secret 不留在输出里 | `tests/unit/test_skills.py::test_trace_replaces_the_whole_secret_token` |
| 同目录被改过的模块不被执行 | `tests/integration/test_skill_sandbox.py::test_tampered_sibling_is_rejected` |
| 宿主密钥和环境变量对脚本不可见 | `tests/integration/test_skill_sandbox.py::test_host_secrets_are_not_visible` |
| 候选不能改可信控制代码 | `tests/unit/test_skills.py::test_candidate_cannot_auto_promote_or_edit_control_code`；`tests/integration/test_skill_sandbox.py::test_candidate_cannot_modify_trusted_control_code` |
