# 审计三轮修复结果

基线 `4ba3b2f04df4bcd28a8d6f6cba0b0fd5d9d353cc`。没有改 `STATUS.json`，没有把失败标成跳过或 xfail，也没有把脚本提供者写成 live 成功。

修复前，把审计包里的 PostgreSQL 候选放进 `tests/integration/test_audit_round3.py` 后，在真实 PostgreSQL 上是 9 个失败：

```text
AETHER_REQUIRE_SERVICES=1 pytest -q tests/integration/test_audit_round3.py --tb=line
FAILED test_round3_cached_pass_is_not_current_validation[corrupt_answer]
FAILED test_round3_cached_pass_is_not_current_validation[delete_answer]
FAILED test_round3_cached_pass_is_not_current_validation[delete_source]
FAILED test_round3_success_requires_actual_goal_satisfaction
FAILED test_round3_verifier_rejects_wrong_contract_hash
FAILED test_round3_repeated_tasks_have_own_answer_reference
FAILED test_round3_paused_task_does_not_execute
FAILED test_round3_append_after_crash_preserves_journal_recovery
FAILED test_round3_repair_second_crash_preserves_complete_prefix
9 failed
```

包内第 10 个失败是「问请求限额却摘到保养周期」。它已经移植成 `test_round3_requested_limit_is_not_replaced_by_an_unrelated_excerpt`。

修复后同一套加上全量单元和集成：

```text
AETHER_REQUIRE_SERVICES=1 AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 pytest -q tests/unit tests/integration
274 passed, 1 warning in 211.88s
```

警告是既有的 Pydantic `ReadOnly` 提示。`scripts/verify_freeze.py` 通过。

## R3-01 旧 PASS 盖住新的失败观察

**修复前：** 同一候选先 PASS，再损坏字节、tombstone 或删掉来源，`_ensure_verification` 仍返回那条 PASS。

**根因：** 缓存查询只比对账本摘要和调用方传入的合同摘要，不看这次读到的字节、删除状态和来源是否还在。

**修改：** `services/worker/aether_worker/goal_effect.py`、`services/worker/aether_worker/research.py`，迁移 `0009` 给 `verification_results` 增加 `observed_sha256`、`source_snapshot_sha256`、`verifier_version`。验收者自己读候选和权威合同。缓存只在候选、合同、观察到的摘要、来源快照、验收版本和结论都一致时命中。新的失败会另插一行，不改旧行。损坏修好之后可以再次命中原来的 PASS，旧 FAIL 还在。

**修复后：** `test_round3_cached_pass_is_not_current_validation` 三个参数化用例，以及 `test_round3_repeat_and_restored_observation_keep_history` 通过。

## R3-02 无关摘录被标成 SUCCEEDED，合同摘要没有独立核对

**修复前：** `scripted_answer` 取第一条足够长的资料。目标是计算 `17 × 23` 时，任务仍 `SUCCEEDED`，回答是设备保养摘录。调用方把合同摘要改成与库里内容不符的 64 位值，验证仍 PASS。

**根因：** 验收只要求回答里出现任意一条来源，不要求这条来源对得上目标，也不重算合同内容的摘要。

**修改：** `research.py` 的 `offline_fact` 只摘录和目标有词面重叠的资料。算术类目标直接返回空。只有一条资料、而且目标是在问已上传资料时，才用这一条。做不到时 `execute_task` 写 `NEEDS_REVIEW`，不写 `SUCCEEDED`。验收报告用 `pipeline`、`byte_integrity`、`goal_completion`、`contract_binding` 分开记录，并标明 `model_mode=scripted`、`offline=true`。合同取任务的 `current_contract_id`，没有当前合同时取最高版本，不用最早一版。调用方摘要必须等于权威内容的摘要。

**修复后：** 算术目标是 `NEEDS_REVIEW`。错误合同摘要不是 PASS。问请求限额时，若标成 `SUCCEEDED`，回答里必须有对应资料。`test_round3_wrong_fact_report_separates_integrity_from_goal` 和 `test_round3_current_contract_is_not_the_earliest_version` 通过。二轮里占位用的合同摘要改成了内容的真实摘要，PASS / FAIL 断言没有放宽。

## R3-03 两个任务写出相同回答，第二个没有自己的产物

**修复前：** `store.put` 按内容去重。第二个任务的 `UPDATE artifacts SET task_id` 影响 0 行，任务仍 `SUCCEEDED`，按 `artifacts.task_id` 查不到回答。

**根因：** 一条产物行的 `task_id` 被当成了唯一的任务引用。

**修改：** 新表 `task_artifacts`（迁移 `0009`，项目 RLS，worker 可写，API 只读）。内容去重保留。第一行任务可以在 `task_id` 仍为空时认领这条 blob，之后不再改写。每个任务单独插链接，并检查插入行数。`GET /v1/projects/{id}/tasks/{id}` 的 `artifact_ids` 同时包含 `artifacts.task_id` 和未删除的链接。

**修复后：** 两个摘录任务都 `SUCCEEDED`，共用一个回答 id，blob 的 `task_id` 仍是第一个任务，第二个任务的更新行数是 0，链接有两行，任务详情能看到该回答。跨项目不可见，不同分类可以各有一条链接，删掉一条链接后另一个任务还在。并发重复绑定后仍然只有一行。

## R3-04 崩溃后的追加把截断尾变成中间损坏，修复再崩溃会擦掉前缀

**修复前：** 完整 staged 记录后面加一段没有换行的尾巴，下一次 `put` 直接 `O_APPEND`，恢复时报 `journal_corrupt`，旧记录恢复不了。修复路径先 `ftruncate(0)` 再重写前缀，重写前崩溃则前缀丢失。

**根因：** 追加不先修尾巴。修复把唯一副本先截成空文件。

**修改：** `packages/storage/aether_storage/artifacts.py`。追加和读取共用 `journal/<project>.lock`。有截断尾时先隔离尾巴，把完整前缀复制到 `journal-repair/`，再把日志截到前缀长度，不先清零。复制失败时原文件不动。中间一整行损坏仍然报 `journal_corrupt`。

**修复后：** `test_round3_append_after_crash_preserves_journal_recovery` 和 `test_round3_repair_second_crash_preserves_complete_prefix` 通过。二轮的短写、截断尾和中间坏行测试仍然通过。

## R3-05 PAUSED 不阻止执行，状态更新 0 行仍返回成功

**修复前：** `execute_task_effect` 不把 `PAUSED` 当边界。`_set_status` 不看更新行数。HTTP `cancel` 只写 `resume_state`。Workflow 对每条命令都走 `prepare` / `execute`，非 `start` 命令在活动里被忽略。

**根因：** 控制命令没有状态迁移。活动在写回答和写终态之前不再读库。

**修改：** `durable_workflow.py`、`goal_effect.py`、`dispatcher.py`、`control.py`。`pause` / `resume` / `cancel` 由 `apply_task_control` 活动处理。接口先写 `resume_state`，worker 再迁移到 `PAUSED`、`RUNNING` 或 `CANCELLED`。`QUEUED` 也可以进入 `PAUSED`，避免还没开始的任务被启动。执行前、写回答前、提交终态前都会再读控制状态；条件更新匹配不到行时返回数据库里的状态。大于 0 的接收停顿仍默认关闭；测试把它设成 4 秒时，控制信号可以提前结束等待，旧历史仍走原来的 `workflow.sleep`。

**修复后：** 直接把任务改成 `PAUSED` 后执行，不产生 `task_answer`，返回的状态是 `PAUSED`，再把状态改成 `SUCCEEDED` 也匹配 0 行。进程测试用公开 HTTP：`RUNNING` 时暂停，没有 `research.call`，也没有摘录回答；恢复后 `SUCCEEDED` 且 `research.call` 为 1。另一条任务在 `RUNNING` 时取消，变成 `CANCELLED`，重启 worker 后仍然没有 `research.call`。

## 没有做完的部分

- 脚本提供者不是 live 模型。`live_ready` 和 F26 没有重跑，也没有付费密钥。
- `create_task` 仍接受 `budget` 和 `allowed_source_ids`，执行路径没有按预算停机，也没有把来源限制成 `allowed_source_ids`。未传这两个字段时行为和以前一样。
- 没有用 `4ba3b2f` 上保存的 Workflow history 做 Replayer。新的接收停顿用了 `workflow.patched("intake-pause-interruptible")`，带有旧 sleep 的历史会继续走 sleep。
- 取消和暂停的进程证据发生在 `RUNNING`、读资料之前。活动在副作用和终态提交前会再核对控制状态；这次没有另起一个长时间外部工具来卡住活动。
- 编码作业仍走已有的 `coding.create`。这次的 HTTP 证据是研究摘录路径。
