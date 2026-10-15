# F14 验收与测试

每条验收对应至少一个会真实执行的测试。没有 skip。live 模型演示不在这项里。

| 验收 | 测试 |
|---|---|
| 套件、合同、候选哈希绑定；改码后旧的通过失效 | `tests/integration/test_verification.py::test_changed_candidate_invalidates_the_previous_pass` |
| 候选用 `def test_x(): pass` 替换测试不能通过 | `tests/integration/test_verification.py::test_candidate_vacuous_tests_fail` |
| 候选 conftest 不能改写验收结果 | `tests/integration/test_verification.py::test_candidate_conftest_cannot_rewrite_the_report` |
| 套件或合同哈希不一致则失败 | `tests/integration/test_verification.py::test_suite_or_contract_hash_mismatch_fails` |
| 评分进程读不到宿主文件和密钥 | `tests/integration/test_verification.py::test_grader_cannot_read_host_files_or_credentials` |
| actor 不能把失败改成通过 | `tests/integration/test_verification.py::test_changed_candidate_invalidates_the_previous_pass` |
| actor 不能写 verification_results | `tests/integration/test_verification.py::test_actor_cannot_write_verification_results` |
| 空套件或全部 skip 不能通过 | `tests/integration/test_verification.py::test_empty_or_skipped_suite_does_not_pass` |
| 断 worker、断 runner、取消都不会留下通过 | `tests/integration/test_verification.py::test_worker_death_runner_death_and_cancel_do_not_pass` |
| 测试运行产物必须存在、属于本项目、且种类是 test_run | `tests/integration/test_memory.py::test_observed_test_requires_a_project_test_run_artifact` |
