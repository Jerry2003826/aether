# AETHER 第四轮复审：live 调用、预算与失败恢复

审计基线：`07cc2ae4b0f864618da44cee7df815aef223f7e9`（main，PR #8 合并）。
上一轮基线：`4ba3b2f04df4bcd28a8d6f6cba0b0fd5d9d353cc`。
审计日期：2026-10-16（Australia/Melbourne）。没有修改 GitHub 远端，没有使用真实供应商密钥。

## 结论

PR #7/#8 确实修复了旧缓存、任务引用和日志恢复问题，并把 live 研究调用接进了常驻任务执行路径。不是仍停留在保存目标的旧实现。

本轮真实运行的本地定点套件：**58 项，46 通过、12 失败、0 error、0 skipped**。12 个失败归并为下面五类问题，其中目标验收是上轮问题的进一步覆盖，不应重复宣称已经完全关闭。

**关键发布建议：先处理模型调用幂等与预算，再处理答案完成判定、显式来源失效和异步阻塞。当前证据不足以支持无人值守付费运行。** 这不是对整个仓库所有功能的否定，也不是一次完整产品验收。

## 证据与边界

- 通过 GitHub 连接器读取 main、PR #7/#8 及选定源码；容器内 git clone 因 GitHub DNS 解析失败，未取得完整工作树。
- `source/` 是九个选定原始文件，不是完整仓库。每个文件按 Git 的 `blob <length>\0<bytes>` 算法核对 SHA-1，全部匹配 GitHub 返回值，见 `evidence/source_checksums.json`。
- 使用真实文件写入、哈希、目录锁、SQLite 事务；加载实际 Database/ArtifactStore/BudgetLedger。
- 模型网关和任务函数从这些原文件的 AST 编译；不重写被测函数体。不可用的导入边界由测试 harness 提供。`ModelGateway.complete()`、真实记账与产物逻辑仍执行，只有 `litellm.completion` 的传输被替换为可控假响应。
- SQLite 适配翻译 JSONB merge、cast、时间、set_config、FOR UPDATE、advisory lock。**不证明 PostgreSQL 行级权限、并发锁语义或真实 Temporal 重试。** 这里的重试是重新调用实际任务函数，模拟故障边界。
- 本地 Python/依赖不是仓库 `uv.lock` 环境，版本见 `evidence/environment.json`。19 个 warning 来自 SQLite datetime adapter，未隐藏。
- 实测网络调用次数指替身传输被调用次数，不是实际计费。所有费用与用量反馈均为可控测试数据。
- 远端 CI #220（run `38796434568`）状态为 success。该结果是 GitHub 返回的远端信息，不算本轮本地执行。没有重跑 F26、OpenHands、BGE、CodeGraph 或真实供应商任务。

## 旧问题复查

| 上轮项 | 本轮直接运行的基础场景 |
|---|---|
| 历史 PASS 覆盖新失败 | 同一候选先 PASS，随后损坏/删除；同一来源删除。新记录不再复用旧 PASS，历史记录保留。通过。 |
| 合同摘要不独立核对 | 与权威合同不一致的摘要得到 FAIL。通过。 |
| 两任务相同答案缺少第二个引用 | 同一答案仍去重，但 `task_artifacts` 中两个任务各有一条关联。通过。 |
| PAUSED 状态仍生成答案 | 执行前已暂停、已取消，以及暂停后恢复的基本路径通过；live 调用进行中的暂停见 R4-02。 |
| 截断尾先追加再恢复 | 追加入口使用同一 sidecar 锁并修复尾部。通过。 |
| 恢复时清零丢失前缀 | 注入 checkpoint 失败，完整前缀保留，之后可以恢复。通过。 |
| 第一行无关摘录被判成功 | 上轮具体例子现在选到了请求限额，但更广的目标完成判定仍未解决，见 R4-03。 |
| 先前上传/去重/压缩包回归 | 25 项相关检查通过。 |

## R4-01 / P1：预算未覆盖真实输入、已知用量和完成时限

### 实际复现

1. 任务 `max_tokens=100`；资料包含正常事实和 2000 个 `token` 单词。实际发出的 prompt 长度是 **12094 字符**。传输仍被调用一次，测试返回 `input_tokens=2500`、`output_tokens=20`，任务仍 `SUCCEEDED`；聚合账本 `tokens_used=0`、`tokens_held=80`。
2. 即使输入 100、输出 20 都已知，只是价格未知，聚合账本也仍显示 `tokens_used=0`、`tokens_held=512`。原始用量保存在 `usage_detail`，不是完全没有记录，但没有与总预算正确结算。
3. 费用上限 100000 micro-USD，测试响应报告 150000。该数值仍在执行事件里，但 `model_requests.actual_micro_usd=NULL`，任务 `SUCCEEDED`，没有超额终态。模拟反馈不等于真实供应商已经扣费。
4. `active_seconds=1`，传输耗时约 1.15 秒，任务约 1.19 秒后仍成功。调用前检查过期限，返回后却未复查，也没有把剩余期限约束到调用超时。

### 根因与代码

`goal_effect.py:825–964`：预留固定上限 `CALL_TOKEN_RESERVE=512` 和 `CALL_COST_RESERVE=50000`，随后才拼接完整 prompt。`max_tokens` 仅限制生成输出，不能覆盖已发送输入。收到超预留或价格缺失时统一 `mark_unknown`，没有分别结算已知 token 与未知费用。

`budget.py:110–169`：unknown 表示整个请求，但维度可能只是钱未知；实际用量已知时仍只保留旧预留。不能靠 UNKNOWN 把超额变成没有发生。

### 修复要求

调用前根据真正要发出的完整消息、工具 schema、模型 tokenizer 与输出上限估算/保守界定输入+输出。对计数或价格不可信的模型，明确拒绝硬预算承诺或转人工审批，而不是使用任意常数保证。

次数、token、金额分别建账。已知部分保留并入账；未知费用只冻结金额相应部分。超额返回值作为 reported/observed 数据保存，是否已对账另设状态，不能要求删掉实际反馈来满足“actual <= reservation”。价格来源可能是适配器估算，应标明来源。

使用一个明确的任务期限；将剩余期限传到调用超时，返回/发布前再核对。明确 `active_seconds` 是累计执行时间还是从创建起的墙钟，不能混称。

## R4-02 / P1：预算预留幂等被误用为模型调用幂等

### 实际复现

在模型已返回之后，第一次保存 `task_answer` 时注入 OSError，再执行同一任务：

- 费用已知：传输被调用 **两次**，任务成功；聚合账本 `requests_admitted=1`，只计一份费用和 120 token。
- 费用未知：传输同样两次，第二次抛 `BudgetError: not_reserved`，任务没有正常结束。
- 模型响应期间写入 `pause_requested`：首次任务正确停到 PAUSED，但未保存已收到的模型回答；resume 后再次调用模型，并触发同类 `not_reserved`。

### 根因

`goal_effect.py:876–928` 的 request key 固定为 `research-{task_id}`，账本 `reserve()` 命中同一预留时直接返回 True（`budget.py:67–71`）。任务层把 True 当成“现在允许发请求”，没检查请求是否已经派发/返回/未知。

调用结果没有在进入后续产物步骤之前形成可恢复记录。`settle()` 对已知同价请求直接返回，实际第二次调用就不增加计数；`mark_unknown()` 只接受 reserved，第二次 unknown 更新零行后报错。

### 修复要求

区分逻辑请求和实际 attempt，建立持久状态：至少 PREPARED / DISPATCHED / RESPONSE_RECORDED / UNKNOWN / SETTLED（命名可沿用现有账本）。相同 request key 不应授权第二次不明派发。

保存可恢复响应/响应产物引用，并绑定输入、模型配置、参数及原始用量；不要只对 alias 做请求摘要。恢复时复用已有响应。结果未知且供应商没有幂等查询时必须对账或人工处理，不能承诺凭空实现恰好一次。

暂停不应丢弃已发生操作的回执，但回执落盘与向用户发布新业务结果应分离；取消也不是回滚。重试幂等与实际调用计数必须分别测试。

## R4-03 / P1：短引用仍被当成目标完成证明

### 实际复现

来源：`接口每分钟最多允许的请求数量明确规定为六十次。`
目标：询问请求限额。
响应只有：`接口每分钟最多允许的请求`（12 字，不含限额），任务仍 `SUCCEEDED`。

另一个响应引用真实的六十次原文，紧接着写“最终答案：每分钟六百次”，也成功。

目标同时询问“请求上限和设备保养周期”，响应只答请求上限，也成功。

这些是特意设置的模型错误响应，证明检查器会放行，不说明真实模型每次都会生成这些错误。

### 根因

`research.py:100–108` 的 `cites_fact()` 接受任意连续 12 字。`goal_effect.py:1035–1051` 将它的 True 直接当作 goal_state=pass，再推成终态。

这只是引用/字符串重叠检查。选中一条来源不等于核对整个目标，字节完整与合同摘要一致也不是任务语义完成。

### 修复要求

保留字节和引用检查，但仅输出其能证明的 `citation_integrity`。对支持的目标类型用结构化验收条件和独立验证（例如必须返回全部要求字段、原文证据覆盖、数值/单位一致、不能自相矛盾）。自由文本或证据不足采用 UNVERIFIABLE / NEEDS_REVIEW 或已有 human_review，而非假装普通字符串包含就是完整验收。

不得针对本例把六十、六百、请求、保养等词写死。新增领域、不同数字/表达和多部分问题的隐藏式回归；提升验收算法时更新 verifier version 与证据缓存兼容策略。

## R4-04 / P1：显式来源损坏时仍继续使用剩余来源

任务显式选两份 artifact：A 可读、B 磁盘字节与摘要不符。代码跳过 B，调用模型一次并成功。

`goal_effect.py:691–739` 对不存在、tombstone 等元数据错误返回 invalid，但 `store.get()` 抛 ArtifactError 后只是 continue，最后返回 ok。与 README 所述“显式列表内任一项不可读就整次待复核”不一致。

修复：区分默认发现和显式完整来源集合。显式集合任一项缺失、不可读、摘要/版本失效，调用前 fail closed，报告具体缺项，不私自缩小任务输入。验证时仍要使用同一获准来源快照。不要放宽允许列表回退到全项目资料。

## R4-05 / P1：async Activity 内同步调用模型，阻塞共享事件循环

`goal_effect.py:88–91` 的 async `execute_task()` 直接运行同步 `execute_task_effect()`；后者调用同步 `ModelGateway.complete()` / `litellm.completion()`，并进行同步数据库/磁盘操作。

本地运行真实 async 包装，在同一 asyncio 循环安排一个 50ms ticker，让模拟传输阻塞 350ms：ticker 约 **387ms** 才执行。不是实际 Temporal 服务的超时复现，但直接证明这段 coroutine 会阻塞事件循环。

当前 `service.py` 直接将 prepare/execute/control 注册为这些 async Activities。Temporal 官方文档说明异步 Activity 与 worker 事件循环共享，阻塞调用会妨碍其他工作；同步 Activity 应配线程执行器，或者全链路改为真实异步。

修复：最小路线优先评估受限 ThreadPoolExecutor 中的同步 Activity；数据库连接按工作线程/事务隔离，不能跨线程共享连接。需要合作式取消、心跳/超时与调用账本。单纯套 `asyncio.to_thread` 不等于线程可强杀或副作用可回滚；同时检查常驻轮询的同步数据库工作。

## 为什么现有 CI 会通过

读取的 `test_live_research.py` 用本地兼容服务测试了正常回复、无引用错误、极低 token 预算、零金额、已耗尽次数、调用前已过期、允许列表等。它有实际价值，但没有覆盖长输入的真实估算、调用返回后超限、响应后保存失败、进行中暂停再恢复、以及“有引用但没回答”的情况。

远端 CI 绿色和本轮定点失败不矛盾。新增结果不是对远端全量测试的替代。

## 交付与运行

```sh
python3 verify_sources.py
python3 -m pytest -q --tb=short
```

审计配置明确收集全部五个本地测试文件，预期当前源版本仍有 12 个失败。不得把它们转为 xfail 或移出 testpaths 作为修复。

`repo_tests/test_audit_round4.py` 是新增 PostgreSQL 测试候选，共 8 个 pytest case；本轮仅 py_compile 语法检查，未在真实仓库执行。它复用现有数据库 fixture 与公开 enqueue 接口，并替换 LiteLLM 传输。

ZIP 已解压到独立目录，重新核对文件清单和九个源文件 blob，并重跑同一套件：仍为 **46 通过、12 失败**。记录在 `evidence/reextracted_checks.txt` 和 `evidence/reextraction_summary.json`。

本轮没有提供生产补丁。几个问题牵涉请求生命周期和合同验收，不适合用未经集成验证的零散条件覆盖。具体施工约束在 `CODEX_REPAIR.md`。
