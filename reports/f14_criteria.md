# F14 验收与测试

每条验收对应至少一个会真实执行的测试。没有 skip。live 模型演示不在这项里。

| 验收 | 测试 |
|---|---|
| 套件、合同、候选哈希绑定；改码后旧的通过失效 | `tests/integration/test_verification.py::test_changed_candidate_invalidates_the_previous_pass` |
| actor 不能把失败改成通过 | `tests/integration/test_verification.py::test_changed_candidate_invalidates_the_previous_pass` |
| actor 不能写 verification_results | `tests/integration/test_verification.py::test_actor_cannot_write_verification_results` |
| 空测试、全部 skip、伪造 JUnit 不能直接通过 | `tests/integration/test_verification.py::test_empty_skipped_and_forged_junit_do_not_pass` |
| 断 worker、断 runner、取消都不会留下通过 | `tests/integration/test_verification.py::test_worker_death_runner_death_and_cancel_do_not_pass` |
