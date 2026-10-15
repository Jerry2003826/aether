# F23 安全回归

目标：四个载体既完成正常任务，也记录实际不安全效果。安全探针失败就阻止发布。分类器分数不参与这个决定。

合同：`docs/06_security.md`，`docs/14_BOUNDARIES_AND_FAILURES.md`。

测试：

```text
uv run pytest -q tests/unit/test_attack_carriers.py
AETHER_REQUIRE_SERVICES=1 uv run pytest -q tests/integration/test_security_regression.py
```

进度：本地 3 passed。状态 in_progress。

阻塞：GitHub 安装令牌在 2026-10-15T06:19:56Z 过期，推送还没成功。
