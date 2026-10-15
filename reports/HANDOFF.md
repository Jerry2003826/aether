# 交接

第四轮审计修复在 `cursor/audit-round4-repairs-7618`，基线 `07cc2ae`。本机没有 key。`live_ready` 没有因为这次改动而变化。细节在 `reports/audits/gpt_round4/REPAIR_RESULT.md`。

`c32e4d3` 上的真实研究任务被误判失败：回答里的「资料1」被当成资料中没有的数字。引用标签不再参与这个检查。回答若把沸点写成九十度或别的数字，仍然失败，原因会写成 `unsupported_number:'…'`。请再跑一次 live task check。F26 在那次已经通过。

研究调用行为变了：短引用不再单独判成功，显式来源损坏会停，预算按将发出的文本估算。监督者若要重跑，用原来的命令：

```sh
uv run --locked --no-build python scripts/run_live_task_check.py --out reports/live_task_check.json
uv run --locked --no-build python scripts/run_f26_acceptance.py --out reports/f26_acceptance_result.json
```

研究任务默认走 live 模型。PR #8 已合并为 `07cc2ae`。现场检查修复的 CI 绿头是 `185c527`。

`77551b4` 的 pull_request CI `38789787068` 里，`test_live_task_check_script_calls_the_fake_upstream` 退出码 3，`reason=api_or_worker_did_not_start`。API 其实已经在听。worker 立刻退出：产物目录是仓库里的 `data/live-task-check`，CI 检出在 `/home/runner` 下，`assert_storage_root` 拒绝宿主 home。当时结果里没有退出码和日志。脚本现在把产物目录放到 `mkdtemp`，启动失败会写下进程退出码和日志尾部。测试把 `HOME` 设成检出目录，所以再退回仓库 `data/` 会失败。假上游这条路径在 head `185c527` 的 CI 上通过：push `38793383253`（integration `116623417400`，pack-and-unit `116623417026`），pull_request `38793387951`（integration `116623434421`，pack-and-unit `116623433993`），SonarCloud 通过。没有把失败改成 `blocked`，也没有放宽断言。

监督者在 head `d694f89` 上跑了 F26，退出码 0，结果已按原字节写入 `reports/f26_acceptance_result.json`（文件 sha256 `52ffd4b2da3a463ddea421db53098b4aaa441d0e7d87d3fc9f900af1d325db38`）。同一头上的 `scripts/run_live_task_check.py` 退出码 1，原因是上传响应没有 `id`，只有 `artifact_id`。任务没有创建，模型没有被调用。脚本现在读取 `artifact_id`，异常结果记下 `step`、`error_type` 和去掉密钥后的 `error_message`。监督者在 head `7eb911b` 上用真实 key 跑了 `scripts/run_live_task_check.py`，退出码 0。`status=probed`，任务 `SUCCEEDED`，1 次 `research.call`，1 次验证，`cost_status=unknown`。原样结果在 `reports/live_task_check.json`，文件 sha256 是 `085b8b3224c305fd5ad0fece2069e939237d7ed04a8fc262862d61943fb8fc91`。文件里的 `live_ready` 是 false。这条脚本不改交付矩阵里的 `live_ready`。key 不在文件里。默认提供者仍是 live，`AETHER_INTAKE_PAUSE_SECONDS` 默认仍是 0。没有新的 Critical：测试不是固定返回，生产路径没有为了这个用例打开的测试钩子。

```text
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 pytest -q tests/integration/test_live_task_script.py tests/integration/test_live_research.py tests/unit/test_live_task_check.py
14 passed, 2 warnings in 16.07s
```

```text
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 pytest -q tests/unit/test_live_task_check.py tests/integration/test_live_task_script.py
4 passed in 5.61s
scripts/verify_freeze.py passed
```

监督者在自己的机器上跑：

```sh
export AETHER_DATABASE_URL='postgresql://aether_api:…@127.0.0.1:5432/aether'
export AETHER_WORKER_DATABASE_URL='postgresql://aether_worker:…@127.0.0.1:5432/aether'
export AETHER_VERIFIER_DATABASE_URL='postgresql://aether_verifier:…@127.0.0.1:5432/aether'
export AETHER_DISPATCHER_DATABASE_URL='postgresql://aether_dispatcher:…@127.0.0.1:5432/aether'
export AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233
export AETHER_LIVE_MODEL_BASE=https://api.deepseek.com/v1
export AETHER_LIVE_MODEL_ALIAS=deepseek-chat
export AETHER_LIVE_MODEL_KEY='只放在本机环境里'
uv run --locked --no-build python scripts/run_live_task_check.py --out reports/live_task_check.json
uv run --locked --no-build python scripts/run_f26_acceptance.py --out reports/f26_acceptance_result.json
```

本地全量：`288 passed`。CI 用本地假的 OpenAI 兼容上游，不代表 live 成功。

第三轮审计修复在 `cursor/audit-round3-repairs-7618`。基线 `4ba3b2f`。全量命令：

```text
AETHER_REQUIRE_SERVICES=1 AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 pytest -q tests/unit tests/integration
274 passed, 1 warning in 211.88s
```

`scripts/verify_freeze.py` 通过。细节在 `reports/audits/gpt_round3/REPAIR_RESULT.md`。脚本路径不是 live 模型。F26 未重跑。`budget` / `allowed_source_ids` 仍未被执行路径强制。

F04–F06 在全量套件通过后保持 done。四条 Critical（请求哈希、token 预留、连接重置、资源当前版本）的复现测试在 `tests/integration/test_budget.py` 与 `tests/integration/test_operations.py`。对照表仍是 `reports/f04_f06_criteria.md`。

## 全量测试（标 done 之前）

```text
AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/unit tests/integration
...................................................................      [100%]
67 passed, 2 warnings in 12.57s
EXIT:0
```

警告是既有的 Starlette `httpx` 弃用提示，以及 Pydantic 对 `ChatCompletionReasoningItem` 的 `ReadOnly` 提示。没有跳过的集成用例。

## P1 之后的全量测试

```text
AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/unit tests/integration
........................................................................ [100%]
72 passed, 2 warnings in 15.23s
EXIT:0
```

dispatcher 接上之后又跑了同一条全量命令：`74 passed, 2 warnings in 14.91s`，`EXIT:0`。

资源行锁和 httpx 传输错误之后：`78 passed, 2 warnings in 15.67s`，`EXIT:0`。派发前对 `resource_heads` 使用 `FOR UPDATE`。确认后的资源版本只经 `note_resource_version` 写入。`httpx.ConnectError`、`ReadTimeout`、`RemoteProtocolError` 以及它们的包装异常记为 unknown。

P1 覆盖：

- `aether_api` 不能写 operations、budget_accounts、model_requests、audit_events、verification_results、evaluation_runs；不能 UPDATE/DELETE command_outbox。新表不继承 DML。
- SUCCEEDED、FAILED、CANCELLED 不能改回 QUEUED、DRAFT、RUNNING。API 仍不能把任务推进到 DRAFT/QUEUED 以外。
- `ensure_database` 写入 `alembic_version=0004`。删掉版本行后再 `alembic upgrade head` 会在已存在的 `owners` 上失败。
- `AETHER_ALLOW_NON_LOOPBACK=1` 单独不能绑定 `0.0.0.0`，还要 Secure cookie 和 TLS 证书文件。
- 登录失败名额在密码校验前原子占用。8 个并发错误口令只有 5 个进入校验，其余 429。

## 状态

- F00、F02、F03、F04、F05、F06、F07：done
- F01 done。CPU 上对 `aether probe` 做了一次 BAAI/bge-m3 编码，`dimension_probed` 为 1024，revision 为 `5617a9f61b028005a4858fdac845db406aefb181`。OpenHands 仍是 partial，live 模型 not_run。
- F07 done。`aether_dispatcher` 把 `command_outbox` 投到真实 Temporal。Temporal 拒绝时行留在 DELIVERING，任务保持 QUEUED，不会被标成成功。同一个 command_id 再投递不会在 Workflow 里出现两次。
- F08 done。任务创建返回 202，状态保持 QUEUED，并经 dispatcher 进入真实 Temporal。SSE 用 Last-Event-ID 按 sequence 补齐，响应里有 pending command 和 projection_updated_at。审批决定由服务端核对 owner、哈希和版本。事件和产物不跨项目。全量：`83 passed, 2 warnings in 16.57s`，`EXIT:0`。对照表 `reports/f08_criteria.md`。
- F09 done。本地产物经真实存储和文本工具进入脚本网关，HTTP 调用 1 次，哈希与 `aether_verifier` 的 PASS/FAIL 写入验收表。过期候选在 actor 自报 PASS 时仍为 FAIL。worker 重启后重投同一命令，`research.call` 仍是 1 条。全量：`87 passed, 2 warnings in 17.84s`，`EXIT:0`。对照表 `reports/f09_criteria.md`。
- F10 done。HTTPS 把连接钉在解析出的地址上，证书和 SNI 仍核对原主机名。响应按流计数，超限即断开。RSS 在带资源限制的子进程里用 defusedxml 解析。Playwright 受控 profile 实际执行了页面脚本。`allow_hosts` 没有测试开关时拒绝。测试开关打开时，不在 `allow_hosts` 里的 `127.0.0.1` 仍拒绝，不会连上。`resource_heads` 的 `FOR UPDATE` 只是派发前预检。全量：`100 passed, 2 warnings in 24.21s`，`EXIT:0`。对照表 `reports/f10_criteria.md`。
- F11 done。项目设置用 `set_config(..., true)`，只在当前事务有效。PR #1 CI run `38676628928`：unit `32 passed`，integration `91 passed, 2 warnings in 35.83s`。SonarCloud quality gate OK，可靠性评级 1。对照表 `reports/f11_criteria.md`。
- 本地 MCP stdio 夹具已能完成 argv 拒绝、schema 隔离、真实 echo、超时 unknown、重连和密钥脱敏。F17 仍是 not_started：交付物要求官方 SDK 客户端，当前客户端不是官方 SDK。
- F12 done。没有测试产物的 `observed_test` 降为 proposed。同一 CI run 通过。对照表 `reports/f12_criteria.md`。
- F13 done。GitHub runner 上的真实容器测试通过：同一 job 只有一个容器，重启后重新挂上；容器内公网、元数据和 Docker socket 不可达，密钥不在环境里，只能打到该 job 的脚本网关。对照表 `reports/f13_criteria.md`。
- F14 done。PR #1 CI run `38678232085`：unit `32 passed`，integration `100 passed, 2 warnings in 45.73s`。SonarCloud quality gate OK。评分在沙箱里只跑绑定套件。对照表 `reports/f14_criteria.md`。
- F15 done。PR #1 CI run `38681057899`：unit `32 passed`，integration `102 passed, 2 warnings in 52.97s`。SonarCloud quality gate OK，可靠性、安全、可维护性都是 1。CodeGraph 1.6.2 的 CLI 在 `--network none` 容器里跑，包哈希已核对。当时产品开关关闭，那次记录里 `codegraph_enabled` 是 not_run。对照表 `reports/f15_criteria.md`。
- F16 done。PR #1 CI run `38682012230`：unit `32 passed`，integration `103 passed, 2 warnings in 70.39s`。SonarCloud quality gate OK。删除是 tombstone：visibility epoch 增加，BM25 generation 退休，已删 chunk 的向量被清空，推荐特征快照记为失效。晚到的向量和记忆文本写不回去。结果写明备份和 Temporal 历史还在。对照表 `reports/f16_criteria.md`。
- F14 done。PR #1 CI run `38683576919`：unit `32 passed`，integration `112 passed, 2 warnings in 70.42s`。SonarCloud quality gate OK。评分器用 `python -I`，先导入 pytest，再把 `/work/src` 加到 `sys.path` 末尾。顶层 `pytest.py` 和 `sitecustomize.py` 直接失败。隔离结果只认评分器写出的第一行。
- F16 done。同一 CI run。搜索缓存同时绑定 visibility epoch 和索引代，删除后不再返回已删内容。
- 当时 CodeGraph 产品开关关闭，那次记录里 `codegraph_enabled` 是 not_run。
- F17 done。PR #1 CI run `38684944139`：unit `34 passed in 8.20s`，integration `113 passed, 1 warning in 70.30s`。SonarCloud quality gate OK。进程崩溃后调用返回 unknown，后续调用不再挂起。服务器起不来是 `connect_failed`。
- F18 done。同一 CI run。未批准不建 Temporal schedule。水位不变不写通知。重复 trigger 只有一条站内通知。停止后不再派发。默认不发外部消息。
- F19 done。PR #1 CI run `38685299953`：unit `34 passed in 6.97s`，integration `114 passed, 1 warning in 67.18s`。SonarCloud quality gate OK。探索默认关闭。不称协同过滤。对照表 `reports/f19_criteria.md`。
- F14、F17、F20 done。PR #1 CI run `38686196972`：unit `40 passed in 10.06s`，integration `115 passed, 1 warning in 69.16s`。SonarCloud quality gate OK。评分结果只认父进程管道。MCP 注解不放宽本地风险和审批。技能包按需加载，未批准脚本不执行。那次记录里 `codegraph_enabled` 是 not_run。
- F21 done。PR #1 CI run `38686730319`：unit `40 passed in 7.73s`，integration `118 passed, 1 warning in 75.07s`。SonarCloud quality gate OK。对照表 `reports/f21_criteria.md`。
- F20、F21 done。PR #1 CI run `38687896639`：unit `40 passed in 7.43s`，integration `123 passed, 1 warning in 72.10s`。SonarCloud quality gate OK。密钥整段替换。脚本整目录哈希后在容器里用 `python -I` 执行。回滚中途失败时指针和 canary 状态都不提交。对照表 `reports/f20_criteria.md`、`reports/f21_criteria.md`。
- F22、F23、F27 在 PR #1 CI run `38694676859` 上是 done：unit `49 passed in 10.97s`，integration `136 passed, 1 warning in 97.37s`。SonarCloud quality gate OK。那次 head 是 `6bc678b`，随后 `5dde69a` 把 F24–F26 也标成 done。这次评审把后三项打回。
- F24 done。head `2513539` 的 PR CI run `38698706734` 和 push run `38698700686` 都成功，SonarCloud quality gate OK。CI 安装了 Hermes v0.22.1（提交 `297552a9d646119871391d202509822cd86330df`，归档 sha256 `45ebe864dbf65ecd31693a811ddd6e2e449b1878b835b563cf0a28d6a9c4bf38`）并核对了 `hermes --version`。当时四组对照还没跑，没有领先结论。
- F25 done。同一对 CI。恢复在同一事务里写回 `tables.json`，并把 `PENDING`/`DELIVERING` 改成 `STOPPED`。产物先复制到临时目录再改名。漏洞库仍是 offline not_run。
- F26 仍是 blocked，不是 done。`live_ready` 是 false。监督者在 head `4f1ca09` 上重跑了 `uv run --locked --no-build python scripts/run_f26_live.py --out reports/f26_live_result.json`，DeepSeek `deepseek-chat`，Docker 在场，退出码 0。原样结果在 `reports/f26_live_result.json`。key 不在文件里。3 次调用、1234 token，费用 unknown。
- 证据现在分得开：call-2 是 1539ms、哈希 `83ebbfa3…`，失败原因 `add(1, 2)=-1 expected 3`；call-3 是 1638ms、哈希 `ba1a531f…`，独立测试和恢复都通过。BM25、dense、RRF 都把 `chunk-apple` 放在第一，向量 revision `5617a9f61b028005a4858fdac845db406aefb181`。容器 `python:3.12-alpine`，flags 含 `--network none`，退出码 0，`network_errno` 101，`secret_present` false。
- 第一次编码失败是探针提示要求 `return a - b`，不是模型自己先完成任务再被测试打回。混合检索是三条短文冒烟，不是检索质量基准，也不是规格里的完整研究演示。编码也没有走授权仓库快照和 OpenHands。所以按 `docs/11_delivery.md`，F26 不能标 done。`live_ready` 要等 F26 通过，因此保持 false。
- `packages/storage/aether_storage/db.py` 的 `NullPool` 写在 `docs/06_security.md`：不用连接池是为了避免 RLS 会话串项目，代价是每次事务重新连接。
- 漏洞库仍是 offline not_run。`dac5c49` 那次失败原样留在 `reports/f26_acceptance_dac5c49.json`。CodeGraph 产品启用和 Hermes 四组对照的当前结论见下面两条，不以这一条旧交接为准。
- F26 done。监督者在 head `d694f89` 上重跑 `scripts/run_f26_acceptance.py`，退出码 0，`status=probed`。原样结果在 `reports/f26_acceptance_result.json`，文件 sha256 是 `52ffd4b2da3a463ddea421db53098b4aaa441d0e7d87d3fc9f900af1d325db38`。比较报告 sha256 是 `710c3053d6be9d52c18986aa3e13adfcd9d005b8ebe664ef69b3a183221b44fe`，completion 484 token。合计 3 次调用、1303 token。协议、现场 BGE 混合检索、OpenHands 1.54.0 补丁和 Linux 隔离都通过。key 不在文件里。`semantic_support` 是 `not_judged`，`cost_status` 是 `unknown`。按 `docs/11_delivery.md`，产品工程已经通过，F26 现在通过，所以 `live_ready` 是 passed。结果 JSON 里的 `live_ready` 仍是 false，脚本不自己改交付结论。此前 `cb22fbc` 那次也是退出码 0。
- CodeGraph 产品开关已由所有者打开。示例和功能矩阵仍是关闭。在 commit `2047af0` 上跑了 `uv run --locked --no-build python scripts/run_codegraph_product.py --out reports/f15_product_result.json`，退出码 0。索引 162 个文件、2674 个节点、7081 条边，库 8892416 字节，冷索引约 2.00 秒，状态 complete，版本 1.6.2。`build_report` 调用 `_counts`，调用方和被调用方与 AST 一致，没有多余边。`f26_acceptance.py` 对 `redact_secret` 的导入也被图谱记到。改快照文件后查询返回 `stale_snapshot`。索引前的文本搜索只命中快照内文件，原因是 `graph_unavailable`。容器带 `--network none`，遥测关闭。因此 `readiness.codegraph_enabled` 改为 passed。
- Hermes 四组探测已经跑过一次。监督者在 head `fcceba5` 上用 live key 跑了 `scripts/run_hermes_compare.py`，DeepSeek，`AETHER_LIVE_MODEL_BASE=https://api.deepseek.com/v1`，请求别名 `deepseek-chat`。原样结果在 `reports/hermes_compare_result.json`，sha256 `92149c0dd84fd60829d8a64e9bf59fb83ca52bbe3e102a25aa3399280fe7d3b2`。`hermes_identity` 是 verified，提交 `297552a9d646119871391d202509822cd86330df`，版本 v0.22.1。四组各 1 条 `DEV-COMPARE-SUM`，都答对。6 次计量调用，费用全部 unknown。`status=probed`。每组 n=1，Wilson 95% 区间是 0.20654329147389294–1.0（约 0.21–1.0），不足以判断领先。`comparative_evidence` 仍是 not_run，`superior` 仍是 null。请求的 model 是 `deepseek-chat`，上游返回的 model 是 `deepseek-flash`。key 不在文件里。`semantic_support` 仍是 not_judged。

## 审计一轮

基线 `b570f9197c650212a693f34a18d3729a04772f0d`。D01–D04 和 C01 的修复记录在 `reports/audits/gpt_round1/REPAIR_RESULT.md`。`STATUS.json` 没有改。

修复前，只跑审计回归：

```text
AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/integration/test_audit_artifact_regressions.py --tb=line
6 failed in 1.99s
```

修复后全量：

```text
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 uv run --locked --no-build pytest -q tests/unit tests/integration --tb=line
231 passed, 1 warning in 162.14s
EXIT:0
```

`scripts/verify_freeze.py` 在没有本地 `.hermes` 检出时 `passed`。`unittest discover -s tests -p 'test_*.py'` 76 项 OK。F26 live 没有重跑。下一步是看这条修复分支的 CI。

## 审计二轮

基线 `55fd636`。记录在 `reports/audits/gpt_round2/REPAIR_RESULT.md`。`STATUS.json` 没有改。

```text
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 uv run --locked --no-build pytest -q tests/unit tests/integration --tb=line
254 passed, 1 warning in 198.69s
EXIT:0
```

任务路径会离开 `QUEUED`。脚本提供者不是 live 模型，`live_ready` 不因此改变。F26 live 没有重跑。

## Hermes 对照审查修复

PR #4 已合入 main `9d22bb8`。Codex 的 P1 已改在 `services/worker/aether_worker/hermes_compare.py`：子进程环境是允许名单，`HOME` 和 `HERMES_HOME` 指向隔离目录；learned 不传 `--ignore-rules`，笔记写进 `memories/MEMORY.md`；`--toolsets bot_room` 加上空的 `platform_toolsets.cli`，不加载终端和文件工具；调用失败后已计量的 `gateway.calls` 仍进入报告；`stream` 和 `stream_options` 原样转发。P2 也改了：转发前预留 token，整段回复当一个 JSON 对象打分，四组共用 45 秒墙钟，记录上游返回的 model，开跑前核对钉住的 Hermes 提交和版本。

监督者已在 head `fcceba5` 用 live key 跑过一次四组探测，原样结果在 `reports/hermes_compare_result.json`。每组 n=1，Wilson 95% 区间约 0.21–1.0，不足以判断领先。`comparative_evidence` 仍是 not_run，`superior` 仍是 null，费用 unknown。请求别名 `deepseek-chat`，上游返回的 model 是 `deepseek-flash`。
