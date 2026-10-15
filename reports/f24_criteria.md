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

监督者还没跑这次对照，所以 `comparative_evidence` 保持 `not_run`。不要提前写 `reports/hermes_compare_result.json`。

先装钉住的 Hermes，再跑对照。密钥只留在这台机器上。

```sh
uv run --locked --no-build python scripts/install_hermes.py
export AETHER_LIVE_MODEL_BASE=https://api.deepseek.com/v1
export AETHER_LIVE_MODEL_ALIAS=deepseek-chat
export AETHER_LIVE_MODEL_KEY=...
uv run --locked --no-build python scripts/run_hermes_compare.py --out reports/hermes_compare_result.json
```

`install_hermes.py` 会下载核对过的 v0.22.1 归档，并用 uv 安装 Hermes 自带的 Python 3.14 环境。对照脚本不再安装。Hermes 可执行文件必须在 `.hermes/.venv/bin/hermes`。Docker 不是这条命令的必需项；Hermes 在宿主机上跑，才能访问本机计量网关。提供者密钥不进 Hermes 进程。

硬上限是 8 次计量调用、累计 8000 token、单次输出 128 token。8000 token 即使按每百万 token 10 美元计也是 0.08 美元。供应商没返回费用时保持 `unknown`，不从均值里删掉。每组只跑一条公开加法题，开发集 `evals/dev_cases.jsonl` 只记哈希。holdout 默认不读；若设置 `AETHER_HOLDOUT_PATH`，路径必须在仓库外面，内容也不会发给模型。`superior` 保持 null。
