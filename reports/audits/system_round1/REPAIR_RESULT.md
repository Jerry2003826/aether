# 系统审计第一轮 · 本批修复结果

本批只做 P0-01、P0-02、P1-01、P1-02、P1-03，以及同根因的 P2-02、P2-03、P2-06 里「CANCELLED 不能改成 SUCCEEDED」的数据库约束。其余条目留给另外两批。

基线是 main `d6ed382`（PR #12 的合并）。分支 `cursor/sysfix-a-p0-p1-7618`。没有把 `product_engineering` 或 `live_ready` 改成通过。本机没有 live model key。

## 已修

| 项 | 做法 | 测试 |
|---|---|---|
| P0-02 | 每条控制台/记忆/评测命令用自己的 savepoint。领域错误和约束错误把这一条标 FAILED。`OperationalError` 继续抛出，让这一批稍后重试。`route_pending` 按项目隔离，`serve` 记下投递或路由失败后继续转 | `test_one_bad_console_command_does_not_wedge_the_batch`、`test_memory_claim_matching_deleted_bytes_does_not_wedge_memory_commands`、`test_documented_worker_survives_a_duplicate_source_and_keeps_running_tasks`、`test_skill_rollback_without_a_pin_fails_only_that_command` |
| P0-01 | 记忆 claim 分类为 `memory_claim`。删除和更正会把旧 claim 产物标成 tombstone。来源发现和显式名单只接受 `project_private`。迁移 `0012` 会改写已有 claim，并给已删除或 superseded 的 claim 打 tombstone | `test_deleted_memory_claim_never_reaches_the_model_or_the_verifier`、`test_corrected_memory_old_claim_is_not_ground_truth`、`test_high_sensitivity_memory_is_not_sent_to_the_cloud_model`、`test_proposed_memory_is_not_verification_evidence`、`test_explicit_memory_claim_is_not_an_allowed_source`、`test_legacy_claim_rows_are_reclassified_and_retired` |
| P1-01 | 活的逻辑产物用部分唯一索引 `(project_id, storage_key, classification, media_type) WHERE deleted_at IS NULL`。同分类同媒体类型仍复用同一行。tombstone 不挡住以后的新行。不同分类或媒体类型是新行。第一次绑定仍写 `artifacts.task_id`，既有的任务引用测试保持不变 | `test_uploading_a_previous_answer_gives_a_usable_project_source`、`test_deleting_an_upload_does_not_delete_another_tasks_answer`、`test_a_deleted_answer_does_not_break_a_later_identical_answer`、`test_reuploading_deleted_bytes_is_a_new_explicit_write`、`test_same_bytes_with_a_different_media_type_are_a_new_artifact` |
| P1-02 | 新运行用 `workflow.patched("bounded-activity-retry")`：最多 3 次，心跳超时 20 秒，`ArtifactError` 不可重试。活动里捕获 `ArtifactError` 后写成 `NEEDS_REVIEW`，不再抛出。旧历史不走这条选项 | `test_poisoned_execute_activity_is_not_retried_forever`、`test_artifact_error_is_not_retried_forever` |
| P1-03 | 投递按 `created_at, command_key` 取回。同一任务有一条信号失败时，后面已领取的命令回到 PENDING。API 的 resume 不清除 `cancel_requested`。worker 看到已接受的取消时把任务收成 CANCELLED，不恢复运行 | `test_dispatcher_keeps_per_task_order_when_one_signal_fails`、`test_cancel_then_resume_never_runs_the_model_when_a_signal_is_retried` |
| P2-02 | 终态任务的 `PUT /contract` 返回 409。排队中的任务仍可改草案，已有测试 `test_draft_contract_does_not_rewrite_an_approved_contract` 保持通过。公开 `contract_sha256` 取 `current_contract_id` 指向的 `goal_contracts`。每次状态更新在同一事务写 `task.status`，payload 带 `status` | `test_public_contract_hash_matches_the_authoritative_contract`、`test_terminal_status_is_visible_in_the_event_stream` |
| P2-03 | API 和 worker 写事件前锁任务行，序号冲突在 savepoint 里重试 | `test_api_command_event_and_worker_event_do_not_collide` |
| P2-06 数据库 | 终态不能改成任何其他状态，包括 CANCELLED→SUCCEEDED | `test_database_refuses_to_rewrite_a_cancelled_task_as_succeeded` |

P1-02 的原触发条件是「删掉答案后再写同字节答案」。P1-01 让这次写入成为新的逻辑产物，任务可以成功，所以那条路径断言的是「16 秒内离开 RUNNING，且尝试次数不超过 3」。无限重试的断言放在注入 `ArtifactError` 的 `test_artifact_error_is_not_retried_forever`，状态必须是 FAILED 或 NEEDS_REVIEW。

`test_tombstone_blocks_republish_and_recovery` 原来要求同字节再写入抛 `tombstone_retained`。那正是 P1-01 要去掉的行为。现在这条测试改为：旧 id 仍然读不到，新写入得到另一个 id，`recover` 不会把 tombstone 复活，日志在第二次 recover 后保持不变。

## 本批没有做

这些不在本批范围，或根因在另一批正在改的文件里：

| 项 | 原因 |
|---|---|
| P1-04、P1-05、P1-06、P2-01、P2-05、P2-07 | 另一批 |
| P1-07、P1-08、P1-09、P2-04、P2-08 | 另一批。P2-04 的「经 API 把 canary 滚回去」没有做。本批只保证一条没有 skill pin 的回滚命令自己 FAILED，不拖垮同批的下一条命令 |
| P2-06 的 SSRF 与 MCP argv | 另一批。本批只改了数据库终态守卫 |
| P2-09 | 产品待办，不是这五项缺陷的同根因 |
| S-012 活动超时后两次尝试互相把请求改成 unknown | 根因在派发状态机，和 P1-04 同一处。本批没有改 `budget.py` 的 `dispatch_state`，避免和那一批冲突。重试上限已经加上 |

`command_outbox` 没有最后错误列。失败命令的状态是 FAILED，没有新增列，避免和迁移链那一批抢表结构。暂时的数据库故障不标 FAILED。

## 监督者

合入后请用真实 key 重跑，不要把这次离线结果写成 live 通过：

```sh
uv run --locked --no-build python scripts/run_live_task_check.py --out reports/live_task_check.json
uv run --locked --no-build python scripts/run_f26_acceptance.py --out reports/f26_acceptance_result.json
```
