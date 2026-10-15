# 第五轮修复记录

基线 `aedbdd54dccaba39e79282f87c09bab6a6dcc873`。审计包 74 项里 13 项失败，归成 R5-01 到 R5-05。那些检查跑在 SQLite 和分词替身上，本轮没有把那个替身套件当成 PostgreSQL 验收。移植后的用例在 `tests/integration/test_audit_round5.py`。没有改 `live_ready`。`comparative_evidence` 仍是 `not_run`。

同一分支带上 PR #9 合入后 Codex 仍未解决的 P1：聊天包装预留、派发前费用上限、`observed` 进入投影、调用期间的 workflow signal、控制线程池分开。第五轮把其中重叠的数字覆盖、空正文入账和停发边界再收紧。

## R5-01 数字集合相同不能通过

复现：甲 60 次、乙 90 次对调后任务成功。每分钟答成每秒、最多答成超过、目标还要超时时间但资料没有，也成功。

根因：覆盖只要求数字出现，不要求对象、时段和比较关系。目标里多出来的一项只要有一条资料重叠就会被当成整题完成。

改动：每个数字绑定紧随单位、前面的时段、最多/超过这类比较，以及标点前的主语。回答里多出来的主语不能算覆盖。目标用「和 / 与 / 以及」拆成两项而其中一项没有资料支持时，结论是 `unsupported_goal_part`，状态 `unverifiable`。正确转述和原句仍可通过。验收版本 `goal-artifact-7`。十二字引用仍写入报告的 `citation`，不再单独否决已经对上的转述。

测试：`test_r501_semantic_mismatch_is_not_success`，`test_r501_positive_correct_paraphrase`。

## R5-02 空正文和 token 超额都入总账

复现：空正文把已知 120 token 标成 unknown。输入 2000 加输出 20 超出预留时，请求行有数字，`tokens_used` 仍是 0。

根因：空正文在入账前返回。超额分支不更新账户上的 token。

改动：先 `record_model_response`。已知 token 无论是否超出预留都计入 `tokens_used`，并释放这次的 token 冻结。费用超额仍不并进已知花费。

测试：`test_r502_empty_text_keeps_usage_receipt`，`test_r502_overrun_books_aggregate_usage`。

## R5-03 已经停止的任务不再发出新调用

复现：`issue_token` 返回前任务已变成 PAUSED 或 CANCELLED，传输仍被调用一次。

根因：预留之后、`gateway.complete` 之前不再看控制状态。

改动：令牌发出后若任务已停止，就 `release_unsent`，状态改为 `cancelled_before_dispatch`，不调用传输。恢复时同一请求键可以重新预留。已经发出的调用仍保留回执。

测试：`test_r503_stop_applied_before_send_blocks_transport`。

## R5-04 恢复和最终提交都核对墙钟

复现：答案已保存、验证第一次抛错，等到超过 `active_seconds` 再执行仍成功。验证结束到提交成功之间越过期限也仍成功。

根因：已有 `task_answer` 直接返回。`_commit_terminal` 不看期限。

改动：读到已有答案时若已过 `tasks.created_at + active_seconds`，任务失败，不再打模型。提交 `SUCCEEDED` 的同一事务里再核对一次，过期则写成 `FAILED`，`resume_state=budget_exhausted`。没有预算账户的脚本任务不受影响。

测试：`test_r504_verifier_retry_checks_deadline`，`test_r504_terminal_commit_checks_deadline`。

## R5-05 原样引用不取消来源身份

复现：回答字节与上传资料相同，去重复用同一 artifact，随后 `task_id` 被写上。显式来源因为 `task_id` 非空变成 invalid。

根因：来源选择把「已被某任务引用」当成「不再是项目资料」。

改动：`project_private` 的文本资料在被 `task_artifacts` 引用之后仍可被显式名单和未指定名单读到。分类不是 `project_private` 的产物仍然不能当来源。第一处引用仍会写下 `artifacts.task_id`，这是第三轮链接测试要求的占用次数；第二处引用不会改写它。

测试：`test_r505_correct_quote_does_not_take_source_identity`。

## 测试

```text
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 pytest -q tests/unit tests/integration
339 passed, 1 warning in 248.28s
EXIT:0
```

## 缺口

本机没有 live model key。不把 `live_task_check` 或 F26 标成失败。R5 合入前尚需监督者用真实 key 重跑：

```sh
uv run --locked --no-build python scripts/run_live_task_check.py --out reports/live_task_check.json
uv run --locked --no-build python scripts/run_f26_acceptance.py --out reports/f26_acceptance_result.json
```

`to_thread` 不能取消已经发出的 HTTP。本地费用上限不是供应商价目。审计包里的 SQLite 74 项没有在本机重跑。
