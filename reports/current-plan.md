# F20 技能包

目标：按需加载 SKILL.md 的元数据、正文和资源。未批准脚本不执行。trace 只给 train/dev 并脱敏。候选不能自动升权或改控制代码。

合同：`docs/07_learning_evaluation.md`。

测试：`uv run --locked --no-build pytest -q tests/unit/test_skills.py`

进度：本地 4 passed。还没标 done。

阻塞：无。
