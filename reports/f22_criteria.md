# F22 验收与测试

每条验收对应一个会真实执行的测试。没有 skip。页面读到的是 API 返回的记录，空列表保持空。

| 验收 | 测试 |
|---|---|
| 生成的 OpenAPI 与合同的 49 个操作一致 | `tests/unit/test_openapi_contract.py::test_generated_openapi_matches_the_contract_operations` |
| 来源、采集、跟踪写入真实行；未索引和草案保持原样 | `tests/integration/test_console_api.py::test_sources_watches_and_ingestions_round_trip` |
| 推荐、反馈和费用来自账本；没有索引时检索是 503 | `tests/integration/test_console_api.py::test_recommendations_feedback_and_costs_read_the_ledger` |
| 技能导入后隔离；评测没有分数；不合格部署不改变版本 | `tests/integration/test_console_api.py::test_skill_import_does_not_score_or_execute` |
| 连接器密钥不可回读；探针不报通过 | `tests/integration/test_console_api.py::test_connector_secret_is_not_readable_and_probes_do_not_pass` |
| 过期审批不出现在待处理列表 | `tests/integration/test_console_api.py::test_expired_approval_stays_out_of_the_pending_list` |
| 控制台有中文长文本、键盘焦点、窄屏、断线，原始 HTML 不执行 | `tests/integration/test_console_browser.py::test_console_shows_real_states_and_does_not_execute_html` |
