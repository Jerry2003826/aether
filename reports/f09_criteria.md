# F09 验收与测试

每条验收对应至少一个会真实执行的测试。没有 skip，也没有空测试。模型调用是脚本上游，不是 live 模型。

| 验收 | 测试 |
|---|---|
| 真实存储、本地工具和脚本网关，并记录哈希与调用次数 | `tests/integration/test_kernel.py::test_scripted_loop_records_hash_and_gateway_calls` |
| 独立 verifier 写验收结果，忽略 actor 自报结论 | `tests/integration/test_kernel.py::test_scripted_loop_records_hash_and_gateway_calls` |
| worker 重启后命令重投不重复研究调用 | `tests/integration/test_kernel.py::test_worker_restart_and_command_redelivery_do_not_repeat` |
| 命令重投不重复副作用 | `tests/integration/test_kernel.py::test_worker_restart_and_command_redelivery_do_not_repeat` |
| 审批重放不重复外部调用 | `tests/integration/test_kernel.py::test_approval_replay_does_not_repeat_side_effect` |
| 过期候选被独立验收拒绝 | `tests/integration/test_kernel.py::test_expired_candidate_is_rejected` |
