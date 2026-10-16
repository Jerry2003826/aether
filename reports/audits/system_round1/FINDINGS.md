# 审计发现（按根因合并，基线 `d6ed382`）

## 0. 结论

在依赖补齐的前提下，基线自带的 347 项产品测试全部通过；但本轮在真实 PostgreSQL 17、Temporal dev server 和 Docker 上写的 43 个复现用例全部失败，归并成 **20 个根因：P0 2 个，P1 9 个，P2 9 个**。其中 2 个 P0 都在默认产品路径上，由普通操作触发，不需要故障注入：

1. 记忆被当成研究资料：删除、更正、高敏标记都挡不住它进入发往云端的提示词，未确认的记忆还能当验收依据把任务判成 SUCCEEDED。
2. 一条重复的来源/连接器或一条悬空的反馈，就让所有项目的命令处理卡死，并让 `python -m aether_worker` 退出；重启后再次退出。

`STATUS.json` 当前写着 `product_engineering: passed`、`live_ready: passed`。本轮只审不修，没有改这个文件；但在 P0 清零之前，这两项与本轮证据冲突，修复方不应据此对外宣称产品就绪。

### 定级口径

- **P0**：普通 owner 操作或常见故障即可在产品主路径上触发，破坏「不越权 / 不谎报成功 / 不坏数据 / 删除不复活 / 长期稳定」之一，且影响持久或跨项目。
- **P1**：在产品主路径上需要一次故障、竞态或特定但现实的条件才触发；或者是信任锚组件（验收器、预算、迁移、恢复）本身的缺陷，哪怕暂未接线。
- **P2**：纵深防线缺口、失真的标签和投影、未接线组件的缺陷、有绕行办法的可用性问题。

### 总表

| ID | 等级 | 根因（一句话） | 不变量 | 场景 | 复现用例（`repro/`） |
|---|---|---|---|---|---|
| P0-01 | P0 | 记忆 claim 存成 `project_private` 文本产物，自动成为研究资料和验收依据；删除、更正、敏感度都不影响它 | DEL-01、LEARN-02、TRUTH-03、EGRESS-01 | S-044～S-047 | `test_p0_01_memory_as_source.py`（3） |
| P0-02 | P0 | 命令应用器整批同事务且只捕获领域异常，主循环无隔离：一条违反约束的命令让所有项目停摆、worker 退出 | STAB-01 | S-001～S-005 | `test_p0_02_poison_commands.py`（5） |
| P1-01 | P1 | 一个 `storage_key` 只有一行：去重共享了分类/归属/删除状态，tombstone 永久占住地址 | DATA-01、DATA-03 | S-036～S-039 | `test_p1_01_dedup_and_tombstone.py`（4） |
| P1-02 | P1 | 活动默认无限重试、不区分确定性错误，任务永远 RUNNING | STAB-02 | S-011 | `test_p1_02_unbounded_retry.py`（1） |
| P1-03 | P1 | 发件箱不保证同一任务的命令顺序；API 处理 resume 时抹掉 `cancel_requested`；取消后仍调用模型 | ORDER-01、STOP-02 | S-008、S-009 | `test_p1_03_command_order.py`（2） |
| P1-04 | P1 | 派发状态机把「已预留未发送」和「已发送结果未知」混为一谈，重试即永久冻结预算并转人工 | MONEY-02 | S-021、S-022 | `test_p1_04_unsent_reservation.py`（2） |
| P1-05 | P1 | 验收重新读取当前资料，不绑定生成时的快照；对矛盾资料，复述两个值的答案可得 PASS | TRUTH-01、TRUTH-03 | S-051、S-053 | `test_p1_05_verifier_evidence.py`（2） |
| P1-06 | P1 | 评分结果通道在候选可写的同一进程，候选 import 时改写 `json.dumps` 即可伪造 PASS | TRUTH-02 | S-054 | `test_p1_06_grader_forgery.py`（1 + 对照 1） |
| P1-07 | P1 | 控制命令分支改版没有 `workflow.patched`，收到过暂停的旧工作流历史回放失败 | STAB-03 | S-013 | `test_p1_07_history_replay.py`（1） |
| P1-08 | P1 | `tokens_held` 和 `resource_heads` 只在 bootstrap 脚本里，Alembic head 建出的库跑不了预算 | STAB-04 | S-072 | `test_p1_08_migration_drift.py`（1） |
| P1-09 | P1 | 备份只含 4 张表；恢复是合并而非还原，会删掉备份后写入的字节，遇终态任务中止，不能在新库重建 | DATA-04、STAB-05 | S-073～S-075 | `test_p1_09_backup_restore.py`（3） |
| P2-01 | P2 | 验收报告把运行模式写死为 scripted/offline；缺失用量被记成超额；本地价目表估算被标成供应商回执 | TRUTH-05、MONEY-04 | S-024、S-025、S-056 | `test_p2_01_evidence_labels.py`（3） |
| P2-02 | P2 | 公开视图的合同哈希来自客户端可写的草稿，终态任务也能改；终态不写事件 | TRUTH-04、PROJ-02 | S-018、S-070 | `test_p2_02_projection.py`（2） |
| P2-03 | P2 | 事件序号用 `max(sequence)+1`，API 与 worker 并发写时撞唯一约束 | PROJ-01 | S-017 | `test_p2_03_event_sequence.py`（1） |
| P2-04 | P2 | 技能回滚经 API 永远失败；晋升不绑定审批意图、评测对象与通过结果 | LEARN-01、AUTH-04 | S-065、S-067 | `test_p2_04_skill_lifecycle.py`（2） |
| P2-05 | P2 | 模型令牌不绑任务、不经账本、任务结束后 10 分钟内仍可调用供应商 | AUTH-03、MONEY-05 | S-028 | `test_p2_05_gateway_token.py`（1） |
| P2-06 | P2 | SSRF 黑名单漏 100.64.0.0/10 与 fec0::/10；DB 终态守卫不拦终态互改；MCP argv 过滤放行 `sh -ec`/`node -e` | UNTRUST-02、STOP-01、UNTRUST-04 | S-059、S-062、S-063 | `test_p2_06_defense_in_depth.py`（7） |
| P2-07 | P2 | `LinuxRunner.cancel` 只改记录，容器继续运行 | STOP-03 | S-061 | `test_p2_07_runner_cancel.py`（1） |
| P2-08 | P2 | `ensure_database` 默认轮换全部角色口令，重跑即让在跑服务断库 | STAB-06 | S-076 | `test_zz_p2_08_password_rotation.py`（1） |
| P2-09 | P2 | 产品路径未接通与规格偏离（静态证据汇总） | 多条 | 见正文 | 无（静态） |

全部复现的实际输出：`repro/output/repro_run.txt`（pytest `-rA --tb=short` 全文）和 `repro/output/repro_results.xml`（junit）；另有 `repro/output/p1_03_repeat.txt`（取消场景重复 5 次）和 `repro/output/litellm_cost_probe.txt`（LiteLLM 本地估价探测）。结果为 **43 failed，4 passed（对照组），退出码 1**。运行命令：

```bash
export AETHER_REQUIRE_SERVICES=1
export AETHER_ADMIN_DATABASE_URL=postgresql://postgres:<本机口令>@127.0.0.1:5432/postgres
export AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233
uv run --locked --no-build pytest -p no:cacheprovider -rA --tb=short reports/audits/system_round1/repro
```

复现用例只替换 `litellm.completion` 传输（或把 `live_model_base` 指向本机伪造的 OpenAI 兼容服务），数据库、Temporal、产物存储、验收器、Docker 都是真的。用例断言的是不变量本身，修好之后应当变绿，可以直接移植成回归测试（见 `REPAIR_PLAN.md`）。

---

## P0-01 记忆 claim 被当成研究资料与验收依据

**分类**：已证实缺陷（3 个用例）。**场景**：S-044、S-045、S-046、S-047。**不变量**：INV-DEL-01、INV-LEARN-02、INV-TRUTH-03、INV-EGRESS-01。

### 根因

记忆模块把 claim 文本用 `ArtifactStore.put` 存成默认分类（`project_private`）的 `text/plain` 产物：

```python
# services/worker/aether_worker/memory.py:298（创建）与 :338（更正）
stored = store.put(owner_id=owner_id, project_id=project_id, data=claim.encode(), media_type="text/plain", conn=conn)
```

研究任务不传 `allowed_source_ids` 时的默认来源，恰好就是「本项目所有未删除的 `text/plain` + `project_private` 产物」；验收器用同一个函数重新取资料：

```python
# services/worker/aether_worker/goal_effect.py:779-787
"SELECT id::text AS id, sha256 FROM artifacts "
"WHERE project_id = :project_id AND deleted_at IS NULL "
"AND media_type = 'text/plain' AND classification = 'project_private' "
# goal_effect.py:574（验收器）
sources, snapshot, source_state, _missing = _source_snapshot(verifier, store, owner_id, project_id, allowed_ids=allowed_ids)
```

记忆删除只写 `memories.deleted_at`（`memory.py:365-372`），更正只把旧行标成 `superseded`（`memory.py:340-343`），都不动 claim 产物；`sensitivity` 只写进 provenance JSON（`memory.py:312`），唯一检查它的 `_exportable`（`memory.py:166-175`）只被未接线的 `assemble_context` 调用；研究提示词把全部资料原文拼进去（`research.py:96-98`，`goal_effect.py:999`）。记忆的生命周期（删除、更正、确认状态、敏感度）因此完全不影响它作为「资料」的身份。

### 复现与实际输出

`repro/test_p0_01_memory_as_source.py`：

- 创建记忆「乙服务的请求超时时间是30秒。」，经 API 删除并应用；项目里没有任何上传。提问「乙服务的请求超时时间是多少秒？」。实际：`status=SUCCEEDED calls=1 prompt_has_deleted_claim=True verifications=[{'verdict': 'PASS', ...}]`，发往模型的提示词是 `[资料1]\n乙服务的请求超时时间是30秒。`。
- 记忆「丙接口每分钟最多接受90次请求。」更正为 60 次。实际：提示词同时含 `[资料1] …90次…` 和 `[资料2] …60次…`。
- `sensitivity=high` 的记忆「丁服务的值班电话是13800000000。」。实际：作为 `[资料2]` 发往模型。

### 影响

用户删掉的内容在之后每个未指定来源的任务里继续发往云端模型；验收器把一条已删除、从未确认的记忆当成事实，任务被判 SUCCEEDED；高敏记忆未经批准外发；被更正的旧说法与新说法同时成为资料，构成一对矛盾资料（再叠加 P1-05，复述两者的答案会被判 PASS）。

### 定级理由

P0：默认路径、普通操作，同时破坏「删除不复活」「不谎报成功」「数据出口」三条。前五轮没有覆盖记忆与研究资料的交互；ADR-F16 的「删除先 tombstone 并递增 visibility_epoch」只覆盖派生索引，没有覆盖 claim 产物。

### 同根因的潜在面

采集一旦接上，`store_text_version` 也把原文和规范文本写成 `project_private` 文本（`ingest.py:283-284`），而 `source.delete` 不 tombstone 产物（`console_commands.py:196-210`），会出现同样的问题（S-049）。

---

## P0-02 一条重复或悬空的命令毒化整批命令，并让 worker 进程退出

**分类**：已证实缺陷（5 个用例，含 1 个进程级用例）。**场景**：S-001～S-005；潜在同类 S-006、S-007。**不变量**：INV-STAB-01。

### 根因

三层叠加：

1. 应用器把一个项目的全部 PENDING 命令放进一个事务（`FOR UPDATE`），逐条用 savepoint，但只捕获领域异常：

```python
# services/worker/aether_worker/console_commands.py:54-61（记忆应用器 memory.py:224-228 同构）
nested = conn.begin_nested()
try:
    _apply(conn, ...)
    nested.commit()
    status = "DELIVERED"
except ConsoleCommandError:
    nested.rollback()
    status = "FAILED"
```

   数据库约束异常（`IntegrityError`）、`ArtifactError`、`NoResultFound` 冒出后，整个事务回滚，同批命令全部留在 PENDING。
2. API 只做格式校验就以 202 接受命令，不预检这些约束：来源的 `canonical_locator_hash` 唯一（`contracts/schema.sql:220`）、连接器 `connector_key` 唯一（`schema.sql:415`）、反馈的 `source_id` 必须存在（`schema.sql:348`）。`_source_create` 只按新生成的 `source_id` 判重（`console_commands.py:140-143`）。
3. `route_pending` 依次处理所有项目，主循环没有任何异常隔离（`service.py:39-65,142-154`），异常直接让 `async with worker` 退出，Temporal worker 一起停；毒行仍是 PENDING，下次启动立刻再崩。

### 复现与实际输出

`repro/test_p0_02_poison_commands.py`：

- 同一 URL 来源提交两次：`IntegrityError ... duplicate key value violates unique constraint "sources_project_id_canonical_locator_hash_key"`；同项目的第一条、后续无关命令和**另一个项目**的命令都是 PENDING。
- 反馈引用不存在的来源：`ForeignKeyViolation ... "feedback_events_project_id_source_id_fkey"`，同上。
- 同 key 连接器：`UniqueViolation ... "connector_configs_project_id_connector_key_key"`，同上。
- 记忆 claim 与已删除产物同字节：`ArtifactError: 已删除的产物仍占用内容地址`，两条记忆命令都 PENDING。
- 进程级：按文档启动 `python -m aether_worker`（真实 PG + Temporal），`worker 进程退出 returncode=1，另一项目的任务状态=RUNNING`，日志尾部是同一个 `IntegrityError` 栈。

### 影响

一次「再添加一次同一个 URL」就让所有项目的命令处理和任务执行停摆，已经 RUNNING 的任务卡住；进程守护重启无法自愈，只能手工改库。

### 定级理由

P0：长期稳定被普通操作破坏，影响全局且持久。

---

## P1-01 内容寻址把「同字节」当成「同一个产物」，tombstone 永久占住地址

**分类**：已证实缺陷（4 个用例；P0-02 的记忆变体也由它触发）。**场景**：S-036～S-039；潜在 S-040。**不变量**：INV-DATA-01、INV-DATA-03。

### 根因

`artifacts` 表用 `UNIQUE(project_id, storage_key)` 约束字节地址（`contracts/schema.sql:155`），一行同时承担字节、角色（`classification`）、归属（`task_id`）和删除状态（`deleted_at`）。

```python
# packages/storage/aether_storage/artifacts.py:703-705
existing = self._live_identity(conn, project_id, storage_key)
if existing is not None:
    return existing          # 返回已有行的 id、media_type、classification，调用方声明的分类被丢弃
# artifacts.py:726-732
retained = conn.execute(text("SELECT id::text FROM artifacts WHERE project_id = :project_id AND storage_key = :storage_key"), ...).scalar()
if retained is not None:
    raise ArtifactError("tombstone_retained", "已删除的产物仍占用内容地址") from None
```

删除就是给这一行打 tombstone（`artifacts.py:182-189`），共享这段字节的所有引用一起「被删」。第五轮 R5-05 只在来源选择里放宽了「被任务引用过的 `project_private` 资料」（ADR-F23），首次引用仍写 `artifacts.task_id`（`goal_effect.py:493-500`）；反方向（上传继承任务产物身份）、删除连带和 tombstone 占位都没处理，根因没有消除。

### 复现与实际输出

`repro/test_p1_01_dedup_and_tombstone.py`：

- 把上一个任务的答案下载后再上传：`上传返回 id=<原答案 id> classification=task_answer`；用它当显式来源的任务 `NEEDS_REVIEW`，`stop_reason=allowed_sources_invalid`。
- 删除用户自己的这份上传：另一个任务的答案 `not_found`。
- 删除任务 A 的答案后，任务 B 生成同字节答案：`ArtifactError: 已删除的产物仍占用内容地址`。
- 删除后重新上传同一文件：`422 {"code":"tombstone_retained", ...}`。

### 影响

普通使用（复用答案作资料、模板化的短答案、重复上传）就会碰到；删除会悄悄连带别的任务的答案与证据；某段字节一旦被删，以后任何同字节的写入都永久失败——答案路径进入 P1-02 的无限重试，记忆路径进入 P0-02 的整批卡死。

### 定级理由

P1：主路径、无需故障注入，但需要字节恰好相同。

---

## P1-02 确定性失败的活动被无限重试，任务永不收敛

**分类**：已证实缺陷（1 个用例，真实 Temporal）。**场景**：S-011；潜在 S-012。**不变量**：INV-STAB-02。

### 根因

所有活动都只设 `start_to_close_timeout=60s`，用 Temporal 默认重试策略（无限次、没有不可重试的错误类型），也没有 `heartbeat_timeout`：

```python
# services/worker/aether_worker/durable_workflow.py:82,86,110,120
await workflow.execute_activity(apply_task_control, activity_input, start_to_close_timeout=timedelta(seconds=60))
prepared = await workflow.execute_activity(prepare_task, activity_input, start_to_close_timeout=timedelta(seconds=60))
handle = workflow.start_activity(execute_task, activity_input, start_to_close_timeout=timedelta(seconds=60), ...)
```

活动内部不区分确定性错误与暂时错误，`ArtifactError`、约束错误、编程错误都原样抛出（例如 `goal_effect.py:1101`）。worker 崩溃要等 60 秒的 start-to-close 超时才会被发现。

### 复现与实际输出

`repro/test_p1_02_unbounded_retry.py`：借用 P1-01 的 tombstone 触发条件，`16 秒内 execute_task 已重试到第 5 次，任务状态 RUNNING`；运行日志里 `attempt` 从 1 递增到 5，每次都是同一个 `ArtifactError`。

### 影响

任务永远停在 RUNNING，API 看不到失败原因；重试风暴持续占用 worker 线程和数据库连接。与 P1-01 叠加时可由普通操作触发。

### 定级理由

P1。

---

## P1-03 发件箱不保证同一任务的命令顺序，取消可以被吞掉

**分类**：已证实缺陷（2 个用例，其中 1 个在真实 Temporal 上）。**场景**：S-008、S-009；潜在 S-010、S-020。**不变量**：INV-ORDER-01、INV-STOP-02。

### 根因

1. 投递器按 `created_at` 领取一批跨任务的命令，单条信号失败时 `continue` 去投下一条，没有「同一任务前一条未送达，后一条就不送」的约束；失败的行保持 DELIVERING，下一轮再投，于是顺序颠倒：

```python
# services/worker/aether_worker/dispatcher.py:99-115
try:
    await _signal(...)
except Exception:
    failed.append(command_key)
    continue
_mark(database, ..., "DELIVERED")
```

2. API 接受命令时直接改写 `tasks.resume_state`，而 worker 的停止检查与派发许可都以它为准（`goal_effect.py:1269-1293`，`budget.py:212-221`）。处理 resume 时，只要任务是 PAUSED 就把它置成 NULL，抹掉了尚未生效的取消请求：

```python
# services/api/aether_api/control.py:168-173
"UPDATE tasks SET resume_state = NULL, version = version + 1 "
"WHERE id = :id AND (status = 'PAUSED' OR resume_state = 'pause_requested')"
```

### 复现与实际输出

`repro/test_p1_03_command_order.py`：

- 只让 pause 的第一次信号失败：`实际送达顺序 ['start', 'resume', 'pause']，用户发出的顺序是 start, pause, resume`。
- 真实 Temporal：任务已 PAUSED，用户先 cancel 再 resume，cancel 的第一次信号失败。正式运行输出 `送达顺序=['resume', 'cancel'] 最终状态=CANCELLED 取消之后的模型调用次数=1`。重复 5 次（`repro/output/p1_03_repeat.txt`）：**每次都发生 1 次模型调用**，最终状态 4 次 CANCELLED、1 次 SUCCEEDED，取决于补投的 cancel 与执行完成谁先到。

### 影响

一次暂时的 Temporal 信号失败（网络抖动、Temporal 重启）就能让用户的取消在模型调用前失效，取消之后发生付费调用，任务甚至以 SUCCEEDED 收尾；暂停与恢复的最终结果也可能与用户最后一次操作相反。

### 定级理由

P1：需要一次投递失败作为前提。

---

## P1-04 未发出的预留在重试时被记成「结果未知」，金额永久冻结

**分类**：已证实缺陷（2 个用例）。**场景**：S-021、S-022；潜在 S-030；相关 S-012。**不变量**：INV-MONEY-02。

### 根因

`prepare_model_call` 新建预留时就写 `dispatch_state='dispatched'`（`budget.py:429-435`），之后才有 `permitted`、`sending`。重试时只要行是 `reserved` 且没有回执，就一律改成 unknown 并返回 `unreconciled`，不看 `dispatch_state`：

```python
# packages/storage/aether_storage/budget.py:411-419
if existing["usage_status"] == "reserved":
    conn.execute(text("UPDATE model_requests SET usage_status = 'unknown', actual_micro_usd = NULL ... AND usage_status = 'reserved'"), ...)
return {"action": "unreconciled"}
```

`dispatched` 和 `permitted` 都表示请求确定没有离开进程，只有 `sending` 才可能已发出。ADR-F24 只修了「到期未派发」这一条路径（改用 `release_unsent`），崩溃或异常后的重试路径没修。

### 复现与实际输出

`repro/test_p1_04_unsent_reservation.py`：在 `issue_token`（或 `consume_dispatch_permit`）抛一次暂时错误后重试同一个活动。实际：`重试前=[{'usage_status': 'reserved', 'dispatch': 'dispatched'}] 重试后=[{'usage_status': 'unknown', ...}]`，任务 `NEEDS_REVIEW(live_call_unreconciled)`，`held=114700 spent=0 provider_calls=0`。

### 影响

一次数据库抖动或 worker 重启就让任务转人工，冻结金额没有对账入口可以释放（S-033）。方向保守，不会多花钱，但「预留要能释放」被破坏，项目预算被永久占用。

### 定级理由

P1。

---

## P1-05 验收器不绑定生成时的快照，也不识别矛盾资料

**分类**：已证实缺陷（2 个用例）。**场景**：S-051、S-053；潜在 S-052；需要真实环境 S-057、S-058。**不变量**：INV-TRUTH-01、INV-TRUTH-03。

### 根因

1. 验收时重新调用 `_source_snapshot`（`goal_effect.py:574`）；发送时的快照哈希只记在 `model_requests.usage_detail`（`budget.py:343-348,429-435`），验收既不读也不比较。
2. `required_facts` 取所有与目标有词重叠的资料行（`research.py:147-163`），`goal_completion_detail` 只要求答案覆盖每一行、且不出现资料外的数字（`research.py:388-409`）。资料互相矛盾时，同时复述两个值的答案满足全部条件；而 docs/14 D 节要求「对明显矛盾证据给出 UNVERIFIABLE / 人工复核」。

### 复现与实际输出

`repro/test_p1_05_verifier_evidence.py`：

- 生成答案之后、验收之前上传一份冲突资料：`模型只看到一份资料并正确作答（调用 1 次）；验收用的快照 6f8f5015e268 ≠ 发送时 02bd89459d14；判定 FAIL（missing_fact），任务 FAILED`。
- 两份资料分别写 60 和 90，答案两个都写：`任务 SUCCEEDED，验收 [{'verdict': 'PASS', ...}]`。

### 影响

验收结论取决于验收那一刻的资料，而不是模型看到的资料；矛盾资料下自相矛盾的答案被判 SUCCEEDED。叠加 P0-01（被更正的旧记忆与新记忆就是一对矛盾资料），正常使用中就会出现。

### 定级理由

P1：谎报成功，需要资料变化或资料矛盾作为条件。

---

## P1-06 可信评分器可被候选代码伪造为 PASS

**分类**：已证实缺陷（1 个用例 + 1 个对照，需要 Docker）。**场景**：S-054；潜在 S-055。**不变量**：INV-TRUTH-02。

### 根因

评分结果由子进程里的 `Report` 插件经 `json.dumps` 写进管道，候选模块在同一个子进程里被 import：

```python
# services/worker/aether_worker/verification.py:228,239（容器内子进程源码）
"sys.path.append('/work/src')\n"
"os.write(fd, json.dumps({'exit_code': Report.exit_code, 'passed_count': Report.passed_count}).encode())\n"
# verification.py:262-268（父进程）
report = json.loads(raw.decode().strip().splitlines()[-1])
ok = int(report.get("exit_code", 1)) == 0 and int(report.get("passed_count", 0)) >= 1
del completed      # 子进程退出码被丢弃
```

候选在 import 时替换 `json.dumps`（或 `os.write`、`Report` 的类属性）就能改写结果；父进程只认管道最后一行，不核对子进程退出码，也不独立统计通过数。docs/06 §1 写着「这些情况一律不能当成通过」，实际不成立。

### 复现与实际输出

`repro/test_p1_06_grader_forgery.py`：候选 `VALUE = 0`，绑定套件断言 `app.VALUE == 1`。候选模块顶部改写 `json.dumps`：`评分器却给出 PASS：绑定套件通过且哈希一致`。同一候选去掉改写的对照组：FAIL（通过）。

### 影响

`trusted_test_suite` 一旦接入产品路径（编码任务），任何候选都能自证通过。目前评分器没有产品调用方。

### 定级理由

P1：信任锚组件被攻破；当前未接线，所以不是 P0。

---

## P1-07 工作流改版没有 patch 保护，旧历史回放失败

**分类**：已证实缺陷（1 个用例，真实 Temporal + `Replayer`）。**场景**：S-013；潜在 S-014。**不变量**：INV-STAB-03。

### 根因

控制命令分支在 4ba3b2f 之后引入，没有 `workflow.patched` 守卫：

```python
# services/worker/aether_worker/durable_workflow.py:81-85
if kind in _CONTROL:
    controlled = await workflow.execute_activity(apply_task_control, activity_input, ...)
```

4ba3b2f 的工作流对每条命令都执行 prepare + execute（`tests/fixtures/durable_workflow_4ba3b2f.py`）。任务工作流从不 close（`durable_workflow.py:73`），旧版本启动的工作流在升级后仍在跑，重放（worker 重启、缓存驱逐）时指令序列对不上。现有 `tests/integration/test_workflow_replay.py` 只回放了只含 start 的历史。48281f3 对 `_drain_controls` 的改动同样没加 patch（S-014）。

### 复现与实际输出

`repro/test_p1_07_history_replay.py`：用 4ba3b2f 工作流跑一个收到 start + pause 的任务，取历史后用当前工作流回放：`NondeterminismError: ... Activity type of scheduled event 'execute_task' does not match activity type of activity command 'apply_task_control'`。

### 影响

持久化 Temporal 下升级，所有收到过暂停/恢复/取消的旧任务工作流会卡在不断失败的工作流任务上。

### 定级理由

P1。

---

## P1-08 Alembic 迁移链与 bootstrap 建库脚本漂移

**分类**：已证实缺陷（1 个用例）。**场景**：S-072。**不变量**：INV-STAB-04。

### 根因

`budget_accounts.tokens_held`、`resource_heads` 表及其 RLS 策略和授权只在 `migrations/sql/0005_runtime_patches.sql` 里；`ensure_database` 会执行它（`bootstrap.py:91`），但没有任何 Alembic 版本执行它（`migrations/versions/0005_control_grants.py` 只有两条 GRANT）。`tests/integration/test_migration.py` 只检查了 `projects` 表。

### 复现与实际输出

`repro/test_p1_08_migration_drift.py`：新建空库执行 `alembic upgrade head`，与 `ensure_database` 建出的 `aether` 库比对列、策略和授权。实际：`alembic head 缺少：{'columns': [('budget_accounts', 'tokens_held'), ('resource_heads', ...)], 'policies': [('resource_heads', 'resource_heads_project_scope')], 'grants': [... resource_heads ...]}`，多出的为空。

### 影响

按 docs/03 声明的「增量迁移与升级路径」建出的库上，预算账本读 `tokens_held` 的语句（`budget.py:69,359,579`）全部报错，所有 live 研究任务失败；资源版本预检（`operations.py:91-99`）也不可用。

### 定级理由

P1。

---

## P1-09 备份/恢复会毁数据，也不能重建

**分类**：已证实缺陷（3 个用例）。**场景**：S-073、S-074、S-075；需要真实环境 S-077。**不变量**：INV-DATA-04、INV-STAB-05。

### 根因

1. 备份只含 4 张表（`supply.py:15`：`SBOM_TABLES = ("projects", "artifacts", "tasks", "command_outbox")`），不含 owners、goal_contracts、verification_results、budget_accounts、model_requests、task_artifacts、memories、task_events 等。
2. 恢复是逐行 upsert 合并（`supply.py:126-158,196-199`），不是时间点还原；备份之后进入终态的任务被终态守卫拒绝，整个恢复事务回滚。
3. 产物目录用 `_install_tree` 整体换成备份副本，并删除现有目录（`supply.py:81-108`，`shutil.rmtree(previous)` 在 108 行）；备份之后写入的字节被删，而它们的数据库行因为是合并恢复仍然存在。

### 复现与实际输出

`repro/test_p1_09_backup_restore.py`：

- 备份 → 新上传 → 恢复：读取新产物 `FileNotFoundError: [Errno 2] No such file or directory: '.../artifacts/<project>/..'`（API 路径上即 500）。
- 备份 → 任务完成 → 恢复：`RaiseException: terminal task cannot return to schedulable status QUEUED`，恢复整体中止。
- 新库恢复：`备份只含 ['projects', 'artifacts', 'tasks', 'command_outbox']，缺 [...8 张表...]；在新库恢复：ForeignKeyViolation ... "projects_owner_id_fkey"`。

### 影响

灾难恢复（换新库）做不到；在原库上恢复要么删掉新数据，要么直接失败。

### 定级理由

P1。

---

## P2-01 证据与账目标签失真

**分类**：已证实缺陷（3 个用例）；需要真实环境 S-026。**场景**：S-024、S-025、S-056。**不变量**：INV-TRUTH-05、INV-MONEY-04。

- 验收报告把运行模式写死：`goal_effect.py:641-642` 的 `"model_mode": "scripted"`、`"offline": True`。live 运行实测输出：`真实调用了 1 次 live 传输，报告写的是 model_mode=scripted offline=True`。
- 缺失用量被记成超额：`record_model_response` 在 token 用量为空时落到 else 分支（`budget.py:498-510`，`tokens_fit` 为 False）。实测：`usage_status=observed book_result=overrun`，任务 `NEEDS_REVIEW(budget_unreconciled)`「模型回报超出预留」。
- 本地估算被标成供应商回执：网关读 LiteLLM 的 `_hidden_params["response_cost"]`（`model.py:24-37`），这是 LiteLLM 按**上游自报的模型名**查本地价目表得到的估算；账本却写 `cost_source: provider_response`（`budget.py:519`）。用本机伪造的 OpenAI 兼容服务（响应里没有任何费用字段、自报 `model=gpt-4o`）实测：`usage_status=known actual=225 cost_source=provider_response`。补充探测脚本 `repro/probe_litellm_cost.py`（输出 `repro/output/litellm_cost_probe.txt`，LiteLLM 1.105.1，用量固定 1000/500 token）：无论请求的别名是什么，上游自报 `gpt-4o-mini` 时 `response_cost=0.00045`，自报 `gpt-4o` 时 `0.0075`，自报 `audit-model` 或 `deepseek-chat` 时为 None。也就是说，费用完全由上游自己声称的模型名决定。

影响：审计证据声称离线运行；缺用量被误报为超额；「已知花费」其实是本地估算，上游自报便宜模型名时会少记。少记多少需要和真实账单比对（S-026），若差额显著应升为 P1。

---

## P2-02 公开投影不可信、终态不进事件流

**分类**：已证实缺陷（2 个用例）。**场景**：S-070、S-018。**不变量**：INV-TRUTH-04、INV-PROJ-02。

- `PUT /contract` 只核对 If-Match 版本，任何状态都能改 `draft_contract`，内容（包括客户端给的 `contract_sha256`）原样保存（`control.py:112-129,507-527`）；`_snapshot` 把它当成公开的合同哈希（`control.py:440`），而 worker 实际使用 `goal_contracts`。实测：任务 SUCCEEDED 后 `PUT /contract → 200`，公开视图 `contract_sha256=ffff…`，权威合同与验收绑定都是 `ccc67fad…`。
- 状态变化不写事件：`_set_status` / `_commit_terminal`（`goal_effect.py:1296-1364`）只更新 `tasks`，只有 running、needs_review、budget_exhausted、research.call 会写事件。实测：任务 SUCCEEDED 后事件流只有 `['task.accepted', 'task.running', 'research.call']`。

影响：UI 和外部集成看到伪造的合同哈希；SSE 断线重连永远看不到终态（docs/14 A 节要求状态由单调事件投影）。

---

## P2-03 事件序号竞态

**分类**：已证实缺陷（1 个用例）。**场景**：S-017。**不变量**：INV-PROJ-01。

API 与 worker 都用 `SELECT coalesce(max(sequence),0)+1` 再插入（`control.py:359-382`，`goal_effect.py:1396-1421`，`research.py:449-472`），没有行锁或序列，并发时撞 `UNIQUE(project_id, task_id, sequence)`（`schema.sql:102`）。实测：worker 事件事务未提交时，API 的命令事件 `IntegrityError ... "task_events_project_id_task_id_sequence_key"`。影响：API 命令偶发 500（命令没被接受）；worker 一侧变成活动失败重试。

---

## P2-04 技能生命周期：回滚经 API 不可达，晋升闸门不绑定

**分类**：已证实缺陷（2 个用例）；潜在 S-007、S-066、S-068。**场景**：S-065、S-067。**不变量**：INV-LEARN-01、INV-AUTH-04、INV-LEARN-03。

- `_rollback` 需要 payload 里的 `task_id`（`console_commands.py:355-358`），API 的部署请求体只接受固定 6 个字段，不含 `task_id`（`console_routes.py:627-647`）。实测：存在 canary 时经 API 回滚，`回滚命令状态=FAILED，canary 仍指向 v2`。
- 晋升只检查「存在某个 verifier 写的评测行」「版本 ELIGIBLE」「存在某个已批准未过期的审批」（`console_commands.py:304-323`），不检查评测的候选是不是这个版本、是否通过，不检查审批意图，也不消费审批；请求里的 `report_sha256` 从不核对。实测：用一个发邮件意图的审批、一个评测别的版本且证据不足（INSUFFICIENT_EVIDENCE）的评测，`命令=DELIVERED canary=v2`。
- `skill.evaluate` 只建 QUEUED 评测行，没有执行者（`console_commands.py:521-553`），所以产品里版本到不了 ELIGIBLE，晋升链目前走不通；回滚不可达是已证实的功能缺陷，晋升闸门是潜在面。

---

## P2-05 模型令牌不绑任务、不经账本、任务结束后仍可用

**分类**：已证实缺陷（1 个用例，机制）；潜在 S-029；需要真实环境 S-078。**场景**：S-028。**不变量**：INV-AUTH-03、INV-MONEY-05。

`issue_token` 只记 project、capabilities、aliases，10 分钟有效，可重复使用（`model.py:60-77`）；`complete` 不查预算、不计次（`model.py:78-146`）；任务结束与取消都不撤销（`goal_effect.py:310-322`）。预算只由调用方 `_ensure_live_answer` 执行。实测：任务 SUCCEEDED 后用旧令牌 `又成功调用了 3 次供应商（传输共 4 次），账本 requests_admitted 仍是 1`。令牌目前只在 worker 进程内存里；按规格把令牌交给 OpenHands 沙箱之后，令牌泄露即可在 10 分钟内绕过预算调用（docs/14 B 节要求「token 窃取仅允许消耗本 job 剩余预算」）。

---

## P2-06 纵深防线缺口：SSRF 地址段、DB 终态守卫、MCP argv 过滤

**分类**：已证实缺陷（7 个用例）；潜在 S-060。**场景**：S-059、S-062、S-063。**不变量**：INV-UNTRUST-02、INV-STOP-01、INV-UNTRUST-04。

- `ingest._blocked` 用 `is_private / is_loopback / is_link_local / is_multicast / is_reserved / is_unspecified` 黑名单（`ingest.py:58-66`），不用 `is_global`。实测 `100.100.100.200`（阿里云元数据地址）、`100.64.0.1`、`fec0::1` 都通过 `assert_public_target`。
- 终态守卫只拦「终态 → 可调度状态」（`task_status_guard.sql:8-12`）。实测：`aether_worker` 角色把 CANCELLED 改成 SUCCEEDED，数据库没有拦。`OperationLedger._mark_task` 又是无条件 UPDATE（`operations.py:214-216`）。
- `_validate_argv` 只拒绝 `-c`/`--command` 和几个 shell 元字符（`mcp_client.py:25-30`）。实测 `sh -ec …`、`bash -lc id`、`node -e …` 都放行；AGENTS.md 明确「shell 正则过滤不是安全边界」。

三项目前都不在产品主路径（采集与 MCP 未接线，终态互改没有代码路径），属于纵深防线；接线之后就是真实缺口。

---

## P2-07 沙箱取消不停止容器

**分类**：已证实缺陷（1 个用例，需要 Docker）。**场景**：S-061。**不变量**：INV-STOP-03。

`LinuxRunner.cancel` 只把本地 JSON 记录改成 CANCELLED（`runner.py:339-344`），不停止容器、不撤销作业令牌；`release_containers` 存在但 cancel 不调用。实测：`cancel 返回 CANCELLED，作业容器 … 运行状态=true`。runner 尚未接入产品路径；接线后取消不会停止正在执行的不可信代码。

---

## P2-08 `ensure_database` 默认轮换全部角色口令

**分类**：已证实缺陷（1 个用例）。**场景**：S-076。**不变量**：INV-STAB-06。

`rotate_passwords=True` 是默认值（`bootstrap.py:54-81`），README 的建库命令就用默认值（`README.md:80`）；`Database` 用 NullPool，每个事务都重新连接（`db.py:18`）。实测：再次执行 `ensure_database` 后，已在运行的 API 角色 `password authentication failed for user "aether_api"`（用例结束时把口令恢复成会话 fixture 的值）。影响：升级时重跑建库/授权会让在跑服务全部断库，需要拿新口令重启。

---

## P2-09 产品路径未接通与规格偏离（静态证据汇总）

**分类**：潜在风险（代码证据，无复现用例）。这些不是本轮复现的缺陷，而是会让上面的不变量在接线后失效、或与规格冲突的事实，供需求方排期：

1. 采集任务、编码作业、技能评测只建行不执行（`console_commands.py:213-229,459-518,521-553`；全仓没有消费者）。
2. `POST /search` 固定返回 503（`console_routes.py:142-158`）。
3. 跟踪无法激活：`watch.patch` 拒绝 ACTIVE（`console_commands.py:399-407`），`enable_watch` 只在库里。
4. 工具注册表、MCP 客户端、操作账本的 `dispatch` / `reconcile` 没有产品调用方（S-060）。
5. NEEDS_REVIEW 没有出口：resume 只接受 PAUSED（`goal_effect.py:297-309`），API 命令只有 approve_goal / pause / resume / cancel（`control.py:23`），approve_goal 在工作流里只走一遍 prepare 然后 ignored。docs/04 §2 要求「NEEDS_REVIEW 必须人工解决后回原阶段」。
6. worker 自动以 owner 名义批准合同（`goal_effect.py:407-426`），与 docs/04 §1「不能虚构用户同意」冲突（S-071）。
7. 验收器凭证、供应商 key、Docker 控制权都在 worker 进程（`service.py:90-110`，`goal_effect.py:570`），与 docs/02 §1、§7「分开进程/凭证」不符。
8. `ArtifactStore.recover()` 没有产品调用方（S-041）；`get()` 单次 `os.read` 可能短读（S-042）。
9. 工作流永不结束、没有 continue-as-new（S-015）；SDK 默认 `ALLOW_DUPLICATE`（S-016）；投递器没有 lease、退避和上限（S-010）。
10. `active_seconds` 按墙钟计时并包含暂停（ADR-F21），与 docs/04 §3「等待不消耗 active execution budget」冲突（S-032）。
11. 来源删除不递增 visibility epoch，检索缓存可返回已删除来源（S-048）；推荐 GET 会写画像与曝光，并与记忆删除的画像版本分配竞态（S-006）。
12. 审批决定失败后幂等键卡在 `response_status=0`（S-019）；overrun 之后没有对账入口（S-033）。
13. 沙箱镜像用可变标签 `python:3.12-alpine`，评分镜像运行时 `pip install pytest==8.3.5` 不校验哈希（S-064）。
14. 所有者可以直接删除验收报告和答案产物，任务仍 SUCCEEDED（S-043）。

---

## 需要真实环境才能判断的项

| 场景 / 不变量 | 需要什么 | 本轮已证实的部分 |
|---|---|---|
| S-023 sending 后崩溃 | 真实供应商：断线请求是否计费、能否按请求 ID 对账 | 代码按 unknown + 冻结处理（保守） |
| S-026 上游自报便宜模型名 | 真实账单与 LiteLLM 价目表比对 | 账本把本地估算当已知花费（P2-01） |
| S-027、INV-MONEY-03 tokenizer 差异 | 真实供应商的 token 计数 | 本地估算与超额兜底逻辑 |
| S-034 截断输出 | 真实模型的 `finish_reason=length` 行为 | 网关不读 `finish_reason` |
| S-035 非超时网络错误 | 真实供应商在 5xx / 连接重置时是否计费 | 代码记 unknown |
| S-057、INV-UNTRUST-01 提示注入 | 真实模型是否服从资料里的指令 | 验收器会放行复述矛盾值的输出（P1-05） |
| S-058 验收启发式误判率 | 大量真实模型输出 | 启发式规则本身 |
| S-077 恢复后与 Temporal 对账 | 持久化 Temporal + 真实备份流程 | 恢复是合并而非还原（P1-09） |
| S-078 OpenHands 令牌窃取 | OpenHands SDK 与真实网关接线 | 令牌不绑预算（P2-05） |
| S-079 生产 Temporal 历史上限 | 生产 Temporal 集群 | 工作流永不结束（S-015） |

## 交付物校验

- 本目录所有文件的 SHA-256 列在 `SHA256SUMS`（`sha256sum -c SHA256SUMS` 可复核）。
- 对本目录新增的全部 43 个文件（含 `SHA256SUMS`）做了与 `BASELINE.md` §5 相同的密钥正则扫描（另加 `aether_*:<口令>@` 角色连接串规则）：命中 6 处，全部是 `BASELINE.md` §5 表格和本节为说明而引用的仓库既有占位符（`postgres:postgres`、`postgres:admin-secret`、`sk-live-connector-secret`、`sk-test-secret-not-for-logs`）。运行输出里没有角色连接串和口令（角色口令每次 `ensure_database` 随机生成，输出只出现用户名）；运行命令里的管理员口令写成 `***`。复现用例里唯一的 key 字面量是假值 `synthetic-audit-key`，与 `tests/integration/test_audit_round4.py` 的同名常量一致。
- 本轮没有修改任何产品代码、测试、迁移、`STATUS.json` 或 `reports/HANDOFF.md`。
