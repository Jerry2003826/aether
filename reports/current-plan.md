# F22 控制台与 API

目标：49 个设计操作都有真实路由。列表读数据库，变更先入命令再由 worker 或 verifier 落库。页面走这些 API。原始 HTML 不执行。

合同：`docs/08_api_ui.md`，`contracts/openapi.yaml`，`contracts/endpoint_stages.yaml`。

测试：

```text
uv run pytest -q tests/unit/test_openapi_contract.py
AETHER_REQUIRE_SERVICES=1 uv run pytest -q tests/integration/test_console_api.py tests/integration/test_console_browser.py
```

进度：本地浏览器用例已通过。状态 in_progress，等 PR CI 和 Sonar 通过后再标 done。

阻塞：检索在没有绑定索引进程时返回 503，不编造命中。模型探针在 live 未运行时是 unavailable。
