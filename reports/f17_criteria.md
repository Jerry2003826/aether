# F17 验收与测试

官方 MCP SDK 还没有接上，所以 F17 不能标 done。下面这些测试已经在本地夹具上执行，留作后续 SDK 客户端必须继续通过的行为。

| 验收 | 测试 | 状态 |
|---|---|---|
| stdio argv 不用 shell 拼接 | `tests/unit/test_mcp.py::test_argv_is_not_a_shell_and_user_text_cannot_install` | 夹具通过，任务未完成 |
| 用户内容不能安装新 server | `tests/unit/test_mcp.py::test_argv_is_not_a_shell_and_user_text_cannot_install` | 夹具通过，任务未完成 |
| schema 漂移隔离 | `tests/unit/test_mcp.py::test_fixture_echo_schema_and_secret_redaction` | 夹具通过，任务未完成 |
| annotations 不能自行授权 | `tests/unit/test_mcp.py::test_fixture_echo_schema_and_secret_redaction` | 夹具通过，任务未完成 |
| 连接本地 fixture | `tests/unit/test_mcp.py::test_fixture_echo_schema_and_secret_redaction` | 夹具通过，任务未完成 |
| 超时记为 unknown | `tests/unit/test_mcp.py::test_fixture_timeout_is_unknown` | 夹具通过，任务未完成 |
| 重连使用新进程 | `tests/unit/test_mcp.py::test_reconnect_uses_a_new_process` | 夹具通过，任务未完成 |
| secret 不留在结果里 | `tests/unit/test_mcp.py::test_fixture_echo_schema_and_secret_redaction` | 夹具通过，任务未完成 |
