# F13 验收与测试

每条验收对应至少一个会真实执行的测试。没有 skip。PR #1 的 CI 在带 Docker 的 GitHub runner 上跑过容器：同一个 job 只有一个容器，重启后重新挂上；容器内公网、云元数据和 Docker socket 不可达，密钥不在环境里，只能访问该 job 的脚本网关。OpenHands 只构造了指向网关的 LLM，没有 live 模型调用。

| 验收 | 测试 |
|---|---|
| job 调用走网关 token，不带 provider 密钥 | `tests/integration/test_runner.py::test_job_call_uses_the_gateway_token` |
| 启动计划不挂 Docker socket，依赖不在 job 里现装 | `tests/integration/test_runner.py::test_openhands_sdk_points_at_the_gateway_without_the_provider_key` |
| OpenHands SDK 的 base_url 是网关，输出不含 provider 密钥 | `tests/integration/test_runner.py::test_openhands_sdk_points_at_the_gateway_without_the_provider_key` |
| start 幂等，重启后 attach 同一个 job，不第二次启动 | `tests/integration/test_runner.py::test_start_is_idempotent_and_restart_attaches_without_the_provider_key` |
| 取消不把外部效果说成已经停止 | `tests/integration/test_runner.py::test_start_is_idempotent_and_restart_attaches_without_the_provider_key` |
| Linux 网络、挂载和 pid namespace 实测；测不到不标支持 | `tests/integration/test_runner.py::test_linux_namespace_probe_is_measured_or_not_claimed` |
| 同一个 job 只创建一个容器，重启后重新挂上；容器内不能访问公网、元数据或 Docker socket，也拿不到 provider/admin 密钥，只能访问该 job 的脚本网关 | `tests/integration/test_runner.py::test_container_is_single_isolated_and_reattaches` |
