# F20 技能包复开

目标：密钥整段替换；脚本按整个 `scripts/` 目录哈希，核对后的字节进 F13 容器，用 `python -I` 和白名单环境执行；路径先归一化再拒绝可信控制代码。

合同：`docs/07_learning_evaluation.md`。

测试：`uv run pytest -q tests/unit/test_skills.py tests/integration/test_skill_sandbox.py`

进度：本地 8 passed。状态 in_progress，等 PR CI 和 Sonar 通过后再标 done。

阻塞：无。
