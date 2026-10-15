# F26 验收与测试

没有 key 就记 blocked。脚本模型不代替这次 live 验收。`live_ready` 保持 false。F26 在 `STATUS.json` 和 `tasks/backlog.yaml` 里是 `blocked`，不是 `done`。`blocked` 和 `not_run` 都不是通过。

监督者已在 head `5ab4fef` 用 DeepSeek 跑过一次：`AETHER_LIVE_MODEL_BASE=https://api.deepseek.com/v1`，`AETHER_LIVE_MODEL_ALIAS=deepseek-chat`，Python 3.13.5，退出码 0。原样结果在 `reports/f26_live_result.json`。文件里没有 key。

head `4f1ca09` 的重跑结果已原样保存，退出码 0。Docker 在场，key 不在文件里。三次调用分别是研究 1778ms、第一次编码 1539ms、修复 1638ms。两次源码哈希不同：`83ebbfa3938d60cd5ba0353e92e097acce0b22131c0c78755e3a9cdc9a8b64b3` 和 `ba1a531f581d2e6094e978ed6f7aca7a8d92eeb62c6e7ad73ee692f7f18bc772`。第一次失败原因是 `add(1, 2)=-1 expected 3`。容器退出码 0，`network_errno` 101，`secret_present` false。

这两点必须照实写，不能把这次探针说成完整验收：

- 第一次编码失败是探针自己诱导的。提示要求模型提交 `return a - b`，独立测试再按 `add(1, 2) == 3` 判失败。模型看到了这个测试输出并另交了一份源码。这不是模型自己先做对任务、再被独立测试打回。
- 混合检索是三条固定短文上的冒烟：BM25 加 `config/retrieval/fixture.json` 里已测量的向量，再 RRF。`chunk-apple` 排在第一。这不是检索质量基准，也不是规格里的完整研究演示（导入真实中英资料，按来源版本和引用位置写比较报告）。

因此 F26 仍是 blocked，`live_ready` 保持 false。规格把 `live_ready` 定义成产品工程加上 F26 通过；F26 没有通过，所以不能勾 live。Hermes 四组对照和 CodeGraph 产品启用仍是 not_run。

选中的供应商是 DeepSeek。key 只留在监督者机器上，不进虚拟机，不进 GitHub secrets，不进日志。

```sh
export AETHER_LIVE_MODEL_BASE=https://api.deepseek.com/v1
export AETHER_LIVE_MODEL_ALIAS=deepseek-chat
export AETHER_LIVE_MODEL_KEY=...
python scripts/run_f26_live.py --out reports/f26_live_result.json
```

`--out` 只接受相对路径 `reports/f26_live_result.json`。退出码：`probed` 是 0，`failed` 是 1，`blocked` 是 2，`incomplete` 是 3。`probed` 不是 live V1 已验收。

不需要锁文件以外的 pip 包。混合检索会导入仓库里的 `bm25s`。Python 3.11 及以上。推荐：

```sh
uv run --locked --no-build python scripts/run_f26_live.py --out reports/f26_live_result.json
```

监督者机器上的命令用上面这一条，这样 BM25 依赖在锁文件里。裸 `python3` 只有在该解释器已经能导入 `bm25s` 时才能做混合检索。

探针最多 8 次 `POST /chat/completions`，每次 `max_tokens` 128，累计 token 达到 4000 就停。费用字段只有供应商返回时才写入；没有就记 `cost_status=unknown`，不估一个价钱。结果里的密钥会换成 `[REDACTED]`。

同一次运行还做三件剩下的事：

- 混合检索：对固定的三条例目做 BM25，并用 `config/retrieval/fixture.json` 里已测量的 BGE-M3 向量做稠密排序，再 RRF。模型只能根据检索结果引用 `chunk-apple` 和对应 offset。编码器失败时这项是 not_run，不编造向量。
- 编码恢复：第一次要求 `return a - b`，独立测试按 `add(1, 2) == 3` 判失败，然后必须再提交一份不同哈希的 `return a + b`。
- Linux 隔离：仅当当前是 Linux 且 `docker info` 成功时，用 `python:3.12-alpine` 跑 `--network none --read-only --cap-drop ALL`，确认容器里没有 key、公网连不上。没有 Docker 或镜像拉不下来就记 not_run 和原因，不假装通过。

`status=probed` 只表示这次探针的检查都通过。它不是规格里的完整 F26，也不是 live V1。有 not_run 时退出码是 3（`incomplete`）。`live_ready` 保持 false。

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
