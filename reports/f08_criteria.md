# F08 验收与测试

每条验收对应至少一个会真实执行的测试。没有 skip，也没有空测试。

| 验收 | 测试 |
|---|---|
| UI 连接真实 API，未登录不显示任务数据 | `apps/web/lib/live.test.mjs` |
| 创建任务写入数据库和 Temporal，不返回固定成功 | `tests/integration/test_control.py::test_create_task_reaches_database_and_temporal_without_fake_success` |
| SSE 断线后按 sequence 补齐 | `tests/integration/test_control.py::test_event_replay_shows_pending_command_and_watermark` |
| pending command 与投影水位可见 | `tests/integration/test_control.py::test_event_replay_shows_pending_command_and_watermark` |
| 审批由服务端核对 owner、哈希和版本 | `tests/integration/test_control.py::test_approval_decision_is_authorized_on_the_server` |
| 事件和产物不跨项目泄漏 | `tests/integration/test_control.py::test_events_and_artifacts_stay_inside_the_project` |
| 草案合同不改写已批准合同 | `tests/integration/test_control.py::test_draft_contract_does_not_rewrite_an_approved_contract` |
