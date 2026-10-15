# F24 验收与测试

每条验收对应一个会真实执行的测试。没有 skip。没有 Hermes 运行结果时不写领先。

| 验收 | 测试 |
|---|---|
| 按 `config/hermes.lock.json` 安装 Hermes，探针输出真实的 `Hermes Agent v0.22.1` 和提交前缀 | `tests/integration/test_hermes_probe.py::test_pinned_hermes_cli_prints_its_real_version` |
| 没有 Hermes CLI 时探针是 not_run，不编版本号 | `tests/unit/test_hermes_baseline.py::test_missing_hermes_cli_is_not_a_version` |
| 开发集和 holdout 分开；缺 `AETHER_LIVE_MODEL_KEY` 时 live 是 not_run；unknown 费用不从均值里删掉 | `tests/unit/test_hermes_baseline.py::test_plan_keeps_dev_and_holdout_apart_and_does_not_claim_a_winner` |
| 四组用同一道公开题；learned 才看冻结笔记；不宣布领先 | `tests/unit/test_hermes_compare.py::test_four_groups_share_the_task_and_do_not_declare_a_winner` |
| 调用数到顶后剩下的组是 not_run | `tests/unit/test_hermes_compare.py::test_cap_leaves_later_groups_unrun` |
| holdout 放在仓库里会被拒绝，并且不调用模型 | `tests/unit/test_hermes_compare.py::test_holdout_inside_the_repo_is_rejected` |
| 整段回复必须是一个 JSON 对象；周围文字不算答对 | `tests/unit/test_hermes_compare.py::test_grade_accepts_only_the_integer_sum` |
| Hermes 子进程只用允许名单，HOME 和 HERMES_HOME 指向隔离目录 | `tests/unit/test_hermes_compare.py::test_child_env_drops_unrelated_secrets` |
| learned 把笔记写成 `memories/MEMORY.md`，不传 `--ignore-rules`，工具集是空的 `bot_room` | `tests/unit/test_hermes_compare.py::test_learned_home_writes_memory_and_disables_host_tools` |
| 四组共用 45 秒墙钟 | `tests/unit/test_hermes_compare.py::test_wall_clock_is_shared` |
| 进程失败后已计量的调用仍留在报告里 | `tests/unit/test_hermes_compare.py::test_report_keeps_calls_when_the_process_fails` |
| 转发前预留 token；`stream` 和 `stream_options` 原样转发，并记录上游返回的 model | `tests/unit/test_hermes_compare.py::test_gateway_preserves_stream_and_reserves_tokens` |
| 对照前核对钉住的 Hermes 提交和版本；对不上就不调用模型 | `tests/unit/test_hermes_compare.py::test_identity_mismatch_does_not_spend` |
| CI 里的真实 Hermes 把冻结记忆送进请求，并且走 SSE | `tests/integration/test_hermes_compare_protocol.py::test_pinned_hermes_identity_and_local_protocol` |

监督者已在 head `fcceba5` 用 live key 跑过一次四组探测。原样结果在 `reports/hermes_compare_result.json`，sha256 `92149c0dd84fd60829d8a64e9bf59fb83ca52bbe3e102a25aa3399280fe7d3b2`。四组各 1 条 `DEV-COMPARE-SUM`，都答对，共 6 次计量调用。每组 n=1，Wilson 95% 区间是 0.20654329147389294–1.0（约 0.21–1.0），不足以判断领先。`comparative_evidence` 仍是 `not_run`，`superior` 仍是 null，费用全部 `unknown`。请求的 model 是 `deepseek-chat`，上游返回的 model 是 `deepseek-flash`。`hermes_identity` 是 verified，提交 `297552a9d646119871391d202509822cd86330df`，版本 v0.22.1。

下面仍是复现命令。密钥只留在这台机器上。

```sh
uv run --locked --no-build python scripts/install_hermes.py
export AETHER_LIVE_MODEL_BASE=https://api.deepseek.com/v1
export AETHER_LIVE_MODEL_ALIAS=deepseek-chat
export AETHER_LIVE_MODEL_KEY=...
uv run --locked --no-build python scripts/run_hermes_compare.py --out reports/hermes_compare_result.json
```

`install_hermes.py` 会下载核对过的 v0.22.1 归档，并用 uv 安装 Hermes 自带的 Python 3.14 环境。对照脚本不再安装。Hermes 可执行文件必须在 `.hermes/.venv/bin/hermes`。开跑前脚本用 `git rev-parse HEAD` 和 `hermes --version` 核对锁里的提交 `297552a9d646119871391d202509822cd86330df` 和 `Hermes Agent v0.22.1`。对不上就停，调用数是 0。Docker 不是这条命令的必需项。Hermes 子进程只拿到允许名单里的变量，`HOME` 和 `HERMES_HOME` 都指向临时目录，提供者密钥留在监督者进程。

硬上限是 8 次计量调用、累计 8000 token、单次输出 128 token、四组共用 45 秒墙钟。转发前按提示长度加 128 的输出上限预留 token，不够就不再转给上游。8000 token 即使按每百万 token 10 美元计也是 0.08 美元。供应商没返回费用时保持 `unknown`，不从均值里删掉。进程失败时，已经记进网关的调用仍写进报告。每组只跑一条公开加法题，开发集 `evals/dev_cases.jsonl` 只记哈希。holdout 默认不读；若设置 `AETHER_HOLDOUT_PATH`，路径必须在仓库外面，内容也不会发给模型。`superior` 保持 null。

Hermes learned 不传 `--ignore-rules`。笔记写在隔离目录的 `memories/MEMORY.md`，由 CLI 放进系统提示。cold 不写这个文件。`--toolsets bot_room` 使用钉住版本里的空工具集，配置里的 `platform_toolsets.cli` 也是空列表。网关保留请求里的 `stream` 和 `stream_options`；上游以 SSE 回答时按 SSE 回传，并记录上游返回的 `model`。Hermes 另外会为会话起名，多一次非流式补全，和正式回答一起计入 8 次上限。整段回复必须本身是一个 JSON 对象，周围的说明或代码块不算答对。
