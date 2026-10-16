# 不变量清单（基线 `d6ed382`）

每条都写成可以直接测的句子，并标出负责组件、代码位置和本轮状态。状态只有四种：

- **守住**：本轮有实测证据（基线 347 项或 `repro/test_c00_controls.py` 对照组）。
- **已证实违反**：`repro/` 里有在真实 PostgreSQL / Temporal / Docker 上失败的用例，编号对应 `FINDINGS.md`。
- **潜在风险**：代码证据充分，但本轮没有构造出触发条件，或组件还没接到产品路径。
- **需要真实环境**：结论取决于真实模型、真实供应商账单、生产 Temporal 等本轮没有的条件。

共 **52 条**：守住 12 条，已证实违反 31 条，潜在风险 7 条，需要真实环境 2 条。

## 一、不重复

| ID | 不变量 | 负责组件 | 代码位置 | 状态 |
|---|---|---|---|---|
| INV-DUP-01 | 一次逻辑模型请求最多派发一次；重试、恢复、暂停后继续都复用已记录的回执 | `BudgetLedger.prepare_model_call` / `record_model_response`，`_ensure_live_answer` | `packages/storage/aether_storage/budget.py:322-459,461-573`；`services/worker/aether_worker/goal_effect.py:1004-1092` | 守住（`tests/integration/test_audit_round4.py`、`test_audit_round5.py`） |
| INV-DUP-02 | 同一 `command_id` 在工作流里只生效一次 | `DurableTaskWorkflow.command` | `services/worker/aether_worker/durable_workflow.py:36-42` | 守住（`test_workflow.py::test_workflow_deduplicates_commands`） |
| INV-DUP-03 | 同一 Idempotency-Key + 同载荷返回同一响应；不同载荷 409；失败不留下"永远未完成"的键 | `control._claim_idempotency` / `_store_idempotency` | `services/api/aether_api/control.py:326-356,251-277` | 潜在风险：审批决定在 `decide` 抛错后键停在 `response_status=0`（S-019） |
| INV-DUP-04 | 外部写操作先落 DISPATCHED 再发送；UNKNOWN 不自动重发 | `OperationLedger.dispatch` | `packages/storage/aether_storage/operations.py:78-150` | 守住（`test_operations.py`；组件未接产品路径） |
| INV-DUP-05 | 同一任务同一类产物与验收结果在重试时不重复写 | `_bind_task_artifact`，`_ensure_verification` 缓存 + advisory lock | `goal_effect.py:483-529,620-696` | 守住 |
| INV-DUP-06 | 同一 task 只有一个工作流运行；重复启动当作重复提交 | `dispatcher._signal` | `services/worker/aether_worker/dispatcher.py:74-82` | 潜在风险：SDK 默认 `ALLOW_DUPLICATE`，工作流一旦结束/被终止，后续命令会重新启动一次运行（S-016） |

## 二、不越权（停止、顺序、权限）

| ID | 不变量 | 负责组件 | 代码位置 | 状态 |
|---|---|---|---|---|
| INV-STOP-01 | 终态（SUCCEEDED/FAILED/CANCELLED）不能被复活，也不能改写成另一个状态 | `task_status_guard` 触发器，`_update_status` 的 allowed 列表 | `migrations/sql/task_status_guard.sql:5-20`；`goal_effect.py:1366-1393`；`operations.py:214-216` | 已证实违反（DB 层，P2-06） |
| INV-STOP-02 | 暂停/取消被 API 接受后，不再发出新的模型调用（已发出的允许收回执） | `_block_execution`，`acquire/consume_dispatch_permit`，API 写 `resume_state`，dispatcher 顺序 | `goal_effect.py:1041-1050,1269-1293`；`budget.py:212-279`；`control.py:154-174`；`dispatcher.py:84-116` | 已证实违反（P1-03） |
| INV-STOP-03 | 取消编码作业要停止容器进程树并撤销作业令牌 | `LinuxRunner.cancel` | `services/worker/aether_worker/runner.py:339-344` | 已证实违反（P2-07；组件未接产品路径） |
| INV-ORDER-01 | 同一任务的命令按 API 接收顺序送达并生效 | `dispatcher._claim` / `deliver_pending` | `dispatcher.py:30-56,84-116` | 已证实违反（P1-03） |
| INV-AUTH-01 | 跨项目访问一律拒绝（API 所有权检查 + RLS + 业务校验） | `control._owned`，schema RLS，`_source_snapshot` | `control.py:301-309`；`contracts/schema.sql:419-609`；`migrations/sql/0002_privileges.sql`；`goal_effect.py:789-801` | 守住（`test_c00_controls.py` 2 项，`test_rls.py`） |
| INV-AUTH-02 | API 角色不能写验收证据、预算账本，也不能把任务推进到运行/终态 | `0003_grants.sql`，`task_status_guard` | `migrations/sql/0003_grants.sql:26-47`；`task_status_guard.sql:13-17` | 守住（`test_c00_controls.py`，`test_privileges.py`） |
| INV-AUTH-03 | 模型令牌只服务于一个任务的已记账调用，任务结束或取消即失效 | `ModelGateway.issue_token` / `_scope` | `services/gateway/aether_gateway/model.py:60-77,148-164` | 已证实违反（P2-05） |
| INV-AUTH-04 | 技能晋升只能使用为这次晋升签发、尚未消费的 owner 审批，以及针对该候选版本、已通过的评测 | `console_commands._skill_deploy` | `services/worker/aether_worker/console_commands.py:297-352` | 已证实违反（P2-04） |
| INV-AUTH-05 | `/v1/admin/*` 依赖单所有者不变量，第二个所有者无法创建 | `one_owner_v1` 唯一索引 | `contracts/schema.sql:14` | 守住（`test_auth.py`） |

## 三、不谎报成功

| ID | 不变量 | 负责组件 | 代码位置 | 状态 |
|---|---|---|---|---|
| INV-TRUTH-01 | 验收绑定生成答案时的不可变来源快照；资料之后变化只能让验收不可判定或重做 | `_ensure_verification` / `_source_snapshot` | `goal_effect.py:558-581`（574 重新取快照）；`budget.py:343-348`（发送时快照只记在调用账本） | 已证实违反（P1-05） |
| INV-TRUTH-02 | 候选代码不能把失败的绑定套件变成 PASS | `verification.grade` 与容器内 `_GRADER_PROGRAM` | `services/worker/aether_worker/verification.py:129-268` | 已证实违反（P1-06；组件未接产品路径） |
| INV-TRUTH-03 | 只有获准、仍可见、已确认的资料能作为验收依据；互相矛盾的证据只能 UNVERIFIABLE | `_source_snapshot`，`research.required_facts` / `goal_completion_detail` | `goal_effect.py:779-787`；`services/worker/aether_worker/research.py:147-163,388-418` | 已证实违反（P0-01、P1-05） |
| INV-TRUTH-04 | 对外展示的合同哈希就是验收绑定的权威合同；批准后的合同不可改写 | `control._snapshot` / `revise_contract` | `control.py:112-129,434-441` | 已证实违反（P2-02） |
| INV-TRUTH-05 | 证据元数据如实：运行模式、费用来源、用量状态 | `_ensure_verification` 报告，`record_model_response`，`ModelGateway._cost_micro_usd` | `goal_effect.py:635-649`；`budget.py:496-524`；`model.py:24-37` | 已证实违反（P2-01） |
| INV-TRUTH-06 | 审批记录真实反映 owner 的决定，系统不代签 | `_ensure_contract` | `goal_effect.py:407-426` | 潜在风险（规格偏离，S-077） |

## 四、不算错钱

| ID | 不变量 | 负责组件 | 代码位置 | 状态 |
|---|---|---|---|---|
| INV-MONEY-01 | 已收到的响应（含空正文、超额）里的已知 token 与费用全部入账 | `record_model_response` | `budget.py:461-573` | 守住（`test_audit_round5.py::test_r502_*`） |
| INV-MONEY-02 | 确定没有离开进程的预留必须释放或复用；只有进入 `sending` 之后才允许记 unknown | `prepare_model_call` | `budget.py:372-419` | 已证实违反（P1-04） |
| INV-MONEY-03 | 派发前的预留覆盖本次最坏费用与 token，且不超过上限 | `_live_dispatch_plan`，`prepare_model_call` | `goal_effect.py:871-887`；`budget.py:420-428` | 需要真实环境：本地 cl100k 与供应商 tokenizer 的差距（基线本地测试守住） |
| INV-MONEY-04 | 费用与用量来源如实：本地价目表估算不是供应商回执；缺失用量记 unknown，不记"超额" | `model._cost_micro_usd`，`record_model_response` | `model.py:24-37,116-141`；`budget.py:496-524` | 已证实违反（P2-01） |
| INV-MONEY-05 | 经过网关的每次供应商调用都进入预算账本 | `ModelGateway.complete` | `model.py:78-146` | 已证实违反（P2-05） |
| INV-MONEY-06 | 未决账单（unknown / observed）保持冻结，并在任务与费用接口可见 | 预算账本 + API 投影 | `control.py:395-449`；`console_routes.py:330-372` | 守住（R4/R5 回归） |

## 五、不坏数据、删除不复活

| ID | 不变量 | 负责组件 | 代码位置 | 状态 |
|---|---|---|---|---|
| INV-DATA-01 | 去重只共享字节，不共享角色、分类、归属和删除状态 | `ArtifactStore._insert_on` / `_live_identity`，`_bind_task_artifact` | `packages/storage/aether_storage/artifacts.py:692-746`；`goal_effect.py:493-500` | 已证实违反（P1-01） |
| INV-DATA-02 | 事务原子；产物日志的 committed 以数据库提交为准；崩溃后的暂存能被对账 | `Database.transaction`，`ArtifactStore._watch_commit` / `recover` | `packages/storage/aether_storage/db.py:20-51`；`artifacts.py:153-165,218-301` | 潜在风险：`recover()` 没有产品调用方（S-042）；提交语义本身守住 |
| INV-DATA-03 | 删除一个产物不能让之后无关的同字节写入永久失败 | `_insert_on` + `UNIQUE(project_id, storage_key)` | `artifacts.py:723-733`；`contracts/schema.sql:155` | 已证实违反（P1-01） |
| INV-DATA-04 | 恢复不毁掉恢复点之后仍被账本引用的字节；恢复能执行完 | `supply.restore_project` / `_install_tree` | `services/worker/aether_worker/supply.py:81-108,183-221` | 已证实违反（P1-09） |
| INV-DEL-01 | 删除或更正后的记忆、删除后的资料，不再进入新任务的模型上下文和验收 | `memory._delete` / `_patch`，`_source_snapshot` | `services/worker/aether_worker/memory.py:298,338,341,365-372`；`goal_effect.py:779-787` | 已证实违反（P0-01） |
| INV-DEL-02 | 删除让派生索引和缓存失效（visibility epoch），晚到写入不能复活 | `retention.invalidate_derived` / `commit_*`，检索缓存 | `services/worker/aether_worker/retention.py:31-38,47-120`；`console_commands.py:196-210` | 潜在风险：来源删除不递增 epoch，检索缓存同 epoch 内仍返回旧结果（S-049；检索未接产品路径） |

## 六、长期稳定

| ID | 不变量 | 负责组件 | 代码位置 | 状态 |
|---|---|---|---|---|
| INV-STAB-01 | 单条命令失败只把这一条标成 FAILED，不回滚同批，不打断应用器和主循环，不影响其他项目 | `apply_console_commands`，`apply_memory_commands`，`route_pending`，`serve` | `console_commands.py:41-71`；`memory.py:210-237`；`services/worker/aether_worker/service.py:39-65,139-154` | 已证实违反（P0-02） |
| INV-STAB-02 | 确定性失败的活动收敛到终态，不无限重试 | `DurableTaskWorkflow` 活动选项 | `durable_workflow.py:59-63,82,86,106-120` | 已证实违反（P1-02） |
| INV-STAB-03 | 工作流改版能回放所有仍在运行的旧历史 | `DurableTaskWorkflow`，`workflow.patched` | `durable_workflow.py:70-102` | 已证实违反（P1-07） |
| INV-STAB-04 | 所有受支持的建库/升级路径得到同一份 schema、策略和授权 | `bootstrap.ensure_database` 与 `migrations/versions/*` | `packages/storage/aether_storage/bootstrap.py:85-95`；`migrations/versions/0001-0011` | 已证实违反（P1-08） |
| INV-STAB-05 | 备份能在空库上重建出可用系统（所有者、合同、验收证据、预算、记忆） | `supply.backup_project` | `supply.py:14-19,63-78` | 已证实违反（P1-09） |
| INV-STAB-06 | 重复执行建库/授权脚本不让在跑的服务断开数据库 | `ensure_database(rotate_passwords=True)` | `bootstrap.py:54-81` | 已证实违反（P2-08） |
| INV-STAB-07 | 工作流历史有界（结束或 continue-as-new） | `DurableTaskWorkflow.run` | `durable_workflow.py:73-102` | 潜在风险（S-015） |
| INV-PROJ-01 | 并发写同一任务的事件不会让命令接收或活动失败 | `control._append_event`，`goal_effect._event` | `control.py:359-382`；`goal_effect.py:1396-1421` | 已证实违反（P2-03） |
| INV-PROJ-02 | 任务每次状态变化都写成可回放事件，SSE 重连能看到终态 | `_set_status` / `_commit_terminal` | `goal_effect.py:1296-1364` | 已证实违反（P2-02） |

## 七、抗不可信输入

| ID | 不变量 | 负责组件 | 代码位置 | 状态 |
|---|---|---|---|---|
| INV-UNTRUST-01 | 资料和模型输出里的文字不能改变权限，也不能被当成系统指令 | `research_prompt` + 验收器 | `research.py:96-98` | 需要真实环境（真实模型是否被注入；验收器对矛盾输出的放行已在 P1-05 证实） |
| INV-UNTRUST-02 | 抓取只连公网地址 | `ingest.assert_public_target` / `_blocked` | `services/worker/aether_worker/ingest.py:58-115` | 已证实违反（P2-06；采集未接产品路径） |
| INV-UNTRUST-03 | 工具权限来自本地注册表，不信 MCP annotations；schema 漂移进入隔离 | `registry.permissions_for_remote_tool`，`mcp_client.admit` | `services/gateway/aether_gateway/registry.py:22-53`；`mcp_client.py:43-52` | 守住（`tests/unit/test_mcp.py`；未接产品路径） |
| INV-UNTRUST-04 | MCP stdio 只接受固定 argv，不能借解释器的 `-c` / `-e` 执行一段 shell 文本 | `mcp_client._validate_argv` | `mcp_client.py:25-30` | 已证实违反（P2-06） |
| INV-UNTRUST-05 | 沙箱里的代码拿不到宿主文件、DB 凭证、供应商 key，也不能联网 | 评分器、技能脚本、runner 的 docker 参数 | `verification.py:157-187`；`skills.py:117-152`；`runner.py:197-269` | 守住（基线 Docker 用例：`test_verification.py`、`test_skill_sandbox.py`、`test_runner.py`） |
| INV-EGRESS-01 | 高敏资料未经批准不送云端模型 | `_source_snapshot` / `research_prompt`（无出口检查）；`memory._exportable`（未被调用） | `goal_effect.py:768-821,999`；`memory.py:166-175` | 已证实违反（P0-01） |

## 八、经验积累可回滚

| ID | 不变量 | 负责组件 | 代码位置 | 状态 |
|---|---|---|---|---|
| INV-LEARN-01 | 已上线（canary）的技能能经 owner 回滚到上一版本 | `console_commands._rollback`，`skill_eval.rollback_active`，`console_routes._deploy_body` | `console_commands.py:355-369`；`skill_eval.py:137-170`；`services/api/aether_api/console_routes.py:627-647` | 已证实违反（P2-04） |
| INV-LEARN-02 | 记忆纠错产生新版本，旧版本不再生效（包括作为资料） | `memory._patch` | `memory.py:319-362` | 已证实违反（P0-01） |
| INV-LEARN-03 | 评测只能由独立 verifier 执行并写分数；actor 不能自带分数；排队的评测会被执行 | `skill_eval.record_evaluation`，`console_commands._queue_evaluation` | `skill_eval.py:35-73`；`console_commands.py:521-553` | 潜在风险：写分数的约束守住（`test_skill_eval.py`），但排队的评测没有执行者（S-069） |
| INV-LEARN-04 | 技能晋升不会自动发生 | `skills.propose_candidate`，`config/feature_matrix.yaml` | `skills.py:205-211` | 守住（`tests/unit/test_skills.py`，`verify_freeze.py`） |

## 状态统计核对

| 状态 | 数量 | 不变量 |
|---|---|---|
| 守住 | 12 | DUP-01、DUP-02、DUP-04、DUP-05、AUTH-01、AUTH-02、AUTH-05、MONEY-01、MONEY-06、UNTRUST-03、UNTRUST-05、LEARN-04 |
| 已证实违反 | 31 | STOP-01、STOP-02、STOP-03、ORDER-01、AUTH-03、AUTH-04、TRUTH-01、TRUTH-02、TRUTH-03、TRUTH-04、TRUTH-05、MONEY-02、MONEY-04、MONEY-05、DATA-01、DATA-03、DATA-04、DEL-01、STAB-01、STAB-02、STAB-03、STAB-04、STAB-05、STAB-06、PROJ-01、PROJ-02、UNTRUST-02、UNTRUST-04、EGRESS-01、LEARN-01、LEARN-02 |
| 潜在风险 | 7 | DUP-03、DUP-06、TRUTH-06、DATA-02、DEL-02、STAB-07、LEARN-03 |
| 需要真实环境 | 2 | MONEY-03、UNTRUST-01 |
