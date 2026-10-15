# F17 验收与测试

每条验收对应至少一个会真实执行的测试。没有 skip。客户端使用官方 `mcp` Python SDK 的 stdio 传输，夹具服务器也是这个 SDK 起的。

| 验收 | 测试 |
|---|---|
| stdio 使用 argv 列表，不拼 shell；用户内容不能安装新 server | `tests/unit/test_mcp.py::test_argv_is_not_a_shell_and_user_text_cannot_install` |
| schema 漂移进入隔离；annotations 不能给自己授权 | `tests/unit/test_mcp.py::test_fixture_echo_schema_and_secret_redaction` |
| 远端 readOnlyHint、destructiveHint=false、idempotentHint 不放宽本地登记的风险和审批 | `tests/unit/test_mcp.py::test_annotations_do_not_relax_configured_permissions` |
| 真正连上 fixture MCP，回显并脱敏密钥，密钥不进入 server 环境 | `tests/unit/test_mcp.py::test_fixture_echo_schema_and_secret_redaction` |
| 重连使用新进程 | `tests/unit/test_mcp.py::test_reconnect_uses_a_new_process` |
| 超时记为 unknown | `tests/unit/test_mcp.py::test_fixture_timeout_is_unknown` |
| 服务进程中途退出时，这次和之后的调用都立刻是 unknown | `tests/unit/test_mcp.py::test_crashed_server_returns_unknown_and_later_calls_do_not_hang` |
| 服务器不存在或立刻退出时，连接失败是 `connect_failed` | `tests/unit/test_mcp.py::test_missing_or_exiting_server_raises_connect_failed` |
