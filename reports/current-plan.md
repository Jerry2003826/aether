# F22 复开

目标：任务、记忆、编码作业用控制台同一条接口创建，再从数据库读回同一条记录。第二个所有者不能创建。`/v1/admin/*` 写明单所有者不变量。

合同：`docs/08_api_ui.md`，`docs/06_security.md`。

测试：

```text
AETHER_REQUIRE_SERVICES=1 uv run pytest -q tests/integration/test_console_records.py tests/integration/test_auth.py::test_second_owner_account_cannot_be_created tests/unit/test_openapi_contract.py
```

进度：实现中。状态 in_progress，等 PR CI 和 Sonar 通过后再标 done。

阻塞：无。
