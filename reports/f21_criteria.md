# F21 验收与测试

每条验收对应一个会真实执行的测试。没有 skip。合成对照不记成学会了。

| 验收 | 测试 |
|---|---|
| grader 身份才能写分数；开发评测不能使用 holdout | `tests/integration/test_skill_eval.py::test_actor_cannot_write_scores_and_holdout_stays_out` |
| 差候选拒绝，证据不足不升级；合成对照显著标注且不升级 | `tests/integration/test_skill_eval.py::test_weak_or_synthetic_candidates_are_not_promoted` |
| 只有 owner 能批准 canary；任务钉住技能版本；回滚不改已钉版本 | `tests/integration/test_skill_eval.py::test_only_owner_can_canary_and_rollback_keeps_the_pin` |
