# 修复清单（给修复方）

本清单对应审计技能第 6～8 步。本轮只审不修，下面是修复方的施工顺序、必须先写的失败回归测试、修复方向和验收门槛。修复方向只给约束，不替修复方做设计决定；任何架构层面的改动按 AGENTS.md 记入 `docs/DECISIONS.md`。

## 1. 施工规则

1. **先冻结**：从 main 最新提交开分支时，先在新基线上跑一遍 `reports/audits/system_round1/repro`，确认要修的用例仍然失败，并保存输出。主分支再有新提交就重新冻结，不要混用两个版本的源码。
2. **先写失败的回归测试，再改代码**：把对应的 repro 用例移进 `tests/integration/`（见 §2），在未修复的代码上跑出失败并保存输出，然后再做最小改动。
3. **禁止**：跳过测试、加 `xfail`、削弱断言、改动验收条件、改用 SQLite/替身代替真实 PostgreSQL/Temporal、写只测替身的假测试、为了让测试变绿去改 `contracts/` 或 `reference/`、把未跑或失败的检查写成通过。在 §5.3 的门槛达到之前，不要用 `STATUS.json` 里的 readiness 对外宣称产品就绪。
4. **不换框架**：Temporal 仍是唯一的持久调度器，PostgreSQL 仍是权威账本，LiteLLM 只做模型适配。
5. **每一项修完都跑全量回归**（§4 的命令），并更新 `reports/HANDOFF.md` 里的实际命令、提交和失败。

## 2. 回归测试怎么移植

- 每个 repro 文件的断言就是修复后的期望，可以原样移植。移植时把 `audit_helpers` 改成 `tests/integration/` 下的一个辅助模块，`conftest` 已经由 `tests/integration/conftest.py` 提供。
- 需要 Temporal 的用例（P0-02 进程级、P1-02、P1-03 第二个、P1-07）放进 CI 的 integration job（它已经有 Temporal 服务）。需要 Docker 的用例（P1-06、P2-07）同样放进 integration job（GitHub runner 有 Docker）。
- `test_zz_p2_08_password_rotation.py` 会轮换集群级角色口令，结束时再改回来。移植时建议改成「默认调用不轮换口令」的断言，或者用独立的角色名前缀，避免影响同一会话里的其他用例。
- `p1_03` 的取消场景最终状态受时序影响（`repro/output/p1_03_repeat.txt`），但「取消之后 0 次模型调用」这一条断言是确定性的，必须保留并放在前面。
- 对照组 `test_c00_controls.py` 和 `test_p1_06_grader_forgery.py::test_control_without_the_patch_still_fails` 修复前后都应通过，一并移植，防止修复把守住的部分弄坏。

## 3. 施工顺序与逐项要求

### 批次 1：P0（先修 P0-02，因为它会挡住所有命令类测试）

#### P0-02 命令应用器隔离

- **不变量**：INV-STAB-01。**场景**：S-001～S-005；顺带覆盖 S-006、S-007。
- **先写的失败测试**：`test_one_bad_console_command_does_not_wedge_the_batch[duplicate_source_locator|feedback_for_unknown_source|duplicate_connector_key]`、`test_memory_claim_matching_deleted_bytes_does_not_wedge_memory_commands`、`test_documented_worker_survives_a_duplicate_source_and_keeps_running_tasks`。补一条：带 `task_id` 的回滚命令在没有 skill pin 合同时也只把这一条标 FAILED（S-007，可直接调用 `apply_console_commands` 构造 payload）。
- **修复方向**：
  - 每条命令自己的 savepoint 捕获所有异常，把这一条标 FAILED 并记录脱敏的错误码；区分「命令本身无效」（约束违反、引用不存在、领域拒绝 → FAILED）和「基础设施暂时故障」（连接断开等 → 整批稍后重试，不标 FAILED）。
  - `route_pending` 对每个项目单独隔离；`serve` 主循环对一次 `route_pending` / `deliver_pending` 的异常记录后继续，不让 Temporal worker 退出。
  - 命令行增加尝试次数和最后错误，超过上限转 FAILED，避免无限重试。
  - API 可以对能预检的约束返回 409（重复 locator、重复 connector_key、不存在的 source_id），但应用器的兜底不能依赖 API 预检。
- **不能做**：把违反约束的命令标成 DELIVERED；删除毒行；在主循环 catch-all 之后让毒行保持 PENDING 无限重试。
- **完成判据**：以上用例通过；`test_console_api.py`、`test_memory.py`、`test_worker_process.py` 不回归。

#### P0-01 记忆与研究资料分离

- **不变量**：INV-DEL-01、INV-LEARN-02、INV-TRUTH-03、INV-EGRESS-01。**场景**：S-044～S-047；同根因潜在面 S-049。
- **先写的失败测试**：`test_deleted_memory_claim_never_reaches_the_model_or_the_verifier`、`test_corrected_memory_old_claim_is_not_ground_truth`、`test_high_sensitivity_memory_is_not_sent_to_the_cloud_model`。补两条：未删除但仍是 `proposed` 的记忆不能作为验收依据（S-047 单独断言）；显式 `allowed_source_ids` 指向一条记忆 claim 产物时的行为（拒绝，或按记忆而不是资料处理，二选一写进 ADR）。
- **修复方向**：
  - 记忆 claim 产物不能再是 `project_private` 资料。给它单独的分类，默认来源发现、显式名单校验和验收器都只认资料类分类。
  - 记忆如果要进入模型上下文，走受控的上下文编译（按删除状态、`superseded`、`visibility_epoch`、敏感度和出口审批过滤），并在提示词里标成「记忆」而不是「资料」；验收器不把记忆当证据。
  - 删除和更正时让旧 claim 产物不可再被选用（tombstone 或改分类），并递增 epoch。
  - 写数据迁移：通过 `memories.claim_artifact_id` 反查现有 claim 产物并改分类，已删除或 `superseded` 的一并处理。
  - 发往云端模型的提示词在构建前做出口检查（AGENTS.md：「enforce scope and approved data destinations before context construction」）。
- **不能做**：只在默认发现查询里排除 claim 产物而不管验收器和显式名单；用删除 `memories` 行代替删除语义；不写迁移只修新数据。
- **完成判据**：以上用例通过；`test_memory.py`、`test_retention.py`、`test_live_research.py` 不回归；迁移有测试。

### 批次 2：P1 数据身份、收敛、预算、顺序

#### P1-01 产物身份与字节分离（与 P1-02 一起修）

- **不变量**：INV-DATA-01、INV-DATA-03。**场景**：S-036～S-039；S-040。
- **先写的失败测试**：`test_p1_01_dedup_and_tombstone.py` 的 4 个用例。补一条：上传与已有产物同字节但 `media_type` 不同（S-040）。
- **修复方向**：把「字节 blob」和「逻辑产物（分类、归属、删除状态、声明的媒体类型）」分开：blob 按 `storage_key` 唯一，可被多条逻辑产物引用；删除只作用于逻辑产物，不阻止以后同字节的新逻辑产物；上传总是得到自己的逻辑产物；`task_artifacts` 只引用逻辑产物，停止写 `artifacts.task_id`（旧数据做只读兼容并有迁移测试）。物理删除仍不做，保留 ADR-F16 的披露。
- **不能做**：给答案加随机盐来规避去重；去掉 tombstone；用 `ON CONFLICT DO NOTHING` 把冲突吞掉。

#### P1-02 活动错误分类与重试上限

- **不变量**：INV-STAB-02。**场景**：S-011；S-012。
- **先写的失败测试**：`test_poisoned_execute_activity_is_not_retried_forever`。补一条：活动超时后的下一次尝试与仍在运行的上一次尝试不会互相把对方的请求改成 unknown（S-012，可用慢传输 + 短 `start_to_close` 构造）。
- **修复方向**：确定性错误以不可重试的应用错误抛给 Temporal，或在重试策略里列出不可重试类型；设置 `maximum_attempts` 和 `heartbeat_timeout`；确定性失败时把任务写成 FAILED 或 NEEDS_REVIEW 并写原因事件。工作流选项改动要加 `workflow.patched`（见 P1-07）。

#### P1-04 派发状态机

- **不变量**：INV-MONEY-02。**场景**：S-021、S-022；S-030、S-012。
- **先写的失败测试**：`test_unsent_reservation_is_reused_after_a_retry[issue_token|consume_dispatch_permit]`。补两条保护性断言：已进入 `sending` 的请求重试时仍记 unknown、不重发（不能回归）；`GatewayDenied` 在发送前抛出时释放预留（S-030）。
- **修复方向**：重试时按 `dispatch_state` 区分：`dispatched` / `permitted` 是确定未发送，复用（重新取得许可）或释放；只有 `sending` 才记 unknown。给 `sending` 之前的状态加持有者（尝试 ID 或租约），防止并发尝试互相改写。
- **不能做**：把 `sending` 也当成未发送（会重复计费）。

#### P1-03 命令顺序与控制状态单写者

- **不变量**：INV-ORDER-01、INV-STOP-02。**场景**：S-008、S-009；S-010、S-020。
- **先写的失败测试**：`test_dispatcher_keeps_per_task_order_when_one_signal_fails`、`test_cancel_then_resume_never_runs_the_model_when_a_signal_is_retried`（先断言 0 次模型调用）。
- **修复方向**：投递器按任务保序：同一任务有更早的未送达命令时，不投递后面的命令（或者给命令一个单调序号，工作流按序号应用、缺号等待）。`resume_state` 只由 worker/工作流写，API 只写命令；如果为了「立即生效」保留 API 写入，就引入单调版本号，并且 resume 不能清除已接受的取消。取消一旦被接受，就是终止性的。
- **不能做**：在测试里加 sleep 规避；只在 API 禁止「取消后恢复」而不修投递顺序。

### 批次 3：P1 验收与信任锚

#### P1-05 验收绑定快照、识别矛盾证据

- **不变量**：INV-TRUTH-01、INV-TRUTH-03。**场景**：S-051、S-053；S-052；真实环境 S-057、S-058。
- **先写的失败测试**：`test_verification_uses_the_snapshot_the_model_saw`、`test_contradictory_sources_are_not_verified_as_success`。补一条：验收第一次失败后、重试前删除一份资料（S-052），结果不能是 PASS。
- **修复方向**：发送时持久化来源快照的内容清单（产物 id + sha256，不只是一个哈希）；验收按这份清单读取并核对哈希，资料已变化时判 UNVERIFIABLE / NEEDS_REVIEW，或者按新快照重做（重做是新的模型调用，要重新预留）。同一对象、指标、单位、时段在不同资料行给出不同数值时，结论至少是 UNVERIFIABLE。递增 `VERIFIER_VERSION`，缓存键包含快照。
- **不能做**：硬编码 60/90 或甲/乙；把所有转述都判失败；把矛盾检测做成只认样例句式。

#### P1-06 评分器结果通道

- **不变量**：INV-TRUTH-02。**场景**：S-054；S-055。
- **先写的失败测试**：`test_candidate_cannot_forge_a_pass_by_patching_json`，加上改写 `os.write`、改写 `Report` 类属性两个变体；对照组保持通过。
- **修复方向**：评分结果不能来自候选可写的进程。由容器外的控制器读取结构化结果（例如 pytest junit 写到候选无法预知、无法写入的位置），同时核对子进程退出码、采集数和通过数三者一致；任何一项缺失或不一致都判 FAIL。容器参数（`--network none`、只读根、无凭证）保持不变。同步更正 docs/06 §1 对剩余风险的描述。

### 批次 4：P1 升级、迁移、恢复

#### P1-07 工作流版本迁移

- **不变量**：INV-STAB-03。**场景**：S-013；S-014。
- **先写的失败测试**：`test_current_workflow_replays_an_old_history_that_saw_a_pause`。补：为每个发布过的工作流版本（4ba3b2f、0359da5、48281f3 及之后每次改动）各录一份含 start / pause / resume / cancel 的历史 fixture，CI 全部回放。
- **修复方向**：用 `workflow.patched` 包住控制分支和 48281f3 的抽取逻辑，旧历史走旧路径；之后任何工作流改动都要求回放测试。顺带决定任务结束后是否 close 工作流或 continue-as-new（P2-09 第 9 条）。

#### P1-08 迁移链统一

- **不变量**：INV-STAB-04。**场景**：S-072。
- **先写的失败测试**：`test_alembic_head_builds_the_same_database_as_bootstrap`。
- **修复方向**：新增一个 Alembic 版本幂等地执行 `0005_runtime_patches.sql`（已被 bootstrap stamp 成 head 的库也要能安全升级），或者让 bootstrap 改为调用 Alembic；保留这个 schema 比对测试防止再次漂移。

#### P1-09 备份与恢复

- **不变量**：INV-DATA-04、INV-STAB-05。**场景**：S-073～S-075；真实环境 S-077。
- **先写的失败测试**：`test_restore_keeps_bytes_written_after_the_backup`、`test_restore_does_not_abort_when_a_task_finished_after_the_backup`、`test_backup_rebuilds_a_fresh_database`。
- **修复方向**：备份覆盖全部权威表（或改用逻辑转储 + 产物目录快照）；以「恢复到空库」为主路径（时间点还原）；在原库恢复需要明确的停服与重建流程；恢复产物时不得删除仍被账本引用的字节（先比对账本）；恢复后被 STOPPED 的命令要有对账流程；在 docs/10 写清 Temporal 状态与数据库恢复点的关系。

### 批次 5：P2

| ID | 先写的失败测试 | 修复方向 |
|---|---|---|
| P2-01 | `test_p2_01_evidence_labels.py` 的 3 个用例 | 报告的运行模式取自实际配置；缺失用量记 unknown 并冻结；只有供应商明确回报费用时才标 `provider_response`，LiteLLM 价目表估算标成本地估算并记价目表版本（`model_requests.pricing_version` 已有列） |
| P2-02 | `test_p2_02_projection.py` 的 2 个用例 | `PUT /contract` 只允许 DRAFT 任务，或者撤掉；公开的合同哈希取自 `current_contract_id` 指向的 `goal_contracts`；每次状态变化与事件在同一事务写入 |
| P2-03 | `test_api_command_event_and_worker_event_do_not_collide` | 序号分配串行化（锁任务行或每任务计数器），或者用唯一 id 排序并在冲突时重试；API 不能因为并发事件返回 500 |
| P2-04 | `test_owner_can_roll_a_canary_back_through_the_api`、`test_promotion_requires_an_approval_and_evaluation_for_this_version`；补「同一审批不能用两次」（S-066） | 回滚不依赖请求体里没有的 `task_id`；晋升核对评测的候选就是目标版本且已通过、审批意图就是这次晋升、原子消费审批、核对 `report_sha256`；评测执行器在接上之前，API 应明确返回「未实现」而不是排队 |
| P2-05 | `test_model_token_dies_with_its_task_and_cannot_bypass_the_ledger`；补「取消后令牌失效」（S-029） | 令牌绑定 task 与 request_key、一次性使用；网关在调用供应商前核对派发许可；任务结束和取消时撤销 |
| P2-06 | `test_p2_06_defense_in_depth.py` 的 7 个用例 | 地址检查改用 `is_global` 并显式拒绝共享地址段；DB 守卫拒绝终态到任何其他状态，`_mark_task` 加状态条件；MCP 只接受管理员审核过的可执行文件白名单，不接受解释器的内联代码参数；文档不再把正则过滤称作边界 |
| P2-07 | `test_cancel_stops_the_job_container` | `cancel` 停止并移除容器、撤销作业令牌，返回「已请求取消 / 已确认停止」两种状态 |
| P2-08 | `test_rerunning_bootstrap_does_not_lock_out_running_services`（移植时改成默认不轮换的断言） | 默认不轮换口令，轮换需要显式参数，README 同步 |
| P2-09 | 各条按需补测 | 产品待办：接通或明确下线采集/编码/评测/跟踪/搜索；NEEDS_REVIEW 的人工处理入口；合同批准不再代签；验收器与网关凭证分进程；`recover()` 接入启动流程；对账入口；镜像固定摘要 |

## 4. 全量回归命令

每一项修复后、合并前都要跑，并把输出写进 `reports/HANDOFF.md`：

```bash
uv sync --locked --no-build
uv run --locked --no-build python scripts/verify_freeze.py
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'
uv run --locked --no-build pytest -q tests/unit
AETHER_REQUIRE_SERVICES=1 \
AETHER_ADMIN_DATABASE_URL=postgresql://postgres:<口令>@127.0.0.1:5432/postgres \
AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 \
uv run --locked --no-build pytest -q tests/integration      # 需要 Docker、Playwright Chromium、Hermes（与 CI 相同）
AETHER_REQUIRE_SERVICES=1 ... uv run --locked --no-build pytest -p no:cacheprovider reports/audits/system_round1/repro   # 移植完成前作为对照
(cd apps/web && pnpm install --frozen-lockfile && pnpm typecheck)
```

基线参照：依赖齐备时 unit + integration = 347 passed（`baseline/run4_full_unit_integration.txt`）。修复后这个数只能增加（新回归测试），不能因为删测试而减少。

## 5. 独立验证（审计技能第 8 步）

### 5.1 真实服务上的故障注入验收

在真实 PostgreSQL、Temporal、worker 进程和 Docker 上重跑以下场景，记录产物、调用次数、账本和身份，而不只看进程是否重启（docs/04 §8、docs/14 G 节）：

| 规格要求 | 对应本轮场景 |
|---|---|
| task 已提交但 workflow 未启动；Temporal 收到命令但 ACK 丢失 | S-008、S-009、S-010 |
| 外部服务已处理但响应丢失；LLM 超时可能计费 | S-021、S-022、S-023、S-035 |
| artifact 落盘但账本未提交 | S-041 |
| worker 在等待中重启；runner 存活而 worker 重启 | S-004、S-012 |
| 重复审批命令；审批后参数变化 | S-019、S-066 |
| verifier 在生成报告前崩溃；新候选配旧报告 | S-051、S-052、S-054 |
| workflow 代码升级后的历史回放 | S-013、S-014 |
| 删除与异步索引提交竞态 | S-044、S-048 |
| SSE 断线、重复事件 | S-017、S-018 |
| 恢复备份后旧命令与结果对账 | S-073～S-075、S-077 |

### 5.2 受控真实模型验收（需要 key，本轮未做）

需要所有者在 Cursor Dashboard（Cloud Agents → Secrets）或本机环境里配置 `AETHER_LIVE_MODEL_KEY`、`AETHER_LIVE_MODEL_BASE`、`AETHER_LIVE_MODEL_ALIAS`，并设定花费上限后再跑：

```bash
uv run --locked --no-build python scripts/run_live_task_check.py --out reports/live_task_check.json
uv run --locked --no-build python scripts/run_f26_acceptance.py --out reports/f26_acceptance_result.json
```

另外针对以下「需要真实环境」场景各做一次受控实验并留下原始响应：S-023（断线后是否计费）、S-026（账单与 LiteLLM 价目表的差额）、S-027（tokenizer 差异）、S-034（截断输出）、S-035（5xx / 连接重置是否计费）、S-057（资料里的提示注入）、S-058（验收启发式在真实输出上的误判率）。S-077、S-078、S-079 需要持久化 Temporal、OpenHands 和生产集群，单独排期。

### 5.3 完成门槛

- P0 清零。
- P1 全部关闭，或者在 `docs/DECISIONS.md` 里写明接受理由、残余风险和复审日期。
- 已测与未知分开列出：哪些场景在真实服务上通过，哪些仍是「需要真实环境」，不能把未跑的检查写成通过。
- 交付物：风险报告、结果文件的 sha256、剩余风险清单，并确认所有产物里没有密钥（可复用 `BASELINE.md` §5 的扫描规则）。
