# F21 回滚原子性

目标：回滚中途失败时，当前版本指针和 canary 状态要么一起变，要么都不提交。

合同：`docs/07_learning_evaluation.md`。

测试：`AETHER_REQUIRE_SERVICES=1 uv run pytest -q tests/integration/test_skill_eval.py::test_rollback_is_atomic_when_the_second_write_fails`

进度：本地与原有 canary 用例一起 2 passed，整文件 4 passed。状态 in_progress，等 PR CI 和 Sonar 通过后再标 done。

阻塞：无。
