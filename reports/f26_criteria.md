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

监督者机器上的命令用上面这一条，这样 BM25 依赖在锁文件里。裸 `python3` 只有在该解释器已经能导入 `bm25s` 时才能做混合检索。

探针最多 8 次 `POST /chat/completions`，每次 `max_tokens` 128，累计 token 达到 4000 就停。费用字段只有供应商返回时才写入；没有就记 `cost_status=unknown`，不估一个价钱。结果里的密钥会换成 `[REDACTED]`。

同一次运行还做三件剩下的事：

- 混合检索：对固定的三条例目做 BM25，并用 `config/retrieval/fixture.json` 里已测量的 BGE-M3 向量做稠密排序，再 RRF。模型只能根据检索结果引用 `chunk-apple` 和对应 offset。编码器失败时这项是 not_run，不编造向量。
- 编码恢复：第一次要求 `return a - b`，独立测试按 `add(1, 2) == 3` 判失败，然后必须再提交一份不同哈希的 `return a + b`。
- Linux 隔离：仅当当前是 Linux 且 `docker info` 成功时，用 `python:3.12-alpine` 跑 `--network none --read-only --cap-drop ALL`，确认容器里没有 key、公网连不上。没有 Docker 或镜像拉不下来就记 not_run 和原因，不假装通过。

`status=probed` 才表示这些检查都通过。有 not_run 时退出码是 3（`incomplete`），`live_ready` 仍是 false。F26 继续 blocked，直到新的结果文件交回来。

系统前提：

- Python 3.11 及以上。混合检索需要本仓库锁文件里的依赖，用下面的 `uv run`。不要另装一份 pip 包。
- Docker 只为隔离检查。镜像是 `python:3.12-alpine`。守护进程没起、或不在 Linux 上，这项保持 not_run。

| 验收 | 测试 |
|---|---|
| 缺这三个变量时结果是 blocked，研究和编码都是 not_run | `tests/unit/test_live_harness.py::test_missing_live_key_is_blocked_and_not_a_pass` |
| 假的 HTTP 服务返回工具调用、schema 和 usage 时，研究和编码检查通过，且不写 live_ready | `tests/unit/test_live_harness.py::test_fake_server_success_records_checks_without_the_key` |
| 401 不是通过 | `tests/unit/test_live_harness.py::test_fake_server_auth_failure_is_not_a_pass` |
| 超时失败，不继续花调用 | `tests/unit/test_live_harness.py::test_fake_server_timeout_fails_closed` |
| 错误文本里的 key 被换成 `[REDACTED]` | `tests/unit/test_live_harness.py::test_error_text_redacts_the_key` |
| 入口在缺变量时把 blocked 写进指定文件 | `tests/unit/test_live_harness.py::test_script_writes_blocked_result_without_calling_out` |
| 混合检索同时用 BM25 和已测量向量，目标条目排在 RRF 第一 | `tests/unit/test_live_harness.py::test_hybrid_context_uses_bm25_and_measured_vectors` |
| 没有 docker 可执行文件时隔离检查是 not_run，命令行不含 key | `tests/unit/test_live_harness.py::test_missing_docker_is_not_run` |
