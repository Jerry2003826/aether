# F26 验收与测试

没有 key 就记 blocked。脚本模型不代替这次 live 验收。`live_ready` 保持 false。F26 在 `STATUS.json` 和 `tasks/backlog.yaml` 里是 `blocked`，不是 `done`。`blocked` 和 `not_run` 都不是通过。

监督者已在 head `5ab4fef` 用 DeepSeek 跑过一次：`AETHER_LIVE_MODEL_BASE=https://api.deepseek.com/v1`，`AETHER_LIVE_MODEL_ALIAS=deepseek-chat`，Python 3.13.5，退出码 0。原样结果在 `reports/f26_live_result.json`。文件里没有 key。

这次通过的是 `protocol.tools`、`protocol.schema`、`protocol.usage`、`research.citation`、`coding.independent_test`。`coding.recovery` 是 not_run（第一次编码就过了）。`hybrid_retrieval` 和 `linux_isolation` 是 not_run。规格里的 F26 还要完整 hybrid 检索加引用、编码恢复，以及 Linux 隔离证据。这些没跑，所以 F26 继续 blocked，`live_ready` 保持 false。

选中的供应商是 DeepSeek。key 只留在监督者机器上，不进虚拟机，不进 GitHub secrets，不进日志。

```sh
export AETHER_LIVE_MODEL_BASE=https://api.deepseek.com/v1
export AETHER_LIVE_MODEL_ALIAS=deepseek-chat
export AETHER_LIVE_MODEL_KEY=...
python scripts/run_f26_live.py --out reports/f26_live_result.json
```

`--out` 只接受相对路径 `reports/f26_live_result.json`。退出码：`probed` 是 0，`failed` 是 1，`blocked` 是 2。`probed` 只表示这次模型探针的必查项通过，不是 live V1 已验收。

不需要额外 pip 包。脚本和 `aether_worker.live_harness` 只用 Python 标准库，Python 3.11 及以上即可。仓库里已经 `uv sync` 时也可以：

```sh
uv run --locked --no-build python scripts/run_f26_live.py --out reports/f26_live_result.json
```

探针最多 3 次 `POST /chat/completions`，每次 `max_tokens` 128，累计 token 达到 2000 就停。费用字段只有供应商返回时才写入；没有就记 `cost_status=unknown`，不估一个价钱。结果里的密钥会换成 `[REDACTED]`。

这次探针做的是协议、引用和研究用的工具调用，以及一个独立的 `add` 函数形状检查。混合检索和 Linux 隔离仍是 `not_run`。编码第一次不合格时才会再花一次调用做恢复，并且不能把失败源码的哈希当成通过。

| 验收 | 测试 |
|---|---|
| 缺这三个变量时结果是 blocked，研究和编码都是 not_run | `tests/unit/test_live_harness.py::test_missing_live_key_is_blocked_and_not_a_pass` |
| 假的 HTTP 服务返回工具调用、schema 和 usage 时，研究和编码检查通过，且不写 live_ready | `tests/unit/test_live_harness.py::test_fake_server_success_records_checks_without_the_key` |
| 401 不是通过 | `tests/unit/test_live_harness.py::test_fake_server_auth_failure_is_not_a_pass` |
| 超时失败，不继续花调用 | `tests/unit/test_live_harness.py::test_fake_server_timeout_fails_closed` |
| 错误文本里的 key 被换成 `[REDACTED]` | `tests/unit/test_live_harness.py::test_error_text_redacts_the_key` |
| 入口在缺变量时把 blocked 写进指定文件 | `tests/unit/test_live_harness.py::test_script_writes_blocked_result_without_calling_out` |
