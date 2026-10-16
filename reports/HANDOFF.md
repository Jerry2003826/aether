# 交接

## P2-09 复核出口、所有者批准、活动时间与工作流投递（分支 `cursor/r7-workflow-review-budget-f3e3`）

基线已变基到 `6f223f7`（PR #24 已合入，ADR-F34 归公开采集）。本决定记为 ADR-F35。没有改 `contracts/`、`reference/`。没有把 `live_ready` 改成 passed。没有新增 OpenAPI 操作，公开命令仍是 approve_goal、pause、resume、cancel。编码作业、技能评测、采集/MCP/检索、安全隔离和备份产品代码没有改。没有低风险自动批准：仓库里没有预批准模板登记。

- NEEDS_REVIEW 记下原阶段。所有者用现有审批接口批准后回到 QUEUED、RUNNING 或 VERIFYING；拒绝则取消并撤销未发出的预留。`budget_unreconciled` 批准时确认账单，拒绝时释放冻结。内部 outbox kind 是 `resolve_review`，不是公开命令。
- worker 不再把 start 命令的 owner_id 写成 `goal_contracts.approved_by`。执行前必须有所有者的 `approve_goal`。`scripts/run_live_task_check.py` 在看到合同哈希后显式提交批准。
- active elapsed 只累计 RUNNING 与 VERIFYING。排队、审批、暂停和复核不计入。已开始的调用在 worker 离线时继续计时。continue-as-new 不重置这笔账。ADR-F21 的墙钟那一句保留，并注明由 ADR-F35 取代。
- 终态结束本次 workflow run，并在历史达到 5000 或服务端建议时 continue-as-new。工作流 ID 复用改为 `REJECT_DUPLICATE`。已结束的 run 不再启动成新 run。投递器领取时写租约，退避 1、2、4…秒，上限 60 秒，最多 8 次。
- 审批决定失败后，幂等键写成真实的 409，不再停在 `response_status=0`。同一键、同一载荷可以继续；不同载荷仍是冲突。超额账单可以确认或释放，且不能改成相反决定。
- 已成功任务的答案和验收报告拒绝删除，HTTP 409 `evidence_retained`。任务保持 SUCCEEDED。`project_private` 上传仍可删除。

有意改动的既有断言和准备步骤：

- `answered_task(delete_answer=True)` 期望从 204 改为 409 `evidence_retained`，任务仍是 SUCCEEDED。
- `test_audit_round3` 去掉 delete_answer 参数，新增 `test_round3_succeeded_answer_deletion_is_refused`；corrupt_answer 与 delete_source 仍要求 verdict 不是 PASS。
- 截止时间断言仍在，计时起点从 `tasks.created_at` 改为未闭合的 `task.active` start（round4 剩余时间为 0、round5 验证重试与终态提交、live 墙钟路径）。
- 需要执行过 WAITING_APPROVAL 的用例插入真实 `approve_goal`（`tests/integration/goal_approval.py` 或 HTTP）。成功断言没有放宽。
- 创建合同的 `_ensure_contract` 调用传入 `approved_by`（r6 草案不符、round3 错误事实）。
- 顺序测试在第二次投递前把租约过期；送达顺序仍是 start、pause、resume。
- 终态会自己结束 workflow，`_stop_workflow` 接住已经完成时的 `RPCError`。状态和尝试次数断言不变。
- `_FlakyHandle` 增加 `describe()`，取消后重试的断言不变。

本机回归（PostgreSQL 17.11 pgvector 0.8.7，Temporal CLI 1.5.1 开发服务器 1.29.1；本机没有 Docker、Playwright Chromium、Hermes）：

```text
uv run --locked --no-build python scripts/verify_freeze.py
# exit 0
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'
# Ran 76 tests, OK
uv run --locked --no-build pytest -q tests/unit
# 231 passed in 16.99s
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 \
  uv run --locked --no-build pytest -q tests/integration
# 320 passed, 38 failed, 1 warning in 250.78s
# 38 条都是环境：docker 不存在、Playwright Chromium 缺失、hermes_cli_missing
# 没有 skip，没有 xfail
```

本路定向：`tests/integration/test_r7_review_budget.py` 加上顺序、重试和两条投递器用例 16 passed。worker 进程、p0 批准和历史回放 13 passed。

剩余边界：没有预批准模板，低风险自动批准未实现。已被终止且任务尚未终态的 workflow 不会被重新启动。本机 Temporal 是 1.29.1，CI 镜像是 1.9.1；回放在本机通过，以 CI 为准。`live_ready` 仍是 pending。

下一步：提交并推送，PR 标为 ready。等 CI（pack-and-unit、integration、SonarCloud）全绿后再向所有者报告。

## P2-09 公开采集、工具账本、检索可见性与只读推荐（分支 `cursor/r7-ingest-tools-search-4ea5`）

基线 `cc471e3`（PR #20/#21/#22/#23 已合入，ADR 用到 F33）。本路只做四项。没有改 `contracts/`、`reference/`、`STATUS.json`。没有把 `live_ready` 改成 passed。没有新增 OpenAPI 操作。编码作业、技能评测、目标工作流和安全隔离没有改。决定见 ADR-F34。

- `github_api` 与 `arxiv_api` 由 worker 经钉住地址的 HTTP 客户端抓取。GitHub 只连 `api.github.com`（夹具主机要同时打开 `AETHER_INGEST_ALLOW_FIXTURES=1` 和 `AETHER_INGEST_FIXTURE_HOSTS`）。仓库根改写为 `/readme`，只解码内联 base64，不跟随 `download_url` 或重定向。arXiv 编号展开到 `export.arxiv.org/api/query`，Atom 在原有隔离进程里取 title、summary、author。正文走既有切块、嵌入和索引，`POST /search` 能检索。
- `route_pending` 在采集之后调用 `dispatch_approved_tools`。只使用已审核 manifest 的 `argv` 与 `bindings`。风险和幂等以注册表为准。授权必须事先存在。MCP 未知结果记 UNKNOWN；幂等工具用新进程对账一次，`non_idempotent` 不重试。`_mark_task` 不改写 SUCCEEDED、FAILED、CANCELLED。
- 成功的 `source.delete`（恰好更新一行）给该来源的 chunk 打 tombstone，并只递增 `visibility_epoch`、丢掉该项目的检索缓存。不调用 `invalidate_derived`。重放删除不再递增。
- `GET /recommendations` 只读预览，不插入画像或曝光。没有画像时 `profile_version` 为 0，`impression_id` 为 null。这与 OpenAPI 里 impression_id 必填 UUID 不一致，合同未改，偏差记在 ADR-F34。`rank_candidates` 仍是显式写入。

有意改动的既有断言：`tests/integration/test_console_api.py` 的 `test_recommendations_feedback_and_costs_read_the_ledger`。GET 不再要求写出曝光行，反馈请求不再带 `impression_id`。标题、exploration 为 false、policy_version、反馈条数和检索 503 仍在。去掉的是对曝光行 `collaborative_filtering` 的读取。

先失败再修：实现前 `tests/unit/test_remote_sources.py` 因缺少 `aether_worker.remote_sources` 无法收集。

本机回归（PostgreSQL 17.11 pgvector 0.8.7，Temporal 1.9.1，Docker 29.1.3，Hermes v0.22.1）：

```text
uv run --locked --no-build python scripts/verify_freeze.py
# exit 0
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'
# Ran 76 tests, OK
uv run --locked --no-build pytest -q tests/unit
# 231 passed
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 \
  uv run --locked --no-build pytest -q tests/integration
# Hermes 不在该进程 PATH：347 passed，1 failed
# tests/integration/test_hermes_probe.py::test_pinned_hermes_cli_prints_its_real_version
# reason=hermes_cli_missing
PATH="$HOME/.local/bin:.hermes/.venv/bin:$PATH" 下重跑该探针和本路 5 条集成用例
# 6 passed
uv run --locked --no-build python scripts/doctor.py --require-services
# exit 0
```

`reports/doctor.json` 的本机探测差异没有提交。本机 Node 是 `/exec-daemon/node`，web 未改，未重跑 typecheck；CI 使用 Node 24。

剩余边界：BM25 文件里可能还留着已删除文本的字节，返回前按 tombstone 滤掉。持有 worker 数据库口令的人仍可以写账本。本路径不发放 capability。probe 仍返回 unavailable，不打开传输。编码作业和技能评测仍是 unsupported。不改变 `live_ready`。

下一步：PR 标为 ready，不合并。等 CI（pack-and-unit、integration、SonarCloud）绿。

## SYS-08 验收报告与 SYS-03 快照边界（分支 `cursor/r6-report-sys08-sys03-e789`）

基线已变基到 `76fd71c`（PR #22，ADR-F31 归 SYS-06/04）。本分支只关闭 SYS-08 与 SYS-03，决定编号是 ADR-F32。没有改 `contracts/`、`reference/`，没有改 F26 的 `100` 判定，没有改 `reports/f26_acceptance_result.json` 的字节。协议断言仍留在父进程，记忆降级与验收条款冻结没有回退，`runner.cancel` 的撤销确认没有动。本机没有 live key。

SYS-08：`build_report` 读完整棵 JUnit。suite 计数必须和实际用例一致，至少有一条真正执行过的用例，必需用例都要出现在通过集里。`pass`、`fail`、`not_run`、`blocked` 分开。空套件和全跳过是 `not_run`，失败是 `fail`，计数对不上是 `blocked`。`live_ready` 还要当前 commit、环境、模式、配置哈希、JUnit 与验收结果哈希互相一致，并且引用的结果文件自己的 `live_ready` 为 true。旧 STATUS 不能把失败或缺失证据写成 live。`STATUS.json` 的 `readiness.live_ready` 现在是 `pending`，`f26.live_ready` 是 false：引用的结果仍是 `d694f89`，文件内 `live_ready` 为 false。文件 sha256 仍是 `52ffd4b2da3a463ddea421db53098b4aaa441d0e7d87d3fc9f900af1d325db38`。

SYS-03：`export_candidate`、`prepare_workspace`、`_stage` 只通过 `O_NOFOLLOW` 和目录描述符读取授权根里的普通文件。指向外部、指向内部、目录链接、fifo、以及根目录本身是符号链接，都会拒绝。合法项目树仍能导出和复制。评分器的协议发布和套件收紧仍在 `_publish_protocol`。

失败用例在变基后的第一笔提交，当时相对 `8b90363` 是 23 失败、1 个合法对照通过。决定见 ADR-F32。写出的目录是 `0o750`，uid 65534 只靠命名用户 ACL 进入；协议套件随后由 `_publish_protocol` 收成 `0o700`，nobody 读不到期望值。

变基到 `76fd71c` 之后的本机回归（PostgreSQL 17.11 pgvector 0.8.7，Temporal 1.9.1）：

```text
uv run --locked --no-build python scripts/verify_freeze.py
# exit 0
uv run --locked --no-build pytest -q tests/unit
# 226 passed
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 uv run --locked --no-build pytest -q tests/integration
# 340 passed, 1 warning
```

下一步：PR 保持 ready，不合并。等 CI（含 SonarCloud，不加抑制）绿。所有者在当前 head 重跑 live F26 之前，`live_ready` 保持 pending。

## SYS-06 记忆来源与 SYS-04 验收条款（分支 `cursor/r6-memory-provenance-110a`）

基线现为 `85d27d3`（PR #21 已合入，ADR-F30 归 SYS-02/07）。实现提交 `9b80aa5`。本决定改记 ADR-F31。只改 SYS-06 与 SYS-04。没有改 `verification.py`、技能评审、工作区导出、`acceptance_report`、搜索路由或 `skill.evaluate`。没有改 `contracts/`、`reference/`、`STATUS.json`。没有放宽断言，没有 skip 或 xfail。不改变 `live_ready`。

SYS-06：确认和更正继承 `provenance.sensitivity` 与 `source_artifact_ids`。显式降级是 `declassify`，要有一次性 `memory_declassify` 授权；已经是目标级别时拒绝且不消耗授权。`_is_project_test_run` 不看 JSON 的 `kind`。产物须已提交，重算哈希等于账本，正文不能自记失败，并且 `aether_verifier` 的 `test_run` 行把 PASS 绑到同一哈希。PASS 与 FAIL 并存则拒绝。正向路径：验证者账本绑定的 PASS 使记忆成为 `observed_test`，见 `tests/integration/test_r6_memory_contract.py` 的 `test_test_run_requires_verifier_ledger_bound_to_the_bytes`。`tests/integration/test_memory.py` 里没有账本的用户 JSON 期望改为 `proposed`，不再把 `kind=test_run` 当成通过。

SYS-04：有用户草案时冻结其中的验收条款和 `required_artifacts`。目标对不上就报错，不插入 `{criterion_id: goal_artifact, goal}`。没有草案时仍用自动合同。已有合同不改写。`human_review` 等不能自动通过的条款把目标完成降为 `UNVERIFIABLE`。验收版本仍是 `goal-artifact-9`。

先失败再修：同一段已提交 JSON 被内容寻址去重后，矛盾账本会绑到后来的 PASS 产物上，正向用例因此是假失败；已改成不同字节。已降到 standard 的行再请求 standard 是 `declassification_rejected`；仍为 high 的旧行使用已消费授权是 `declassification_unauthorized`。

变基到 `85d27d3` 后的本机回归（Postgres 17.11 + pgvector 0.8.7，Temporal 1.9.1，Docker 29.1.3，Hermes v0.22.1）：

```text
uv run --locked --no-build python scripts/verify_freeze.py                                              # exit 0
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'                         # Ran 76 tests, OK
uv run --locked --no-build pytest -q tests/unit                                                         # 204 passed
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 \
  PATH="$HOME/.local/bin:.hermes/.venv/bin:$PATH" uv run --locked --no-build pytest -q tests/integration
# 340 passed
uv run --locked --no-build python scripts/doctor.py --require-services                                   # exit 0
```

没有加 Sonar 抑制。PR #22 已合入 main。

## SYS-02 协议评分与 SYS-07 指标校验（分支 `cursor/r6-grader-isolation-f31b`）

基线 `8b90363`。只关闭复审 SYS-02 与 SYS-07。没有改 `contracts/`、`reference/`、验收标准或 `STATUS.json`。没有改 SYS-01（search/evaluate）、SYS-03/08（export、stage、prepare_workspace、acceptance_report）、SYS-04/06（合同与记忆）所属实现。`reports/doctor.json` 的本机探测差异没有提交。

SYS-02：能归约成「探针 == 字面期望值」的套件，断言和期望值留在父进程；nobody 子进程只返回值，读不到套件。正确实现 PASS（`trust=isolated`，`security_certified`，`auto_promote` 为 false）。错误实现、pytest `Function.runtest` 补丁、旧继承 fd 伪造都 FAIL。不能归约的白盒 pytest 即使看起来通过也不是 PASS，`trust=cooperative`，不认证、不自动发布。`record_grade` 拒绝没有隔离认证的 PASS。

SYS-07：`judge` 对 families / ci_lower / success_delta / cost_ratio 做类型、有限数和 schema 范围检查。字符串 `"NaN"`、NaN、Inf、-Inf、bool、缺失和越界都不是 ELIGIBLE。原有门槛和候选/评测/报告/审批绑定不变。这不证明曾经走出未授权发布。

先失败再修：旧代码上指标用例 22 failed / 20 passed；协议错误实现与 pytest 补丁得到 PASS。

本机回归（Postgres 17.11 + pgvector 0.8.7，Temporal `1.9.1`）：

```text
uv run --locked --no-build python scripts/verify_freeze.py                         # exit 0
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'     # Ran 76 tests, OK
uv run --locked --no-build pytest -q tests/unit                                   # 199 passed
AETHER_REQUIRE_SERVICES=1 PATH=.hermes/.venv/bin:$PATH uv run --locked --no-build pytest -q tests/integration
# 328 passed，2 failed：Playwright chromium 未安装（本分支未改 web）
# 安装后重跑这两条：2 passed。合计 330 passed
uv run --locked --no-build python scripts/doctor.py --require-services             # exit 0
```

环境变量：`AETHER_REQUIRE_SERVICES=1`，`AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres`，`AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233`。本机 Node 22，未重跑 web typecheck。

未覆盖、不能称为已修复：容器逃逸、ptrace 父进程、猜中期望值、不能归约的白盒断言。`os.kill(-1, SIGKILL)` 只出现在评分容器程序里，宿主机测试不执行它。SYS-01/03/04/05/06/08/09 保持审计状态，本分支不关单。决定见 ADR-F30。不改变 `live_ready`。

## SYS-01 公开入口接到真实检索，未实现的作业显式 unsupported（分支 `cursor/r6-entry-sys01-ac4b`）

基线 `8b90363`，变基到 `e2e4c6d`。没有改 `STATUS.json`、`contracts/`、`verification.py`、`acceptance_report.py`、`memory.py`。没有把 `live_ready` 改成通过。

- `POST /v1/projects/{id}/search` 在有 ACTIVE 索引时走 `authorised_search`，没有索引仍是 503 `index_unavailable`。
- 生产 worker 把已入队的 url / rss / local_document 采集跑到 SUCCEEDED 或 FAILED，并写出版本、分块和索引代。命令已经 DELIVERED、采集仍是 QUEUED 或 RUNNING 时，重启后按索引根目录的 `projects.json` 继续执行。
- 来源名单对得上的 Watch 变为 ACTIVE 后，在 worker 自己的任务队列上创建 Temporal schedule（间隔 `interval_seconds`），立刻跑第一次。水位变化才写通知和 `watch-run` 产物。
- `skill.evaluate` 与编码作业返回 422 `unsupported`，不写 outbox。`docs/08_api_ui.md` 的编码作业行改为 unsupported。决定见 ADR-F33（ADR-F30 已被 SYS-02/07 占用，ADR-F31 已被 SYS-04/06 占用，ADR-F32 已被 SYS-08/03 占用）。ADR-F27 里「保持 202 + QUEUED」的旧记录保留。
- 进程测试从公开 HTTP 开始，不直接调用内部 consumer。

本机回归（Postgres 17.11 + pgvector 0.8.7，Temporal dev server `SERVING`）：

```text
uv run --locked --no-build python scripts/verify_freeze.py                         # exit 0
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'     # Ran 76 tests, OK
uv run --locked --no-build pytest -q tests/unit                                   # 227 passed
AETHER_REQUIRE_SERVICES=1 PATH=.hermes/.venv/bin:$PATH uv run --locked --no-build pytest -q tests/integration
# 343 passed，1 warning（pydantic ReadOnly，既有）
uv run --locked --no-build python scripts/doctor.py --require-services             # exit 0
(cd apps/web && pnpm typecheck)                                                    # exit 0（本机 Node 22；CI 使用 Node 24）
```

环境变量：`AETHER_REQUIRE_SERVICES=1`，`AETHER_ADMIN_DATABASE_URL` 指向本机 Postgres 17，`AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233`。`reports/doctor.json` 的本机探测差异没有提交。

剩余边界：github/arxiv 采集仍显式拒绝；水位没有变化时不发通知；持有 worker 数据库口令的人仍可以伪造账本。这些没有记成已修复。不改变 `live_ready`。

下一步：PR 标为 ready，不合并。等 CI（含 SonarCloud，不加抑制）绿。

## F26 缺事实不再记成引用失败（分支 `cursor/f26-missing-fact-1f84`）

基线 `887444a`（PR #18 已合入）。没有改 `contracts/`、`reference/`，也没有放宽「报告必须包含 `100`」。没有改 `STATUS.json` 的 `live_ready`。本机没有 live model key。所有者需要重跑 `scripts/run_f26_acceptance.py`。

PR #18 合入后的 live 运行里，比较报告没有写出数字 `100`。`_report_cites` 失败，`_research_verdict` 记成 `citation_mismatch`。现在：

- 报告里没有 `100`（包括写成「一百」）时，原因是 `missing_required_fact`。`citations_cover_sources` 仍为否，状态仍是 fail。
- 数字还在，但版本或引用不对，原因仍是 `citation_mismatch`。
- `finish_reason=length` 仍优先于缺事实。
- 研究提示要求每条来源写出笔记里的数值和单位，并写明 src-zh 与 src-en 都要写出数字 `100` 和单位。

`_report_cites` 里的 `100` 检查没有改。决定见 ADR-F29。

先失败再修：旧代码上，省略数字的验收原因是 `citation_mismatch`；提示里没有「每条来源都要写出笔记里的数值和单位」。

本机回归：

```text
uv run --locked --no-build python scripts/verify_freeze.py    # exit 0
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'  # Ran 76 tests, OK
uv run --locked --no-build pytest -q tests/unit               # 151 passed
uv run --locked --no-build pytest -q tests/unit/test_f26_acceptance.py  # 30 passed
```

下一步：PR 标为 ready，不合并。等 CI（含 SonarCloud，不加抑制）绿。所有者重跑 live F26。

## F26 验收与研究路径的两处不稳定（分支 `cursor/f26-hardening-1f84`）

基线 `0767a5e`，实现提交 `bfca959`。没有改 `contracts/`、`reference/`、验收通过条件的判定口径，也没有改 `STATUS.json` 的 `live_ready`。本机没有 live model key，不能重跑真实 DeepSeek。所有者需要重跑 `scripts/run_f26_acceptance.py` 和 `scripts/run_live_task_check.py`。

两处修复：

- 版本字段除 `version` 加可选 `=` / `:` / `：` 加数字外，接受 `version 为 N`、`版本 N`、`版本号：N`、`version is N`。数字必须出现在同一分句里、并绑到对应的 source id。同一分句只有一个版本号时，这个数字绑到该分句里的每个 source id。错误版本、版本只写在另一条来源上、版本单独成句、或缺少引用，仍然失败。研究提示要求写成 `version=N`。
- 研究报告单次输出上限改为 2048 token（`RESEARCH_OUTPUT_TOKENS`）。编码和协议调用仍是 512。8 次研究上限是 16384，仍低于累计 20000 和 1 美元。网关读取 `finish_reason`；`length` 不把截断正文当成完整回答。F26 验收记 `output_truncated`。产品研究路径把任务标成人工复核，已知用量照常入账，截断正文不写入任务回答。决定见 `docs/DECISIONS.md` 的 ADR-F28。

先失败再修：在未改解析器和网关时，四种新表述、共享分句绑定、2048 上限、`version=` 提示，以及网关 `finish_reason` 断言都失败。错误版本、未绑定版本、缺少引用在旧代码上已经失败，修复后仍然失败。

本机回归（Postgres 17.11 + pgvector 0.8.7，Temporal dev server `SERVING`）：

```text
uv run --locked --no-build python scripts/verify_freeze.py                         # exit 0
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'     # Ran 76 tests, OK
uv run --locked --no-build pytest -q tests/unit                                   # 145 passed
PATH=.hermes/.venv/bin:$PATH uv run --locked --no-build pytest -q tests/integration
# 324 passed，1 failed：test_hermes_compare_protocol.py::test_pinned_hermes_identity_and_local_protocol
# 身份探测在全量负载下得到 commit=None（与上次交接里同一条用例的超时相同，本分支没有改 Hermes 身份代码）
# 单独重跑该用例：1 passed
uv run --locked --no-build python scripts/doctor.py --require-services             # exit 0
(cd apps/web && pnpm typecheck)                                                    # exit 0（本机 Node 22；CI 使用 Node 24）
```

环境变量：`AETHER_REQUIRE_SERVICES=1`，`AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres`，`AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233`。

CI：`bfca959` 的 pack-and-unit、integration 和 SonarCloud 都通过。随后只改交接的 `9dddf59` 与上一轮同时跑集成，`test_pinned_hermes_identity_and_local_protocol` 再次在负载下失败（第一次身份探测已通过，`run_four_way` 里的第二次没有通过）。本分支没有改 Hermes 身份代码。

下一步：PR #18 已标为 ready，不合并。所有者在有 live key 的机器上重跑 F26 验收和 live 任务检查。

## 系统审计第一轮 · C 组 P2-04 收紧（分支 `cursor/sysfix-c2-f4de`）

PR #16 已合入 main `b735f28`。所有者批准了 P2-04 的三项收紧，并批准相应修改验收用例 `test_owner_can_roll_a_canary_back_through_the_api`。本分支从 `b735f28` 开出，决定与剩余风险写在 `docs/DECISIONS.md` 的 ADR-F27 P2-04 段。`STATUS.json` 的 `live_ready`、`product_engineering` 未改，没有新迁移。

- 回滚只回到曾部署过的版本：目标是 active 版本，或操作账本里有已确认的 `skill.promote` 曾把它放上 canary。
- 晋升要求不可变评测报告：verifier 在 `record_evaluation` 的事务里写 JSON 报告，哈希同时记在只有 verifier 能插入的评测行里；worker 晋升时核对命令哈希、评测行哈希、产物账本哈希三者相等，再重读字节重算，并核对报告指向本次评测与本候选版本。缺少任何一项都拒绝，且不消耗审批。
- `skill.evaluate` 保持现有契约 202 + QUEUED，不实现执行器；QUEUED 评测不能晋升。
- 为让验收用例先部署父版本，晋升可以替换已有 canary（须给出当前部署版本号与当前 canary 版本）。

先失败的证据：

| 提交 | P2-04 用例结果 |
| --- | --- |
| `0f4fb37` 只改测试 | 8 failed：无报告晋升被投递成功；其余因 `record_evaluation` 不能写报告而失败 |
| `0fccccc` 加报告写入、尚无校验 | 5 failed、5 passed：回滚到从未部署的 v1、无报告晋升、worker 改写账本哈希、替换磁盘字节都被投递成功；验收用例在 canary 上晋升 v2 被拒 |
| `88b4a12` 校验 | 10 passed |

「借用另一份报告的真实哈希」在旧代码上就被拒（旧代码只在有报告时比对哈希），作为反例保留。验收用例原有两条断言（回滚命令 DELIVERED、canary 指向 v1）不变，另加两个版本状态的断言；其余 P2-04 用例在需要晋升成功的位置改用真实报告哈希，这样每条用例失败的原因只剩它要测的那一项。

全量回归（head `a8013b4`，本机 Postgres 17 + Temporal dev server，环境变量同下一节）：

```text
uv run --locked --no-build python scripts/verify_freeze.py                         # exit 0
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'     # Ran 76 tests, OK
uv run --locked --no-build pytest -q tests/unit                                   # 129 passed
PATH=.hermes/.venv/bin:$PATH uv run --locked --no-build pytest -q tests/integration  # 323 passed
uv run --locked --no-build python scripts/doctor.py --require-services             # exit 0
(cd apps/web && pnpm typecheck)                                                    # exit 0
```

integration 比 main 的 318 多 5 条：新增「从未部署的回滚」「无报告晋升」两条，以及「伪造报告」按三种伪造参数化的 3 条。下一步：等 CI 绿后把 PR 标为 ready，不合并。

## 系统审计第一轮 · C 组修复（分支 `cursor/sysfix-c-f4de`）

已 rebase 到 main `bbf2811`（含 PR #14 A 组的 P0/P1-01~03 修复与 Alembic `0012_artifact_identity`，以及 PR #15 B 组的 P1-04~06、P2-01/05/07 修复）。审计在 PR #13（`reports/audits/system_round1/`）。本组只修 P1-07、P1-08、P1-09、P2-04、P2-06 的 SSRF 网段与 MCP argv、P2-08；其余条目由另外两组处理，本分支没有碰。每项先把复现用例移植进 `tests/` 并确认失败，再修。决定与剩余风险见 `docs/DECISIONS.md` 的 ADR-F27（F25、F26 已由 A、B 使用）。`STATUS.json` 的 `live_ready`、`product_engineering` 未改。

| 条目 | 新测试 | 修复前 | 修复后 |
| --- | --- | --- | --- |
| P2-06 | `tests/unit/test_round1_p2_06_ssrf_and_mcp.py`（21） | 14 failed | 全过 |
| P2-08 | `tests/integration/test_round1_p2_08_bootstrap_passwords.py`（2） | 1 failed | 全过 |
| P1-08 | `tests/integration/test_round1_p1_08_migration_drift.py`（3） | 全部 failed | 全过 |
| P2-04 | `tests/integration/test_round1_p2_04_skill_lifecycle.py`（5） | 4 failed | 全过 |
| P1-09 | `tests/integration/test_round1_p1_09_backup_restore.py`（8） | 4 failed；审查补的 4 条 4 failed | 全过 |
| P1-07 | `tests/integration/test_round1_p1_07_history_replay.py`（8） | NondeterminismError | 全过 |

rebase 时的处理：

- 迁移改号为 `0013_runtime_patches`（down_revision `0012`），建库脚本 head 记 `0013`，`0012` 进入可升级旧标记。P1-08 增加「已在 `0012` 的库升级到 head」一例，原「已在 `0011`」一例保留。
- `durable_workflow.py` 保留 A 的 `bounded-activity-retry`（重试上限、心跳、`ArtifactError` 不重试），改为实例方法，补丁为真时同时证明是新版本。没有这一点，80f1d39 启动、start 与 pause 同时排队的历史在 prepare 之后会被当成首个版本回放，报 NondeterminismError（已用去掉该行的代码验证）。新增 80f1d39 的两份录制历史。
- `console_commands.py` 自动合并，A 的异常处理不变，晋升闸门在其上。

PR 标为 ready 后 Codex 在 `b87e6b9` 上提了 5 条 P1（没有 Critical）。已修 3 条，均先补失败用例再修：

- 恢复把被更正（superseded）的记忆改回可用：`memories.epistemic_status` 保留当前值。用例 `test_restore_does_not_revive_a_memory_corrected_after_the_backup`。
- 恢复把退役的索引代改回 ACTIVE，撞 `one_active_lexical_generation_per_project` 使整个恢复中止：`index_generations.state/activated_at` 保留当前值。用例 `test_restore_keeps_the_index_generation_activated_after_the_backup`。同类问题一起修：产物与任务产物的 `classification` 也保留当前值，否则记忆 claim 会变回可当资料的 `project_private`（用例 `test_restore_keeps_a_classification_decided_after_the_backup`）。
- 产物逐个替换中途失败会留下一半，且数据库已提交：改为先在同级暂存目录合并完整，数据库事务提交前才整体换入。用例 `test_a_restore_that_cannot_stage_every_file_changes_nothing`。

当时留给所有者决定的三项（`skill.evaluate` 的契约、回滚目标限于曾部署版本、晋升要求不可变评测报告）已在 PR #16 合并后获批，处理见上一节「P2-04 收紧」。

- B 没有改工作流和迁移，bbf2811 的工作流与 80f1d39 相同，不需要新的回放历史。B 的 `docs/06_security.md` 改动与本组的 SSRF/MCP 段落自动合并。

全量回归（head `4c38026`，本机 Postgres 17 + Temporal dev server）：

```text
export AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233
uv run --locked --no-build python scripts/verify_freeze.py                         # exit 0
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'     # Ran 76 tests, OK
uv run --locked --no-build pytest -q tests/unit                                   # 129 passed
PATH=.hermes/.venv/bin:$PATH uv run --locked --no-build pytest -q tests/integration  # 318 passed
uv run --locked --no-build python scripts/doctor.py --require-services             # exit 0
(cd apps/web && pnpm typecheck)                                                    # exit 0
```

合计 447 = main 的 400 + 本分支 47。同一 head 上第一次全量里 `test_hermes_compare_protocol::test_pinned_hermes_identity_and_local_protocol` 报 commit 为 None（身份探测子进程超时或失败时的返回值），单独重跑通过，integration 全量重跑 318 全过；本分支没有改 Hermes 相关代码。PR #13 的 `repro/`（main 未收录，临时放回跑）：46 passed、1 failed；`test_p1_02_unbounded_retry` 用 main 自己的工作流也同样失败，因为它借用的 P1-01 触发条件已被 A 修掉，任务直接 SUCCEEDED，A 的 P1-02 测试在 `tests/` 里通过。

SonarCloud：PR 之前那次失败是 abe0f5c 的新代码重复率 22.2%，来源是三份整段复制的旧版工作流代码夹具；1ba72d5 起换成录制的 JSON 事件历史，重复率 0.0%，质量门通过，没有加排除或抑制。

工作流以后任何改动都要加 patch，并在 `tests/fixtures/workflow_histories/` 加该版本的录制历史。

系统审计第一轮（PR #13 `reports/audits/system_round1/`）中 P1-04、P1-05、P1-06、P2-01、P2-05、P2-07 修在 `cursor/sysfix-b-d9aa`，最初基线 main `d6ed382`，PR #14（A 组）合入后已变基到 main `80f1d39`。P0、P1-01~03、P1-07~09、P2-04、P2-06、P2-08 由另外两个分支负责，本分支没有改。复现用例先作为一个提交移植进 `tests/integration/test_sysfix_*.py`，在修复前的代码上 20 个失败、8 个通过（通过的是对照组和保护性用例），之后每条一个提交。没有 skip、xfail 或放宽断言。ADR-F25 已被 A 组使用，本分支的裁决是 ADR-F26。本分支没有新增迁移。

- P1-04：每次执行带 `attempt_id`。只有 `sending` 的预留才记为 unknown；`dispatched`/`permitted` 的预留由重试重新定价并接管；网关在联系供应商前拒绝时释放预留并转人工复核。测试 `test_sysfix_p1_04_unsent_reservation.py`。
- P1-05：生成记录写入 `source_manifest`，验收器只按清单重读资料，清单缺失、字节变化、资料被删或提示哈希不符都是 `UNVERIFIABLE`。同一主语、单位、时段、比较关系的资料数值不同（中文数字归一化后）为 `conflicting_sources`，不判 PASS。验收版本 `goal-artifact-9`。测试 `test_sysfix_p1_05_verifier_evidence.py`。
- P1-06：评分器父进程不导入候选代码，pytest 在降权到 nobody 的子进程里跑，以退出码和随机路径 JUnit 报告为准，受信套件静态解析出的每个测试都要通过。剩余风险写在 `docs/06_security.md` §1：候选与 pytest 同进程，导入时改写 pytest 结果对象仍挡不住。测试 `test_sysfix_p1_06_grader_forgery.py`。
- P2-01：报告的 `model_mode`/`offline` 取自生成记录；未报用量为 unknown 并继续冻结，不判超额；LiteLLM 金额标为 `local_price_estimate` 并带 `pricing_version`。测试 `test_sysfix_p2_01_evidence_labels.py`。
- P2-05：模型令牌必须带 `task_id`、`request_key`、`attempt_id`；网关在同一事务里核对账本行、提示哈希、别名和输出上限后单次作废；任务停止即撤销。测试 `test_sysfix_p2_05_gateway_token.py`，`test_gateway.py`、`test_kernel.py` 改用已预留的令牌。
- P2-07：取消删除作业令牌和 job/gateway 容器，核对标签下无残留才是 `confirmed`，否则保持 requested 并报 `cancel_unconfirmed`；已取消作业再 start 直接返回 CANCELLED。测试 `test_sysfix_p2_07_runner_cancel.py`。

```text
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 uv run --locked --no-build pytest -q -p no:cacheprovider tests/unit tests/integration
400 passed, 1 warning in 313.26s
EXIT:0
uv run --locked --no-build python scripts/verify_freeze.py              -> exit 0
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'   -> Ran 76 tests, OK
uv run --locked --no-build python scripts/doctor.py --require-services  -> exit 0
```

本机没有 live model key。`live_ready` 和 `product_engineering` 没有改动。验收版本、令牌绑定和用量标注都动了 live 路径，合入前需要监督者用真实 key 重跑：

```sh
uv run --locked --no-build python scripts/run_live_task_check.py --out reports/live_task_check.json
uv run --locked --no-build python scripts/run_f26_acceptance.py --out reports/f26_acceptance_result.json
```

重跑时重点看：真实供应商没回报 usage 时任务是否停在 unknown 而不是超额；`cost_source` 是否为 `local_price_estimate`；`goal-artifact-9` 的验收报告是否带 `source_manifest`。

系统审计第一轮里，本批在 `cursor/sysfix-a-p0-p1-7618` 修了 P0-01、P0-02、P1-01、P1-02、P1-03，以及 P2-02、P2-03、P2-06 的终态数据库约束。基线是 main `d6ed382`。细节在 `reports/audits/system_round1/REPAIR_RESULT.md`。P1-04 至 P1-09 和其余 P2 不在本批。`live_ready` 与 `product_engineering` 没有改成通过。`comparative_evidence` 仍是 `not_run`。

本机没有 live model key。不把 F26 或 `live_task_check` 标成失败。合入后仍要监督者用真实 key 重跑：

```sh
uv run --locked --no-build python scripts/run_live_task_check.py --out reports/live_task_check.json
uv run --locked --no-build python scripts/run_f26_acceptance.py --out reports/f26_acceptance_result.json
```

```text
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 pytest -q tests/unit tests/integration
372 passed, 1 warning in 278.77s
EXIT:0
```

PR #10 与 PR #11 已由用户合并进 main，合并提交 `967b55f691af0559308a1c7d33d100d47ffde388`。main 上 pack-and-unit 与 integration 均为 success。F00–F27 仍是 done。`live_ready` 与 `comparative_evidence` 没有改成假绿；`comparative_evidence` 仍是 `not_run`。

PR #11 合并后 Codex 仍有未解决的 P1，修在 `cursor/audit-round5-codex-p1-7618`。多对象答案不能靠空主语通过；目标的每一部分要有自己的对象，不能只共享「限额」。pause/cancel 即使排在非控制命令后面也会被抽出来。`apply_task_control` 走独立控制线程池。派发许可在 `permitted` 阶段可以被已提交的停止撤销，撤销后不再调用供应商。尚未派发就到期的预留会释放。输出上限按剩余金额缩小。验收版本是 `goal-artifact-8`。细节在 `reports/audits/gpt_round5/REPAIR_RESULT.md`。

本机没有 live model key。不把 F26 或 `live_task_check` 标成失败，也不改 `live_ready`。合入后仍建议监督者用真实 key 重跑 `scripts/run_live_task_check.py` 和 `scripts/run_f26_acceptance.py`。

```text
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 pytest -q tests/unit tests/integration
347 passed, 1 warning in 266.29s
EXIT:0
```

PR #9 已由用户合并进 main，合并提交 `aedbdd54dccaba39e79282f87c09bab6a6dcc873`。main 上当时 pack-and-unit 与 integration 均为 success。SonarCloud 对那个合并提交是 neutral（Quality Gate not computed）；PR 当时的 quality gate passed。

第五轮审计基线仍是合并提交 `aedbdd54dccaba39e79282f87c09bab6a6dcc873`。Codex 在 PR #9 上的 P1 与 R5-01 到 R5-05 在同一分支 `cursor/audit-round5-repairs-7618`。对象对调、时段改错、比较关系反转都不能成功；目标多问一项而资料没有时是 `unverifiable`。正确转述可以成功。空正文和 token 超额都把已知 token 计入总账。任务已经 PAUSED 或 CANCELLED 后，还没发出的调用不会再发出。已有答案的重试和最终成功提交都会再核对墙钟。原样引用去重后，`project_private` 资料仍能被选为来源。验收版本是 `goal-artifact-7`。细节在 `reports/audits/gpt_round5/REPAIR_RESULT.md`。

本机没有 live model key。不把 `live_task_check` 或 F26 标成失败，也不改 `live_ready`。R5 合入前尚需监督者用真实 key 重跑 `scripts/run_live_task_check.py` 和 `scripts/run_f26_acceptance.py`。

```text
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 pytest -q tests/unit tests/integration
339 passed, 1 warning in 248.28s
EXIT:0
```

```text
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 pytest -q tests/unit tests/integration
324 passed, 1 warning in 237.74s
EXIT:0
```

第四轮审计修复曾在 `cursor/audit-round4-repairs-7618`，基线 `07cc2ae`。`live_ready` 没有因为这些改动而变化。

`c32e4d3` 上的真实研究任务被误判失败：回答里的「资料1」被当成资料中没有的数字。引用标签不再参与这个检查。回答若把沸点写成九十度或别的数字，仍然失败，原因会写成 `unsupported_number:'…'`。该修正已随 PR #9 进入 main。F26 在那次已经通过。

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
