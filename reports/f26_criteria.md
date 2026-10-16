# F26 验收与测试

没有 key 就记 blocked。脚本模型不代替这次 live 验收。`blocked` 和 `not_run` 都不是通过。F26 现在是 `done`。`live_ready` 在交付矩阵里是 passed。结果 JSON 自己的 `live_ready` 仍是 false，脚本不自证。

监督者已在 head `5ab4fef` 用 DeepSeek 跑过一次：`AETHER_LIVE_MODEL_BASE=https://api.deepseek.com/v1`，`AETHER_LIVE_MODEL_ALIAS=deepseek-chat`，Python 3.13.5，退出码 0。原样结果在 `reports/f26_live_result.json`。文件里没有 key。

head `4f1ca09` 的重跑结果已原样保存，退出码 0。Docker 在场，key 不在文件里。三次调用分别是研究 1778ms、第一次编码 1539ms、修复 1638ms。两次源码哈希不同：`83ebbfa3938d60cd5ba0353e92e097acce0b22131c0c78755e3a9cdc9a8b64b3` 和 `ba1a531f581d2e6094e978ed6f7aca7a8d92eeb62c6e7ad73ee692f7f18bc772`。第一次失败原因是 `add(1, 2)=-1 expected 3`。容器退出码 0，`network_errno` 101，`secret_present` false。

这两点必须照实写，不能把这次探针说成完整验收：

- 第一次编码失败是探针自己诱导的。提示要求模型提交 `return a - b`，独立测试再按 `add(1, 2) == 3` 判失败。模型看到了这个测试输出并另交了一份源码。这不是模型自己先做对任务、再被独立测试打回。
- 混合检索是三条固定短文上的冒烟：BM25 加 `config/retrieval/fixture.json` 里已测量的向量，再 RRF。`chunk-apple` 排在第一。这不是检索质量基准，也不是规格里的完整研究演示（导入真实中英资料，按来源版本和引用位置写比较报告）。

因此已经跑过的短探针还不是完整 F26。写这段时 Hermes 四组对照和 CodeGraph 产品启用都还没跑。当前结论以 `STATUS.json` 为准：CodeGraph 已启用；Hermes 在 head `fcceba5` 上有一次每组 n=1 的 live 探测，`comparative_evidence` 仍是 not_run。

监督者机器需要：

- Linux，并且已经有 Docker 和 uv。
- 仓库根目录的控制面锁：下面的 `uv run --locked --no-build` 会用它。不要另装一份 pip 包。
- OpenHands 不需要单独的容器镜像。在 `environments/openhands` 里执行 `uv sync --frozen`，装上钉住的 `openhands-sdk`、`openhands-tools`、`openhands-workspace`、`openhands-agent-server`，版本都是 1.54.0。
- 在 `environments/ml` 里执行 `uv sync --frozen`。编码器是这里的 `FlagEmbedding`。第一次会下载 `BAAI/bge-m3`，revision `5617a9f61b028005a4858fdac845db406aefb181`。
- `docker pull python:3.12-alpine`。独立测试和网络隔离都用这个镜像。

```sh
cd environments/openhands && uv sync --frozen
cd ../ml && uv sync --frozen
docker pull python:3.12-alpine
cd ../..
export AETHER_LIVE_MODEL_BASE=https://api.deepseek.com/v1
export AETHER_LIVE_MODEL_ALIAS=deepseek-chat
export AETHER_LIVE_MODEL_KEY=...
uv run --locked --no-build python scripts/run_f26_acceptance.py --out reports/f26_acceptance_result.json
```

`--out` 只接受相对路径 `reports/f26_acceptance_result.json`。退出码：`probed` 是 0，`failed` 是 1，`blocked` 是 2，`incomplete` 是 3。

硬上限是 8 次 chat completion、累计 20000 token。编码和协议调用单次输出最多 512 token。研究报告单次输出最多 2048 token（ADR-F28）：512 会在写完两条 sha256 引用之前被截断。20000 token 即使按每百万 token 10 美元计也是 0.20 美元，低于 1 美元。8 次研究上限也是 16384 token，仍低于累计上限。供应商没有返回费用时 `cost_status` 是 `unknown`，不估一个账单。到顶就停，不能记成通过。key 只留在监督者机器上，不进虚拟机，不进 GitHub secrets，不进日志，也不进 OpenHands 进程。job 只拿本机网关的 token。

这一次做四件事：

- 协议：一次 `tools` / `schema` / `usage` 探针，模型别名必须是 `deepseek-chat`。
- 编码：`evals/f26_snapshot` 里的 `add` 本来就是 `return a - b`。可信检查在 `evals/f26_checker/check.py`，每次复制进一个新目录，不放在模型可以改的快照里。独立测试先观察到 `add(1, 2)=-1`。每次尝试都从快照重新复制，失败目录的哈希不能当成通过。OpenHands SDK 进程自己把补丁写进 `billing.py`，父进程不替它写。通过必须来自另一次只读容器：`python:3.12-alpine`，`--network none --read-only --cap-drop ALL --pids-limit 32`。没有 Docker 或镜像拉不下来，这项是 not_run。
- 研究：`evals/f26_sources` 的中文笔记、英文笔记，加上一条无关的钢琴备忘。版本、内容哈希和 offset 记在结果里，不假装已经写入 Postgres。稠密向量必须来自现场编码器；查询和正文如果全在 fixture 里，这项是 not_run。排序是 BM25 加精确余弦再 RRF，两条实验笔记都要排在钢琴备忘前面。比较报告要带上两条的 source id、version、完整 sha256 和 offset。`version=1`、`version is 1`、`version 为 1`、`版本 1`、`版本号：1` 和 `offset=0-92` 与空格写法是同一字段。版本数字必须写在对应的 source id 旁边，数字不同、版本没有绑到来源、或缺少引用仍然失败。`finish_reason=length` 的研究报告不是通过。`semantic_support` 保持 `not_judged`，引用齐了也不等于已经人工判断语义成立。
- Linux 隔离：同一组容器 flags。容器里看不到 key，公网连不上就记下 `network_errno`。没有 Docker 是 not_run，不假装通过。

head `dac5c49` 的完整验收退出码是 1，原样留在 `reports/f26_acceptance_dac5c49.json`。协议、OpenHands 补丁和 Linux 隔离都通过。研究被记成 `citation_mismatch`。那次研究调用的 `completion_tokens` 是 512，报告最后停在 `src-en`。两条引用在截断之前已经写全。失败原因是检查器当时只认空格。检查器改完后，监督者在 head `cb22fbc` 重跑，退出码 0。那次比较报告 sha256 是 `2f811cf9a9ce878c87ba02ed5946637a2ebdebaa4c5859b9034ef7486df1057d`，488 token。四项都通过。

当前 `reports/f26_acceptance_result.json` 是后来在 head `d694f89` 上的重跑，文件 sha256 是 `52ffd4b2da3a463ddea421db53098b4aaa441d0e7d87d3fc9f900af1d325db38`。退出码 0，`status=probed`。协议、现场 BGE、OpenHands 1.54.0 和 Linux 隔离都通过。3 次调用，合计 1303 token。比较报告 sha256 是 `710c3053d6be9d52c18986aa3e13adfcd9d005b8ebe664ef69b3a183221b44fe`，completion 484 token。`semantic_support` 仍是 `not_judged`，`cost_status` 仍是 `unknown`。

按 `docs/11_delivery.md`，`live_ready` 是产品工程加 F26。产品工程此前已经 passed，这次 F26 的验收项也通过，所以交付矩阵里的 `live_ready` 改为 passed。下面几项照实保留，不算通过：

- `semantic_support` 是 `not_judged`。引用在，不等于已经判断比较内容成立。
- `cost_status` 是 `unknown`。供应商没有返回费用，不估一个账单。产物哈希有记录。
- 稠密通道是这三条笔记上的精确余弦，不是 pgvector 基准。reranker 没有用。版本记在验收结果里，不是 Postgres 的 `source_versions` 行。
- 写这段时 Hermes 四组对照和 CodeGraph 产品启用都还没跑。当前 CodeGraph 已启用。Hermes 后来在 head `fcceba5` 上做了一次每组 n=1 的 live 探测，`comparative_evidence` 仍是 not_run，见 `reports/hermes_compare_result.json`。

结果文件里的 `live_ready` 仍是 false。那是脚本不自证，不是这次交付结论。

## 已经跑过的短探针

下面是 `scripts/run_f26_live.py`，不是上面的完整验收。结果已经在 `reports/f26_live_result.json`。供应商仍是 DeepSeek。key 只留在监督者机器上，不进虚拟机，不进 GitHub secrets，不进日志。

```sh
export AETHER_LIVE_MODEL_BASE=https://api.deepseek.com/v1
export AETHER_LIVE_MODEL_ALIAS=deepseek-chat
export AETHER_LIVE_MODEL_KEY=...
python scripts/run_f26_live.py --out reports/f26_live_result.json
```

`--out` 只接受相对路径 `reports/f26_live_result.json`。退出码：`probed` 是 0，`failed` 是 1，`blocked` 是 2，`incomplete` 是 3。这条短探针的 `probed` 不是完整验收。

不需要锁文件以外的 pip 包。混合检索会导入仓库里的 `bm25s`。Python 3.11 及以上。推荐：

```sh
uv run --locked --no-build python scripts/run_f26_live.py --out reports/f26_live_result.json
```

短探针用上面这条 `uv run`。裸 `python3` 只有在该解释器已经能导入 `bm25s` 时才能做混合检索。

探针最多 8 次 `POST /chat/completions`，每次 `max_tokens` 128，累计 token 达到 4000 就停。费用字段只有供应商返回时才写入；没有就记 `cost_status=unknown`，不估一个价钱。结果里的密钥会换成 `[REDACTED]`。

同一次运行还做三件剩下的事：

- 混合检索：对固定的三条例目做 BM25，并用 `config/retrieval/fixture.json` 里已测量的 BGE-M3 向量做稠密排序，再 RRF。模型只能根据检索结果引用 `chunk-apple` 和对应 offset。编码器失败时这项是 not_run，不编造向量。
- 编码恢复：第一次要求 `return a - b`，独立测试按 `add(1, 2) == 3` 判失败，然后必须再提交一份不同哈希的 `return a + b`。
- Linux 隔离：仅当当前是 Linux 且 `docker info` 成功时，用 `python:3.12-alpine` 跑 `--network none --read-only --cap-drop ALL`，确认容器里没有 key、公网连不上。没有 Docker 或镜像拉不下来就记 not_run 和原因，不假装通过。

短探针的 `status=probed` 只表示那次探针的检查都通过。它不是上面的完整验收。有 not_run 时退出码是 3（`incomplete`）。

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
| 授权快照在任何补丁之前就让独立检查失败，提示不要求写成加法 | `tests/unit/test_f26_acceptance.py::test_authorized_snapshot_fails_before_any_patch` |
| 失败目录不会被复用，补丁由 job 写入，provider key 不进 job | `tests/unit/test_f26_acceptance.py::test_failed_acceptance_is_not_reused_and_key_stays_out_of_the_job` |
| 父进程不会把模型返回的文本自己写成补丁 | `tests/unit/test_f26_acceptance.py::test_harness_does_not_apply_the_model_text_itself` |
| 没有 Docker 时不发起编码调用，记 not_run | `tests/unit/test_f26_acceptance.py::test_missing_docker_does_not_spend_a_coding_call` |
| fixture 向量不能当成现场编码器 | `tests/unit/test_f26_acceptance.py::test_fixture_vectors_are_not_a_live_encoder` |
| 网关丢掉多余字段，并拒绝别的模型名 | `tests/unit/test_f26_acceptance.py::test_gateway_drops_extra_fields_and_rejects_other_models` |
| 执行器错误里的 key 被换成 `[REDACTED]` | `tests/unit/test_f26_acceptance.py::test_secret_in_an_executor_error_is_redacted` |
| dac5c49 的报告用等号写出两条引用；缺哈希、错 offset 或错 version 仍失败 | `tests/unit/test_f26_acceptance.py::test_dac5c49_report_cites_both_sources_with_equals_signs` |
| `version 为 1`、`版本 1`、`版本号：1`、`version is 1` 绑在对应来源上可以通过 | `tests/unit/test_f26_acceptance.py::test_version_phrasing_is_accepted` |
| 错误版本、版本只写在另一条来源上、缺少版本字段、或版本没有绑到来源，都失败 | `tests/unit/test_f26_acceptance.py::test_wrong_version_phrasing_is_rejected` |
| 研究报告请求 2048 token，超过 2048 的请求被夹到 2048；编码上限仍是 512 | `tests/unit/test_f26_acceptance.py::test_research_report_cap_accepts_2048_tokens` |
| `finish_reason=length` 被记下来，引用齐了也不算通过 | `tests/unit/test_f26_acceptance.py::test_length_truncated_gateway_response_is_recorded` |
| 产品网关不把 `finish_reason=length` 的正文当成完整回答 | `tests/integration/test_gateway.py::test_length_truncated_completion_is_not_a_complete_answer` |
| 截断的研究回答不是 SUCCEEDED，原因是 `output_truncated` | `tests/integration/test_live_research.py::test_length_truncated_research_answer_is_not_accepted` |
| 缺 key 时完整验收是 blocked | `tests/unit/test_f26_acceptance.py::test_missing_key_is_blocked` |
| 入口拒绝别的输出路径，并把 blocked 写进指定文件 | `tests/unit/test_f26_acceptance.py::test_script_writes_blocked_result_without_calling_out` |
