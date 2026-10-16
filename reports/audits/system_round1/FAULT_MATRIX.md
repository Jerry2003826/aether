# 故障矩阵（不变量 × 故障时点）

每个场景只属于三类之一：**已证实缺陷**（`repro/` 里有在真实服务上失败的用例）、**潜在风险**（代码证据充分但本轮没有构造出触发条件，或组件未接产品路径）、**需要真实环境**（取决于真实模型、真实账单、生产 Temporal 等）。在本轮实测中守住的场景不放进矩阵，单独列在末尾的对照表里。

共 **79 个场景**：已证实缺陷 40，潜在风险 29，需要真实环境 10。已证实的 40 个场景对应 `repro/` 里 43 个失败用例（S-062、S-063 各有 3 个参数，S-044 与 S-047 共用一个用例）。

## 1. 时点与维度

| 代号 | 含义 |
|---|---|
| T1 | 动作前 |
| T2 | 动作中断线 |
| T3 | 返回未写库 |
| T4 | 写库后后续失败 |
| T5 | 暂停后恢复 |
| T6 | worker 任意时点重启 |
| D1 | 并发竞争 |
| D2 | 重试 |
| D3 | 超时与期限 |
| D4 | 恶意或异常输入 |
| D5 | 历史回放与升级 |
| L | 长期运行 |

证据列的路径缩写：`test_*.py` 都在 `repro/` 目录下；没有目录前缀的源文件，`budget.py`、`artifacts.py`、`operations.py`、`db.py`、`bootstrap.py` 在 `packages/storage/aether_storage/`，`model.py`、`mcp_client.py`、`registry.py` 在 `services/gateway/aether_gateway/`，`control.py`、`app.py`、`console_routes.py`、`memory_routes.py` 在 `services/api/aether_api/`，其余在 `services/worker/aether_worker/`。

## 2. 总表

单元格里是场景编号；**加粗**为已证实缺陷，*斜体*为需要真实环境，其余为潜在风险。不变量 ID 省略了 `INV-` 前缀，定义见 `INVARIANTS.md`。

| 不变量 | T1 | T2 | T3 | T4 | T5 | T6 | D1 | D2 | D3 | D4 | D5 | L |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| DUP-03 |  |  |  | 019 |  | 019 |  |  |  |  |  |  |
| DUP-06 |  |  |  | 016 |  |  |  |  |  |  |  |  |
| STOP-01 |  |  | 060 |  |  |  |  |  |  | **059** |  |  |
| STOP-02 |  |  |  |  | **009** |  | 020 | **009** |  |  |  |  |
| STOP-03 |  |  |  |  | **061** |  |  |  |  |  |  |  |
| ORDER-01 |  |  |  |  | **009** |  | 010 | **008** **009** |  |  |  |  |
| AUTH-01 |  |  |  |  |  |  |  |  |  | 069 |  |  |
| AUTH-03 |  |  |  | **028** | 029 |  |  |  |  |  |  |  |
| AUTH-04 |  |  |  |  |  |  |  | 066 |  | **065** |  |  |
| TRUTH-01 |  |  |  | **051** 052 |  | 052 |  |  |  |  |  |  |
| TRUTH-02 |  |  |  |  |  |  |  |  |  | **054** 055 |  |  |
| TRUTH-03 |  |  |  | **044** |  |  |  |  |  | **047** **053** *058* |  |  |
| TRUTH-04 |  |  |  |  |  |  |  |  |  | **070** |  |  |
| TRUTH-05 |  |  |  | 043 **056** |  |  |  |  |  |  |  |  |
| TRUTH-06 | 071 |  |  |  |  |  |  |  |  |  |  |  |
| MONEY-01 |  |  | 031 |  |  |  |  |  |  |  |  |  |
| MONEY-02 | **021** **022** 030 | 012 *023* |  |  |  | **021** **022** *023* |  |  | 012 |  |  |  |
| MONEY-03 |  |  |  |  | 032 |  |  |  | *027* 032 |  |  |  |
| MONEY-04 |  | *035* |  |  |  |  |  |  |  | **024** **025** *026* |  |  |
| MONEY-05 |  |  |  | **028** |  |  |  |  |  | *078* |  |  |
| MONEY-06 |  |  |  | 033 |  |  |  |  |  |  |  |  |
| DATA-01 |  |  |  | **037** |  |  |  |  |  | **036** 040 |  |  |
| DATA-02 |  |  |  |  |  | 041 |  |  | 042 |  |  |  |
| DATA-03 |  |  |  | **005** **038** |  |  |  | **005** |  | **039** |  |  |
| DATA-04 |  |  |  | **073** |  |  |  |  |  |  |  |  |
| DEL-01 | 050 |  |  | **044** 049 |  |  |  |  |  |  |  |  |
| DEL-02 |  |  |  | 048 |  |  |  |  |  |  |  |  |
| STAB-01 | **001** **002** **003** |  |  | **005** |  | **004** | 006 | **005** |  | **001** **002** **003** 007 |  |  |
| STAB-02 |  | 012 |  |  |  |  |  | **011** | 012 |  |  |  |
| STAB-03 |  |  |  |  |  |  |  |  |  |  | **013** 014 |  |
| STAB-04 |  |  |  |  |  |  |  |  |  |  | **072** |  |
| STAB-05 |  |  |  | **074** |  | **075** |  |  |  |  | *077* |  |
| STAB-06 |  |  |  |  |  | **076** |  |  |  |  |  |  |
| STAB-07 |  |  |  |  |  |  |  |  |  |  |  | 015 *079* |
| PROJ-01 |  |  |  |  |  |  | **017** |  |  |  |  |  |
| PROJ-02 |  |  |  | **018** |  |  |  |  |  |  |  |  |
| UNTRUST-01 |  |  |  |  |  |  |  |  |  | *034* *057* |  |  |
| UNTRUST-02 |  |  |  |  |  |  |  |  |  | **062** |  |  |
| UNTRUST-04 |  |  |  |  |  |  |  |  |  | **063** |  |  |
| UNTRUST-05 |  |  |  |  |  |  |  |  |  | 064 *078* |  |  |
| EGRESS-01 |  |  |  |  |  |  |  |  |  | **046** |  |  |
| LEARN-01 |  |  |  |  | **067** |  |  |  |  |  |  |  |
| LEARN-02 |  |  |  | **045** |  |  |  |  |  |  |  |  |
| LEARN-03 | 068 |  |  |  |  |  |  |  |  |  |  |  |

## 3. 场景明细

### A. 命令、发件箱与工作流

| ID | 不变量 | 时点/维度 | 前置条件与注入方式 | 预期 | 实际 / 判定依据 | 分类 | 证据 |
|---|---|---|---|---|---|---|---|
| S-001 | STAB-01 | T1、D4 | 同一 URL 来源经 API 提交两次（不同幂等键），再交一条无关来源；另一项目也有一条待应用命令；调用 `route_pending` | 重复的那条标 FAILED，其余 DELIVERED | `IntegrityError` 冒出 `route_pending`，两个项目的全部命令停在 PENDING | 已证实缺陷 | `test_p0_02_poison_commands.py::test_one_bad_console_command_does_not_wedge_the_batch[duplicate_source_locator]` |
| S-002 | STAB-01 | T1、D4 | 反馈引用一个不存在的 `source_id`（API 只校验 UUID 格式） | 同上 | `ForeignKeyViolation`，同上 | 已证实缺陷 | `…[feedback_for_unknown_source]` |
| S-003 | STAB-01 | T1、D4 | 同一项目注册两个同 `connector_key` 的连接器 | 同上 | `UniqueViolation`，同上 | 已证实缺陷 | `…[duplicate_connector_key]` |
| S-004 | STAB-01 | T6 | 库里已有 S-001 的毒命令；另一项目有已上传资料和一个任务；按文档启动 `python -m aether_worker`（真实 PG + Temporal） | 进程存活，另一项目任务 SUCCEEDED | 进程以退出码 1 退出（`IntegrityError` 栈），另一项目任务停在 RUNNING；再启动同样退出 | 已证实缺陷 | `test_p0_02_poison_commands.py::test_documented_worker_survives_a_duplicate_source_and_keeps_running_tasks` |
| S-005 | STAB-01、DATA-03 | T4、D2 | 上传并删除一段文本，再创建同文本的记忆和另一条无关记忆 | 第一条 FAILED，第二条 DELIVERED | `ArtifactError: tombstone_retained` 冒出，两条记忆命令都停在 PENDING | 已证实缺陷 | `test_p0_02_poison_commands.py::test_memory_claim_matching_deleted_bytes_does_not_wedge_memory_commands` |
| S-006 | STAB-01 | D1 | 记忆删除触发 `invalidate_derived` 以 max+1 写 `profile_snapshots`，同时 `GET /recommendations` 首次以版本 1 建画像 | 两边都成功 | 两边可能撞 `UNIQUE(project_id, profile_version)`；worker 一侧的 `IntegrityError` 等同毒命令 | 潜在风险 | `retention.py:66-80`；`recommendations.py:23-39` |
| S-007 | STAB-01 | D4 | 存在 canary、回滚命令带 `task_id`，但该任务没有 skill pin 合同 | 回滚 FAILED 并写明原因 | `rollback_active` 的 `scalar_one()` 抛 `NoResultFound`（不是 `SkillEvalError`），整批回滚；当前 API 不传 `task_id`，先被 `deploy_rejected` 拦下，补上之后就会触发 | 潜在风险 | `skill_eval.py:162-169`；`console_commands.py:355-369` |
| S-008 | ORDER-01 | D2 | 任务 start 后依次提交 pause、resume；pause 的信号第一次投递抛 `ConnectionError` | 送达顺序 start、pause、resume | 实际 start、resume、pause | 已证实缺陷 | `test_p1_03_command_order.py::test_dispatcher_keeps_per_task_order_when_one_signal_fails` |
| S-009 | STOP-02、ORDER-01 | T5、D2 | 真实 Temporal：任务已 PAUSED；用户先 cancel 再 resume；cancel 的信号第一次投递失败 | CANCELLED，0 次模型调用 | resume 先送达；API 处理 resume 时已把 `cancel_requested` 清成 NULL；任务恢复执行，**每次都发生 1 次模型调用**；最终状态取决于补投的 cancel 与执行完成谁先到（`output/p1_03_repeat.txt` 重复 5 次：4 次 CANCELLED、1 次 SUCCEEDED） | 已证实缺陷 | `test_p1_03_command_order.py::test_cancel_then_resume_never_runs_the_model_when_a_signal_is_retried` |
| S-010 | ORDER-01 | D1 | 两个 dispatcher 循环同时跑，或一次投递超时后下一轮重领 DELIVERING 行 | 一条命令只投递一次 | `_claim` 不看 lease，DELIVERING 可被并发重领，重复信号只靠工作流 `command_id` 去重；没有退避和上限，毒行每 0.25 秒重试一次 | 潜在风险 | `dispatcher.py:30-56,84-116` |
| S-011 | STAB-02 | D2 | 真实 Temporal：被 tombstone 的答案字节让 `execute_task` 每次抛 `ArtifactError` | 尝试次数有界，任务 FAILED 或 NEEDS_REVIEW | 16 秒内已到第 5 次尝试，任务仍 RUNNING | 已证实缺陷 | `test_p1_02_unbounded_retry.py` |
| S-012 | STAB-02、MONEY-02 | T2、D3 | `execute_task` 超过 `start_to_close=60s`（慢库或慢供应商），Temporal 发起下一次尝试而上一次线程还在跑 | 同一请求只有一个执行者 | 新尝试在 `prepare_model_call` 看到 reserved 就记 unknown 并 NEEDS_REVIEW；旧线程仍可能发出请求并写回执 | 潜在风险 | `durable_workflow.py:106-120`；`goal_effect.py:113-123`；`budget.py:411-419` |
| S-013 | STAB-03 | D5 | 用 `tests/fixtures/durable_workflow_4ba3b2f.py` 跑一个收到 start + pause 的任务，取历史，用当前工作流 `Replayer` 回放 | 回放通过 | `NondeterminismError`：历史里是 `execute_task`，当前代码排的是 `apply_task_control` | 已证实缺陷 | `test_p1_07_history_replay.py` |
| S-014 | STAB-03 | D5 | 0359da5 时期 `pending=[approve_goal, pause]` 时记录的历史 | 回放通过 | 48281f3 把 `_drain_controls` 改成抽取全部控制命令，没有 `workflow.patched` 保护，指令序列可能不同 | 潜在风险 | `git diff 0359da5 48281f3 -- services/worker/aether_worker/durable_workflow.py` |
| S-015 | STAB-07 | L | 任务结束后继续收到命令；任务数量持续增长 | 历史有界 | 工作流永不收到 `close`，没有 continue-as-new，开放工作流数随任务数线性增长 | 潜在风险 | `durable_workflow.py:73-102` |
| S-016 | DUP-06 | T4 | 工作流被终止或结束之后又来一条 pause/resume | 按「任务已结束」处理 | `start_workflow` 使用 SDK 默认 `ALLOW_DUPLICATE`，会新开一次运行并重新进入 prepare/execute | 潜在风险 | `dispatcher.py:79`；temporalio 1.34 `Client.start_workflow` 默认 `id_reuse_policy` |
| S-017 | PROJ-01 | D1 | worker 的事件事务尚未提交时，API 的命令事务写 `command.accepted` | 两边都成功 | API 一侧 `UniqueViolation`（HTTP 路径上就是 500，命令没有被接受） | 已证实缺陷 | `test_p2_03_event_sequence.py` |
| S-018 | PROJ-02 | T4 | 任务 SUCCEEDED 之后读取 SSE | 有终态事件 | 只有 `task.accepted`、`task.running`、`research.call` | 已证实缺陷 | `test_p2_02_projection.py::test_terminal_status_is_visible_in_the_event_stream` |
| S-019 | DUP-03 | T4、T6 | 审批决定时 `decide` 抛 `OperationError`，或进程在 `_store_idempotency` 之前崩溃 | 同键重试拿到原结果或明确可重试 | 幂等键停在 `response_status=0`，同键永远 409「尚未完成」 | 潜在风险 | `control.py:251-277` |
| S-020 | STOP-02 | D1 | API 的 resume 把 `resume_state` 置 NULL，与 worker `_block_execution` 把 `pause_requested` 落成 PAUSED 同时发生 | 最终状态与命令顺序一致 | 两个写入方没有共同版本号，可能得到 PAUSED + `resume_state=NULL`，而 resume 命令又因「不是 PAUSED」被忽略 | 潜在风险 | `control.py:154-174`；`goal_effect.py:1283-1292,297-309` |

### B. 预算与模型调用

| ID | 不变量 | 时点/维度 | 前置条件与注入方式 | 预期 | 实际 / 判定依据 | 分类 | 证据 |
|---|---|---|---|---|---|---|---|
| S-021 | MONEY-02 | T1、T6 | `prepare_model_call` 之后 `issue_token` 抛错，活动重试 | 复用预留，调用 1 次，SUCCEEDED | 记 unknown，held=114700 永久冻结，0 次调用，NEEDS_REVIEW(`live_call_unreconciled`) | 已证实缺陷 | `test_p1_04_unsent_reservation.py[issue_token]` |
| S-022 | MONEY-02 | T1、T6 | `acquire_dispatch_permit` 之后 `consume_dispatch_permit` 抛错（permitted），重试 | 同上 | 同上 | 已证实缺陷 | `test_p1_04_unsent_reservation.py[consume_dispatch_permit]` |
| S-023 | MONEY-02 | T2、T6 | `consume` 之后（sending）进程崩溃 | 记 unknown、冻结、转人工，不重发 | 代码按此处理（保守） | 需要真实环境 | `budget.py:411-419`；需要知道真实供应商对断线请求是否计费、能否按请求 ID 对账 |
| S-024 | MONEY-04 | D4 | 供应商不回 usage（prompt/completion tokens 为空），正文正常 | `usage_status=unknown`，金额继续冻结 | 记成 `observed` + `overrun`，任务 NEEDS_REVIEW「模型回报超出预留」 | 已证实缺陷 | `test_p2_01_evidence_labels.py::test_missing_usage_is_unknown_not_an_overrun` |
| S-025 | MONEY-04 | D4 | 本机 OpenAI 兼容服务的响应里没有任何费用字段，自报 `model=gpt-4o` | `cost_source` 标为本地估算或 unreported | `usage_status=known`，`actual_micro_usd=225`，`cost_source=provider_response` | 已证实缺陷 | `test_p2_01_evidence_labels.py::test_local_price_table_is_not_labelled_as_a_provider_receipt` |
| S-026 | MONEY-04 | D4 | 上游自报一个比实际更便宜的模型名 | 账本不能低于真实账单 | 机制同 S-025：账本按 LiteLLM 价目表记「已知花费」 | 需要真实环境 | `model.py:24-37`；差额要和真实账单比对 |
| S-027 | MONEY-03 | D3 | 真实供应商 tokenizer 与本地 cl100k 估算 | 单次调用不超过预留 | 超出时走 overrun → NEEDS_REVIEW；超出概率未知 | 需要真实环境 | `goal_effect.py:871-887` |
| S-028 | MONEY-05、AUTH-03 | T4 | 任务 SUCCEEDED 后用旧令牌再调 3 次 `complete` | 令牌失效 | 3 次都成功（传输共 4 次），`requests_admitted` 仍是 1 | 已证实缺陷 | `test_p2_05_gateway_token.py` |
| S-029 | AUTH-03 | T5 | 取消任务后检查模型令牌 | 撤销 | 取消路径只 `revoke_unsent`，不碰 `capability_grants`；令牌按 10 分钟过期 | 潜在风险 | `goal_effect.py:310-322`；`model.py:73` |
| S-030 | MONEY-02 | T1 | 发送前抛 `GatewayDenied`（`token_rejected` / `alias_denied`） | 释放预留 | `mark_unknown`，预留永久冻结 | 潜在风险 | `goal_effect.py:1053-1055`；`model.py:79-82,159-160` |
| S-031 | MONEY-01 | T3 | 用量已知，但输出里含供应商 key → `secret_leak` | 已知用量入账 | `GatewayDenied` 后 `mark_unknown`，已知用量被丢弃 | 潜在风险 | `model.py:143-145` |
| S-032 | MONEY-03 | T5、D3 | 任务暂停超过 `active_seconds` 后恢复 | 等待不消耗执行预算（docs/04 §3） | 墙钟含暂停，恢复即 FAILED(`budget_exhausted`)；ADR-F21 记录了这一取舍，与规格冲突 | 潜在风险 | `goal_effect.py:989-992`；`docs/DECISIONS.md` ADR-F21 |
| S-033 | MONEY-06 | T4 | overrun 之后 | 有对账入口，冻结金额能被确认或释放 | `reconciliation=unreconciled`，没有对账代码，held 永久保留 | 潜在风险 | `budget.py:505-524` |
| S-034 | UNTRUST-01 | D4 | 输出因 `max_tokens` 截断（`finish_reason=length`） | 截断答案不按完整答案验收 | 网关不读 `finish_reason` | 需要真实环境 | `model.py:112-141` |
| S-035 | MONEY-04 | T2 | 非超时类网络错误（连接重置、5xx）发生在请求已到达供应商之后 | 记 unknown 并冻结 | 记 `upstream_error` → unknown，行为保守；是否已计费未知 | 需要真实环境 | `model.py:96-111` |

### C. 存储与去重

| ID | 不变量 | 时点/维度 | 前置条件与注入方式 | 预期 | 实际 / 判定依据 | 分类 | 证据 |
|---|---|---|---|---|---|---|---|
| S-036 | DATA-01 | D4 | 上传一个与已有任务答案同字节的文件 | 得到新的 `project_private` 资料 | 返回原答案的 id 和 `classification=task_answer`；作为显式来源时任务 NEEDS_REVIEW(`allowed_sources_invalid`) | 已证实缺陷 | `test_p1_01_dedup_and_tombstone.py::test_uploading_a_previous_answer_gives_a_usable_project_source` |
| S-037 | DATA-01 | T4 | 删除用户上传（与别的任务答案同字节） | 别的任务答案仍可读 | 别的任务答案 `not_found` | 已证实缺陷 | `…::test_deleting_an_upload_does_not_delete_another_tasks_answer` |
| S-038 | DATA-03 | T4 | 删除任务 A 的答案后，任务 B 生成同字节答案 | B 正常收尾 | `ArtifactError: tombstone_retained` | 已证实缺陷 | `…::test_a_deleted_answer_does_not_break_a_later_identical_answer` |
| S-039 | DATA-03 | D4 | 删除后重新上传同一文件 | 新的显式写入被接受 | 422 `tombstone_retained` | 已证实缺陷 | `…::test_reuploading_deleted_bytes_is_a_new_explicit_write` |
| S-040 | DATA-01 | D4 | 上传与已有产物同字节、但 media_type 不同的文件 | 保留上传者声明的类型 | 返回并沿用旧 media_type | 潜在风险 | `artifacts.py:703-705`；`app.py:214` |
| S-041 | DATA-02 | T6 | `store.put` 写盘之后、数据库提交之前崩溃 | 启动时对账 | journal 留下 prepared；`ArtifactStore.recover()` 没有产品调用方 | 潜在风险 | `artifacts.py:153-165`（全仓 grep 无调用） |
| S-042 | DATA-02 | D3 | 读取接近 25 MiB 的产物 | 正确读出 | 单次 `os.read`，短读即报 `hash_mismatch` | 潜在风险 | `artifacts.py:173-179` |
| S-043 | TRUTH-05 | T4 | 所有者删除验收报告或答案产物 | 证据保留，或明确标为已删除 | 可以直接删除；任务仍 SUCCEEDED，`evidence_ids` 指向已删除产物 | 潜在风险 | `app.py:234-243`；`0003_grants.sql:33` |

### D. 记忆与删除

| ID | 不变量 | 时点/维度 | 前置条件与注入方式 | 预期 | 实际 / 判定依据 | 分类 | 证据 |
|---|---|---|---|---|---|---|---|
| S-044 | DEL-01、TRUTH-03 | T4 | 创建记忆「乙服务的请求超时时间是30秒。」后删除；项目里没有任何上传；提问该超时 | 提示词不含该 claim，任务不能 SUCCEEDED | 提示词的 `[资料1]` 就是被删的 claim；验收 PASS；任务 SUCCEEDED | 已证实缺陷 | `test_p0_01_memory_as_source.py::test_deleted_memory_claim_never_reaches_the_model_or_the_verifier` |
| S-045 | LEARN-02 | T4 | 记忆「90 次」更正为「60 次」后提问 | 旧 claim 不再出现 | 提示词同时含 90 和 60 | 已证实缺陷 | `…::test_corrected_memory_old_claim_is_not_ground_truth` |
| S-046 | EGRESS-01 | D4 | `sensitivity=high` 的记忆（电话号码） | 不进发往云端的提示词 | 作为 `[资料2]` 发往模型 | 已证实缺陷 | `…::test_high_sensitivity_memory_is_not_sent_to_the_cloud_model` |
| S-047 | TRUTH-03 | D4 | 未确认（proposed）的记忆 | 不作为验收依据 | 验收把它当资料行判定 PASS（与 S-044 同一用例） | 已证实缺陷 | `…::test_deleted_memory_claim_never_reaches_the_model_or_the_verifier` |
| S-048 | DEL-02 | T4 | 删除来源后在同一 epoch 内重复检索 | 不返回已删除来源 | `source.delete` 不递增 epoch，内存缓存命中旧结果 | 潜在风险 | `console_commands.py:196-210`；`retention.py:31-38`；`retrieval.py:506-507` |
| S-049 | DEL-01 | T4 | 采集接上产品路径之后删除来源 | 原文与规范文本不再进入任务 | `store_text_version` 写 `project_private` 文本，`source.delete` 不 tombstone 产物 | 潜在风险 | `ingest.py:283-284`；`console_commands.py:196-210` |
| S-050 | DEL-01 | T1 | 创建一条与现有记忆同文本的新记忆 | 返回已有记忆或明确失败 | 命令 DELIVERED，但 API 返回的 `memory_id` 永远不存在 | 潜在风险 | `memory.py:289-291` |

### E. 验收器

| ID | 不变量 | 时点/维度 | 前置条件与注入方式 | 预期 | 实际 / 判定依据 | 分类 | 证据 |
|---|---|---|---|---|---|---|---|
| S-051 | TRUTH-01 | T4 | 生成答案之后、验收之前新增一份冲突资料 | 按发送时快照验收，或判 UNVERIFIABLE | 快照不同，正确答案被判 FAIL(`missing_fact`) | 已证实缺陷 | `test_p1_05_verifier_evidence.py::test_verification_uses_the_snapshot_the_model_saw` |
| S-052 | TRUTH-01 | T4、T6 | 验收第一次因 `OSError` 失败，重试之前删掉一份资料 | 同上 | 机制同 S-051，方向也可能是把错误答案改判 PASS | 潜在风险 | `goal_effect.py:574`；`test_audit_round5.py::test_r504_*` 已证明验收可重试 |
| S-053 | TRUTH-03 | D4 | 两份资料分别写 60 和 90，答案两个都写 | UNVERIFIABLE / NEEDS_REVIEW | PASS，任务 SUCCEEDED | 已证实缺陷 | `test_p1_05_verifier_evidence.py::test_contradictory_sources_are_not_verified_as_success` |
| S-054 | TRUTH-02 | D4 | 候选 `app.py` 在 import 时改写 `json.dumps`，绑定套件断言失败 | FAIL | PASS「绑定套件通过且哈希一致」；不改写的对照组为 FAIL | 已证实缺陷 | `test_p1_06_grader_forgery.py` |
| S-055 | TRUTH-02 | D4 | 候选改写 `os.write`、`Report` 类属性，或用 gc 找到同进程对象 | FAIL | 与 S-054 同一缺口：结果通道在候选可写的同一进程里 | 潜在风险 | `verification.py:225-268` |
| S-056 | TRUTH-05 | T4 | live 运行（替换传输，调用 1 次）之后读取验收报告 | `model_mode=live`，`offline=false` | `model_mode=scripted`，`offline=true` | 已证实缺陷 | `test_p2_01_evidence_labels.py::test_live_run_report_does_not_claim_scripted_offline` |
| S-057 | UNTRUST-01 | D4 | 资料里写入提示注入，诱导真实模型输出指定文本（例如复述矛盾值） | 模型不服从；即便服从也不能验收 | 验收器放行这类输出已证实（S-053）；真实模型是否服从未知 | 需要真实环境 | `research.py:96-98` |
| S-058 | TRUTH-03 | D4 | 真实模型的同义转述、单位换算、中文数字 | 正确答案能过，错误答案不能 | 启发式 `_covers` 的误判率只能用真实输出统计 | 需要真实环境 | `research.py:306-418` |

### F. 权限、令牌、沙箱、MCP、技能

| ID | 不变量 | 时点/维度 | 前置条件与注入方式 | 预期 | 实际 / 判定依据 | 分类 | 证据 |
|---|---|---|---|---|---|---|---|
| S-059 | STOP-01 | D4 | `aether_worker` 角色把 CANCELLED 任务改成 SUCCEEDED | 数据库拒绝 | 成功 | 已证实缺陷 | `test_p2_06_defense_in_depth.py::test_database_refuses_to_rewrite_a_cancelled_task_as_succeeded` |
| S-060 | STOP-01 | T3 | non_idempotent 操作结果未知 → `_mark_task(NEEDS_REVIEW)` | 不改写终态 | 无状态条件的 UPDATE，可把 CANCELLED/SUCCEEDED 改成 NEEDS_REVIEW | 潜在风险 | `operations.py:141-146,214-216` |
| S-061 | STOP-03 | T5 | `LinuxRunner.start` 之后 `cancel` | 容器停止 | `cancel` 返回 CANCELLED，容器仍在运行 | 已证实缺陷 | `test_p2_07_runner_cancel.py` |
| S-062 | UNTRUST-02 | D4 | 目标地址 100.100.100.200、100.64.0.1、fec0::1 | 拒绝 | 全部放行 | 已证实缺陷 | `test_p2_06_defense_in_depth.py::test_fetch_rejects_non_public_addresses[*]` |
| S-063 | UNTRUST-04 | D4 | argv 为 `sh -ec …`、`bash -lc …`、`node -e …` | 拒绝 | 全部放行 | 已证实缺陷 | `test_p2_06_defense_in_depth.py::test_mcp_stdio_argv_rejects_shell_interpreters[*]` |
| S-064 | UNTRUST-05 | D4 | 上游 `python:3.12-alpine` 标签漂移；评分镜像运行时 `pip install pytest` | 固定摘要与哈希 | 可变标签，安装无哈希 | 潜在风险 | `runner.py:20`；`verification.py:90-105` |
| S-065 | AUTH-04 | D4 | 用一个发邮件的审批、一个评测别的版本且证据不足的评测去晋升 v2 | FAILED | DELIVERED，canary=v2 | 已证实缺陷 | `test_p2_04_skill_lifecycle.py::test_promotion_requires_an_approval_and_evaluation_for_this_version` |
| S-066 | AUTH-04 | D2 | 同一审批再用于另一次晋升 | 拒绝（已消费） | 晋升不写 `consumed_by_operation_id` | 潜在风险 | `console_commands.py:315-322` |
| S-067 | LEARN-01 | T5 | 存在 canary 时经 API 回滚到 v1 | DELIVERED，canary 回到 v1 | FAILED（payload 没有 `task_id`），canary 仍是 v2 | 已证实缺陷 | `test_p2_04_skill_lifecycle.py::test_owner_can_roll_a_canary_back_through_the_api` |
| S-068 | LEARN-03 | T1 | `POST /skills/{id}/evaluate` | 评测被执行并产出分数 | 只建 QUEUED 行，没有执行者 | 潜在风险 | `console_commands.py:521-553`（全仓 grep 无消费者） |
| S-069 | AUTH-01 | D4 | 跟踪的 `source_ids` 填其他项目的来源 | 拒绝 | JSONB 原样写入，不校验归属（跟踪目前无法激活） | 潜在风险 | `console_commands.py:372-389` |
| S-070 | TRUTH-04 | D4 | 对已 SUCCEEDED 的任务 `PUT /contract`，带伪造 `contract_sha256` | 409/422 | 200，公开视图显示伪造哈希，验收绑定的仍是真哈希 | 已证实缺陷 | `test_p2_02_projection.py::test_public_contract_hash_matches_the_authoritative_contract` |
| S-071 | TRUTH-06 | T1 | 任务进入执行 | 有 owner 审批记录才写 `approved_by` | worker 自动写 `approved_by=owner` | 潜在风险 | `goal_effect.py:407-426` |

### G. 部署、备份、迁移

| ID | 不变量 | 时点/维度 | 前置条件与注入方式 | 预期 | 实际 / 判定依据 | 分类 | 证据 |
|---|---|---|---|---|---|---|---|
| S-072 | STAB-04 | D5 | `alembic upgrade head` 新建一个库，与 `ensure_database` 建的库比对 | 同构 | 缺 `budget_accounts.tokens_held`、`resource_heads` 表及其策略与授权 | 已证实缺陷 | `test_p1_08_migration_drift.py` |
| S-073 | DATA-04 | T4 | 备份 → 新上传 → 恢复 | 新产物可读 | 文件被删，读取抛 `FileNotFoundError` | 已证实缺陷 | `test_p1_09_backup_restore.py::test_restore_keeps_bytes_written_after_the_backup` |
| S-074 | STAB-05 | T4 | 备份 → 任务完成 → 恢复 | 恢复完成 | 终态守卫抛异常，恢复整体回滚 | 已证实缺陷 | `…::test_restore_does_not_abort_when_a_task_finished_after_the_backup` |
| S-075 | STAB-05 | T6 | 在一个新库上恢复 | 得到可用系统 | `owners` 不在备份里，FK 失败；另缺 7 张账本表 | 已证实缺陷 | `…::test_backup_rebuilds_a_fresh_database` |
| S-076 | STAB-06 | T6 | 再次执行 `ensure_database`（README 写法） | 在跑的服务不受影响 | API 角色口令认证失败 | 已证实缺陷 | `test_zz_p2_08_password_rotation.py` |
| S-077 | STAB-05 | D5 | 恢复备份后，旧命令与在跑工作流的对账（docs/14 G.12） | DB 与 Temporal 一致 | 恢复是 upsert 合并，Temporal 历史不会随之回退 | 需要真实环境 | `supply.py:183-221`；需要持久化 Temporal 与真实备份流程 |

### H. 其他需要真实环境的场景

| ID | 不变量 | 时点/维度 | 前置条件与注入方式 | 预期 | 实际 / 判定依据 | 分类 | 证据 |
|---|---|---|---|---|---|---|---|
| S-078 | UNTRUST-05、MONEY-05 | D4 | OpenHands 作业在沙箱里拿到网关令牌后反复调用 | 只能用本作业剩余预算 | runner 现在用容器内假网关；接上真实网关后令牌不绑预算（S-028） | 需要真实环境 | `runner.py:21-46`；`model.py:60-146` |
| S-079 | STAB-07 | L | 生产 Temporal 集群下永不结束的工作流触及历史上限（事件数/字节数） | 有界 | dev server 内存存储无法验证 | 需要真实环境 | `durable_workflow.py:73` |

## 4. 本轮实测守住的对照场景（不计入上面的三类）

| 编号 | 不变量 | 场景 | 证据 |
|---|---|---|---|
| K-01 | AUTH-01 | 显式来源指向别的项目的产物 → 调用前 NEEDS_REVIEW(`allowed_sources_invalid`)，0 次调用 | `repro/test_c00_controls.py::test_control_cross_project_source_is_rejected_before_any_call` |
| K-02 | AUTH-02 | API 角色插入 `verification_results`、把任务改成 SUCCEEDED 都被拒 | `repro/test_c00_controls.py::test_control_api_role_cannot_write_verifier_evidence` |
| K-03 | AUTH-01 | 不设 `aether.project_id` 时 worker 角色看不到任务；设成别的项目也看不到 | `repro/test_c00_controls.py::test_control_rls_hides_rows_without_the_project_setting` |
| K-04 | TRUTH-02 | 评分器对照：候选不改写 `json.dumps` 时，失败的绑定套件判 FAIL | `repro/test_p1_06_grader_forgery.py::test_control_without_the_patch_still_fails` |
| K-05 | DUP-01 | 响应已记账、写答案前失败 → 重试复用回执，不再调用供应商 | 基线 `tests/integration/test_audit_round4.py`、`test_audit_round5.py`（347 项中） |
| K-06 | STOP-02 | 令牌发出后、发送前暂停或取消 → 不发送；调用进行中暂停 → 回执保留、任务停住 | 基线 `test_audit_round5.py::test_r503_*`，`test_audit_round4.py::test_codex_workflow_signal_during_the_call_blocks_success` |
| K-07 | MONEY-01 | 空正文、token 超额时已知用量入账 | 基线 `test_audit_round5.py::test_r502_*` |
| K-08 | MONEY-03 | 已有答案的重试与终态提交都核对 `active_seconds` | 基线 `test_audit_round5.py::test_r504_*` |
| K-09 | UNTRUST-05 | 评分容器、技能容器、runner：无网络、无凭证、看不到宿主文件 | 基线 `test_verification.py`、`test_skill_sandbox.py`、`test_runner.py`（装好 Docker 后的第 3、4 次运行） |
| K-10 | UNTRUST-03 | 注册表不信 MCP annotations；schema 变化进入隔离 | 基线 `tests/unit/test_mcp.py` |
| K-11 | DUP-02 | 同一 `command_id` 在工作流里只生效一次 | 基线 `tests/integration/test_workflow.py::test_workflow_deduplicates_commands` |
| K-12 | DATA-02 | 产物日志的 committed 以数据库提交为准；回滚与 tombstone 不被重新公开 | 基线 `test_audit_artifact_durability.py`、`test_audit_round3.py` |

## 5. 计数核对

| 分组 | 已证实缺陷 | 潜在风险 | 需要真实环境 | 合计 |
|---|---|---|---|---|
| A. 命令、发件箱与工作流 | 11 | 9 | 0 | 20 |
| B. 预算与模型调用 | 5 | 5 | 5 | 15 |
| C. 存储与去重 | 4 | 4 | 0 | 8 |
| D. 记忆与删除 | 4 | 3 | 0 | 7 |
| E. 验收器 | 4 | 2 | 2 | 8 |
| F. 权限、令牌、沙箱、MCP、技能 | 7 | 6 | 0 | 13 |
| G. 部署、备份、迁移 | 5 | 0 | 1 | 6 |
| H. 其他需要真实环境的场景 | 0 | 0 | 2 | 2 |
| 合计 | 40 | 29 | 10 | 79 |

