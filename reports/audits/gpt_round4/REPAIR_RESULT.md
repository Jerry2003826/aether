# 第四轮修复记录

基线 `07cc2ae4b0f864618da44cee7df815aef223f7e9`。审计包记录的定点套件是 58 项、46 通过、12 失败。那些检查跑在 SQLite 替身上，本轮没有重跑那个替身。移植后的 PostgreSQL / Temporal 用例在修复后通过。没有真实供应商 key，没有把 `live_ready` 改高或改低。

`active_seconds` 是从 `tasks.created_at` 起的墙钟，排队和暂停都算进去，不是累计 CPU 时间。

## R4-01 预算覆盖真实输入、已知用量和完成时限

复现：审计在 `max_tokens=100` 时仍发出约 12094 字符的 prompt，回报 2500+20 token 后任务成功，账本 `tokens_used=0`。价格未知时 token 也不入账。回报 150000 micro-USD 时 `actual_micro_usd` 被写成 NULL 且任务成功。`active_seconds=1` 的调用返回后仍成功。

根因：调用前只预留固定 512 token 和 50000 micro-USD，不看已经拼好的 prompt。超预留或价格缺失都走 `mark_unknown`，已知 token 留在 hold 里。期限只在调用前看一次。

改动：`services/worker/aether_worker/research.py` 用本地 cl100k 估算将发出的文本，并加上输出上限。`goal_effect.py` 在估算超出剩余额度时不发请求。`budget.py` 的 `record_model_response` 把已知 token 入账；价格未知只继续冻结金额；回报超过预留时写入 `actual_micro_usd`，状态是 `observed`，任务 `NEEDS_REVIEW`。返回后再核对墙钟，剩余时间传给调用超时。

之后：`tests/integration/test_audit_round4.py` 里长输入 0 次调用、未知价格下 `tokens_used=120`、超额 `actual_micro_usd=150000` 且不成功、调用后超过 1 秒不成功。`tests/unit/test_goal_satisfaction.py` 断言 `token ` 重复 2000 次的估算至少 2000，不看模拟供应商回报的 11/7。

## R4-02 预留幂等不再当成可以再打一次模型

复现：答案落盘失败后同一任务再执行，传输被调用两次。价格未知时第二次 `mark_unknown` 报 `not_reserved`。调用过程中暂停会丢掉已返回的正文，恢复后再打一次。

根因：`research-{task_id}` 的 `reserve()` 命中旧行就返回 True。任务把 True 当成新的派发许可。响应在写入 `task_answer` 之前没有落账。

改动：`prepare_model_call` 把一次逻辑请求标成已派发。已有回执就复用，不再请求供应商。已派发但没有回执则标 unknown 并 `NEEDS_REVIEW`（`live_call_unreconciled`），不自动重打。`record_model_response` 在 `task_answer` 之前写下正文和用量。暂停留下回执，但不发布成功；恢复复用同一回执。取消仍不是回滚。

之后：响应后注入 `OSError`，已知和未知费用都只调用 1 次。调用中暂停再恢复仍是 1 次。传输直接失败后的第二次执行也是 1 次，并且不成功。

## R4-03 短引用不是目标完成

复现：十二字前缀、引用了原文但最终数字相反、两问只答一问，都会 `SUCCEEDED`。

根因：`cites_fact` 的任意连续 12 字直接变成 `goal_state=pass`。

改动：12 字重叠只写入报告的 `citation`。`goal_completion` 要求每条和目标有词重叠的资料行都被覆盖：整句在回答里，或该行的数字都在且回答没有换一个资料和目标里都没有的数字。做不到是失败，不是成功。验收版本改为 `goal-artifact-5`，旧 PASS 不会被当成这次验收。没有把六十、六百、保养写进判断。

之后：十二字前缀、矛盾数字、只回答两条里的一条都不成功。整段原文或含同一数字的转述可以通过。

## R4-04 显式来源损坏就停止

复现：显式两份来源里有一份磁盘哈希不符时，代码跳过它并继续调用模型。

根因：`_source_snapshot` 对 `store.get()` 的 `ArtifactError` 只 `continue`，最后仍返回 ok。

改动：显式列表里任一项缺失、删除、不可读或哈希不符，调用前返回 `NEEDS_REVIEW`，`missing` 里是那一项。未指定列表时仍可跳过单份坏文件，但快照里记为 unreadable。验证使用同一规则。

之后：损坏那一份时 0 次调用，状态 `NEEDS_REVIEW`。

## R4-05 同步模型调用不再占住事件循环

复现：async `execute_task` 直接跑同步 `litellm.completion`。50ms 的 ticker 要等阻塞结束才运行。

根因：Activity 是 async，里面全是同步数据库、磁盘和 HTTP。`service.py` 的 `route_pending` 也在事件循环上跑。

改动：`prepare_task`、`execute_task`、`apply_task_control` 用 `asyncio.to_thread` 执行同步函数，并在等待时 heartbeat。线程不会被强行杀掉；返回后仍检查暂停和取消。worker 使用最多 8 个线程的执行器，轮询也放到线程里。Workflow 历史没有改，回放测试仍在。

之后：0.4 秒的阻塞调用期间，50ms ticker 在 0.25 秒内运行。Temporal 上一个 1.2 秒的慢调用不会挡住另一个任务在 1 秒内完成。

## 测试

```text
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 pytest -q tests/unit tests/integration
309 passed, 1 warning in 233.77s
```

没有 skip，没有 xfail。假上游只替换 LiteLLM 传输。

## 缺口

没有用真实 key 重跑 `scripts/run_live_task_check.py` 和 F26。研究路径的完成判定更严，短引用可能不再成功。`to_thread` 不能取消已经发出的供应商请求。价格仍然来自供应商回报或缺失，不是本地价目表。审计包里的 SQLite 58 项没有在本机重跑。

## Codex 跟进（PR #9 合入之后）

PR #9 已由用户合并为 `aedbdd54dccaba39e79282f87c09bab6a6dcc873`。合入时 main 的 pack-and-unit 与 integration 成功。SonarCloud 对合并提交是 neutral（Quality Gate not computed）；该 PR 的 quality gate 当时 passed。下面八项是合入后仍未解决的 P1，不是把验收改松。

- 数字覆盖：每个数字要带上紧随的单位和邻近主语。六十次与九十天对调、六十摄氏度写成六十华氏度，都是 `missing_fact`。整句被引用时仍然覆盖。沸点原句里的「资料1」仍不计入无依据数字。
- 引用：`goal_completion` 可以通过，但回答没有资料原句或 12 字重叠时，结论是 `citation_fail`，任务不成功。验收版本改为 `goal-artifact-6`。
- 空正文：先 `record_model_response`，已知 token 和费用入账，然后 `NEEDS_REVIEW`（`empty_answer`），不把这次调用标成 unknown。
- 费用上限：派发前按输入 100 micro-USD/token、输出 400 micro-USD/token 估算本次最坏费用。剩余金额装不下就不插入预留、不发请求。这是本地派发上限，不是供应商价目；更贵的模型仍可能在账单之后变成 `observed`。
- token：预留使用 chat 包装后的估算，大于裸 prompt。裸估算加 1 个 token 的额度不再派发。
- `observed`：快照、任务预算和 `/costs` 单独给出 `observed_micro_usd` 与 `observed_request_count`，不并进 `actual_known_micro_usd`。150000 对 预留 的超额仍是 `actual_micro_usd=150000`，公开视图能看见这笔超额。
- 暂停/取消：`workflow.patched("execute-interruptible")` 在 `execute_task` 进行时排空控制命令。测试通过 workflow signal，而不是直接改数据库。回执保留，任务不是 `SUCCEEDED`。旧的 `4ba3b2f` 历史没有这个标记，仍走原来的 `execute_activity`。
- 线程池：活动池满时，控制投递走单独的 `aether-control` 执行器。

一并处理的 P2：剩余时间不大于 0 时不派发，也不再把超时抬到 0.05 秒。显式来源文件不存在时与损坏一样 `NEEDS_REVIEW`。降级 `0011` 前先把 `observed` 行改成 `unknown`，金额写入 `usage_detail`，再恢复旧的 CHECK。

全量：

```text
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 pytest -q tests/unit tests/integration
324 passed, 1 warning in 237.74s
EXIT:0
```

本机没有 live model key。不把 `live_task_check` 或 F26 标成失败，也不改 `live_ready`。`comparative_evidence` 仍是 `not_run`。R4 合入后尚需监督者用真实 key 重跑 `scripts/run_live_task_check.py`。`to_thread` 仍不能取消已经发出的 HTTP。本地费用上限不是供应商价目。审计包里的 SQLite 58 项没有重跑。
