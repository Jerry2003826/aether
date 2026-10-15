# AETHER 第三轮定点复审

## 基线与结论

检查提交：`4ba3b2f04df4bcd28a8d6f6cba0b0fd5d9d353cc`（PR #6 合并后的 main）。上一轮是 `55fd636fa1615314c129f5728663f3450595bc47`。

远端 GitHub Actions CI #196（run 38772351651）状态 success；这是远端运行记录，不是本次本地执行结果。

**结论：旧问题的基础路径已有改进，但不建议签署产品验收通过。本轮 42 项定点检查：32 通过、10 失败、0 跳过、0 测试错误。10 个失败用例归并为五类问题。没有修改 GitHub 远端，没有给当前生产代码打修复补丁。**

这不是完整仓库审计，也不是 PostgreSQL/Temporal/live 模型的现场运行。

## 测试边界

通过 GitHub 连接器固定提交读取代码。本机 git 访问 GitHub 的 DNS 解析失败，没有成功 clone。选定的七个完整文件由已读取内容和上次快照重建，全部与当前提交的 Git blob SHA-1 核对一致，另外提供 SHA-256。

`Database.transaction` 与 `ArtifactStore` 采用原始源文件；任务执行和验证测试编译原始同步函数 AST，未改动函数体。数据库构造入口注入为真实文件型 SQLite，翻译 PostgreSQL 文本 cast、jsonb cast、set_config/now/current_user。角色名称仅由测试替身提供，**不验证真实角色权限或 RLS**。任务表是本轮最小关系模型，不能用来证明生产迁移、权限、并发调度或完整约束正确。

上传测试只注册从源码提取的上传路由，身份认证和项目权限是测试替身，不是完整 API 应用。文件读写、损坏、tombstone、事务提交和归档操作真实执行。

`repo_tests/test_audit_round3_postgres.py` 是供真实仓库采用的 PostgreSQL 版本候选，**只做语法检查，没有执行**，不计入 42 项。

## 上轮问题的关闭程度

| 上轮范围 | 本轮证据 | 判定 |
|---|---|---|
| Database.fetch_one / fetch_all 缩进 | 对真实 SQLite 查询返回正确结果，类方法存在 | 基础回归关闭 |
| ZIP/TAR `./` 成员 | 两种格式均通过；规范化冲突和穿越/链接检查保留 | 基础回归关闭 |
| 单次短写 | 写入循环和读取通过 | 已修复此场景 |
| 截断尾立即 recover | 完整前缀恢复、尾部隔离通过 | 已修复此顺序；追加与二次崩溃仍有问题 |
| 首次验证时损坏或 tombstone | 首次检查分别 FAIL；正常产物 PASS | 已修复首次检查；缓存复用仍阻塞 |
| 新候选/新合同 | 获取不同验证 ID | 基础绑定比上版更强 |
| 正式任务执行 | 从原文存档升级成读取上传文本、生成固定摘录并写终态 | 仍是 scripted 路径，不能等同完成任意用户目标 |
| 第一轮二进制上传、事务回调、去重恢复、普通归档重试 | 对应隔离控制测试通过 | 没有在这些场景复现回归 |

## R3-01：旧 PASS 覆盖新的失败观察（P1）

位置：`services/worker/aether_worker/goal_effect.py:222–286`，尤其读取之后的已有证据查询与 `if existing is not None: return str(existing)`。

新的实现确实会读取真实候选字节，并计算 `checks_passed`。问题出在随后的缓存命中：查询仍按账本里的候选摘要与传入的合同摘要匹配，命中后不看本次 `observed`、`deleted`、`checks_passed` 就返回旧 ID。

真实复现场景：正常候选验证 PASS；随后分别损坏同一候选、tombstone 同一候选、删除唯一来源，再对同一任务/候选/合同调用验证。三种情况下都返回同一条历史 PASS。

历史 PASS 作为“过去观察”可以保留，但不能由当前重新验证入口当作“现在仍通过”的结果返回。若执行器在首次验证记录落库后、最终状态落库前中断，恢复阶段便可能使用已失效证据。

回归：`test_r3_01_cached_pass_cannot_override_new_failed_observation`，三个参数化用例均失败。

修复要求：区分查询历史证据和验证当前候选；任何新的不可读、已删除、摘要不符或来源失效都必须影响当前返回结果。缓存键应绑定当前候选、权威合同、验收器/套件版本和所需来源快照/可见性状态；不把失败的新观察丢弃。保留历史证据，不修改历史事实来掩盖变化。补测先 FAIL 再修好，避免旧 FAIL 永远挡住新的有效结果。

## R3-02：目标、合同与 SUCCEEDED 的含义不一致（P1）

位置：`research.py:37–48`、`goal_effect.py:95–128,165–186,222–286,354–366`。

`scripted_answer()` 选第一行至少 8 个字符、且不在目标原文中的资料，返回“资料记载：<该行>”。`_answer_uses_source()` 只检查回答包含一行来源文本，不检查是否回应目标。`execute_task_effect()` 将这个 PASS 映射成业务 `SUCCEEDED`。

### 研究范围内的真实反例

上传文本：

```text
实验设备每九十天完成一次保养。
接口请求上限为每分钟六十次。
```

目标：`根据资料，接口每分钟最多允许多少次请求？只回答请求限额。`

实际回答：`资料记载：实验设备每九十天完成一次保养。`

实际写入的任务结果：`SUCCEEDED`。

另一个反例是目标“计算 17 × 23，只返回整数”，实际仍返回设备保养摘录并 SUCCEEDED，而不是 391 或明确拒绝/交待无法完成。数学例子不是要求系统必须支持所有任务；不支持就不能宣布完成。

### 合同摘要也没有被独立验证

传入一个真实 contract_id，但把 contract_sha 改成与数据库记录不同的 64 位值，验证仍 PASS。此函数没有独立读取合同内容重新核对摘要。仅把调用方给的摘要写进验证记录，不是验证了合同。

回归：`test_r3_02_research_answer_must_address_requested_fact`、`test_r3_02_unrelated_goal_must_not_be_marked_succeeded`、`test_r3_02_contract_hash_must_be_validated_against_contract`，均失败。

修复要求：把 scripted provider 明确限制为离线流程/特定摘录能力测试，并让报告区分工程流程通过、字节完整性通过、用户目标完成。未实现目标类型时 NEEDS_REVIEW/UNVERIFIABLE，而不是泛化成功。已有模型网关和 typed loop 应按既有架构接入，不重做框架；独立验收必须根据当前批准合同执行相应检查。不能把示例里的“六十次”或“391”硬编码成修复。

这项判断仅针对当前 `python -m aether_worker` 所注册的 `prepare_task/execute_task` 路径，不否认仓库另有 live 探针或编码组件；本轮未运行它们。

## R3-03：两个任务产生相同答案，只有第一个拥有产物（P1）

位置：`goal_effect.py:313–351`，与 `ArtifactStore` 的按项目内容地址去重、API `control.py` 的 `_snapshot` 联合发生。

`store.put` 对相同字节返回已有 artifact ID。随后 `_ensure_answer` 只在 `task_id IS NULL` 时绑定任务，但不检查实际更新行数。若同一产物已经属于任务 A，任务 B 的 UPDATE 是 0 行；它仍返回这个 artifact ID，并继续通过验证到 SUCCEEDED。

本轮在同一项目相同来源上顺序执行两个不同目标，二者均 SUCCEEDED、返回同一个回答 ID。数据库中 task_answer 只有一行，task_id 为任务 A；任务 B 没有自己的回答关联。

已读到的 `_snapshot()` 按 `artifacts.task_id = task_id` 收集 `artifact_ids`，因此第二个任务的产物清单不能通过这条查询暴露答案。这里只读了该 API 的查询，未启动真实浏览器。

回归：`test_r3_03_identical_answers_must_be_linked_to_each_task` 失败。

修复要求：分离内容字节去重和逻辑产物/任务引用。可以使用任务—产物关联表，或每个任务独立逻辑产物记录指向共享 blob；补迁移和权限。API 通过授权关联展示产物。不能覆盖第一任务 task_id，不能给输出文本加随机 UUID 规避去重，不能仅绕过验证。

## R3-04：日志修复顺序与二次崩溃仍会破坏恢复（P2）

位置：`artifacts.py:557–618`。

### A. 崩溃后先写入，后恢复

准备一个完整 staged 记录，再添加一个无换行的不完整尾记录，模拟上一进程中断。新进程没有先 recover，而是正常 `put` 新文件。`_journal()` 只做 O_APPEND，不处理已有损坏尾；新 JSON 接在半行后面，文件末尾现在有换行。之后 `_journal_rows` 不再把它当尾损坏，解析时报告 `journal_corrupt`。新的文件可以保存，但旧的完整待恢复记录被阻断。

这不意味着吞掉真正的中间损坏。应在追加入口或经过强制验证的启动流程里，先处理已知截断尾，再允许新追加。

### B. 修复过程再次失败

当前恢复先保留 head 在内存，然后 `ftruncate(fd,0)`，再把完整前缀写回。注入“截断后、重写前”的 I/O 故障，原来完整的 journal 前缀丢失；再次恢复返回空列表。磁盘字节仍可能存在，这里复现的是恢复日志/元数据前缀丢失，不声称所有已提交业务数据丢失。

回归：`test_r3_04_append_after_crash_must_not_convert_tail_into_middle_corruption` 和 `test_r3_04_tail_repair_crash_must_not_erase_valid_prefix`，均失败。

修复要求：隔离损坏尾后，在同一稳定锁协议下把文件截到完整前缀的长度，不要先清零重写前缀。追加也要遵守同一协议。若使用原子文件替换，需要独立稳定锁文件以避免 flock 锁住旧 inode 后与新写入竞争。保留隔离数据和错误信息，不能用 `except: return []` 当成功恢复。

## R3-05：PAUSED 不阻止执行，函数返回状态可与数据库矛盾（P1）

位置：`goal_effect.py:95–128,399–411`，另读了 `durable_workflow.py` 和 API `control.py`。

`execute_task_effect` 仅对 SUCCEEDED/FAILED/NEEDS_REVIEW/CANCELLED 提前返回，没有阻止 PAUSED。直接在 PAUSED 上执行仍读取资料、写回答、写 PASS。`_set_status()` 的条件更新匹配 0 行，却不检查 rowcount；函数仍返回 SUCCEEDED，数据库仍是 PAUSED。

回归：`test_r3_05_paused_task_must_not_create_answer_or_report_success` 失败。取消状态 CANCELLED 的控制用例通过，但这不证明 HTTP cancel 命令能把任务变成 CANCELLED。

静态调用链：HTTP commands 接受 pause/resume/cancel；cancel 只写 `resume_state='cancel_requested'`；Workflow 对各命令都调用 prepare/execute；`_start_command` 只接受 kind=start，其余返回 ignored。当前展示的路径没有完整执行控制语义。本轮没有真实 Temporal 服务，不能把这段静态分析称为 HTTP/Temporal 动态复现。

修复要求：明确 pause/resume/cancel 的 Workflow 状态转换和边界，避免排在长 Activity 后的取消命令一直得不到处理。在执行/新副作用/最终状态提交前核对授权与当前控制状态；检查条件更新结果，以数据库实际状态或权威 Workflow 结果返回，不凭本地变量宣布成功。补进程级取消/暂停竞态测试。

## 不计入上述五类的待验证事项

1. **旧 Workflow 历史升级兼容。** 同一 Workflow 类型的 activity 序列由 materialize_goal 改成 prepare_task/execute_task。新启动的 sleep/restart 测试不等于旧历史可回放。官方文档要求不兼容 Workflow 改动使用版本固定或 patch；需要对 55fd636 的历史做 Replayer 测试。本轮未运行，不宣布已发生 nondeterminism。
2. **任务约束入口。** 当前已读到的 create_task 接受 budget 和 allowed_source_ids，但计算幂等摘要和 insert_start_command 时只使用 goal；后续 `_source_texts` 读项目所有未绑定纯文本。需要在上线 live 路径前验证授权来源/预算不会被静默忽略，未支持的字段应拒绝，而不是看起来接受。本轮只做静态检查，未运行完整 HTTP 权限测试。
3. **真正的中断恢复。** 要在外部工具执行、写入成功但回应丢失、验证写入与最终状态之间中断，而非只在 durable sleep 期间停 worker。需要真实 PostgreSQL/Temporal 与当前角色验证。

## 本轮命令与结果

```bash
python -m pytest -q -s test_artifact_audit.py test_patch_guards.py   test_upload_endpoint_isolated.py test_round3_tasks.py test_round3_journal.py   --tb=short --junitxml=evidence/round3_results.xml
```

```text
10 failed, 32 passed
exit: 1
```

42 项包括以前的定点控制检查与本轮新反例，没有失败标跳过或 xfail。失败不是产品全部不能运行，而是五类明确的边界/目标验收缺陷。原仓库全量 CI 与本轮反例是不同测试集合，远端绿色与本地失败并不矛盾。

## 交接

先修 R3-01/R3-02 和 R3-05，再处理任务引用与日志恢复。按 `CODEX_REPAIR.md` 实施。完成后在真实 PostgreSQL、Temporal 和文档启动命令下复验；保留本轮的反例，不用新示例替换旧断言来获得绿色。

`source/` 只是七个文件的固定版本子集，不是完整仓库，禁止覆盖整个项目。包中没有生产修复补丁，也没有运行或授权 push、部署、采购或真实外部写入。

## 可核对来源

- 固定提交：https://github.com/Jerry2003826/aether/commit/4ba3b2f04df4bcd28a8d6f6cba0b0fd5d9d353cc
- 源码精确路径与 blob：`evidence/source_checksums.json`。
- 源码静态补充：https://github.com/Jerry2003826/aether/blob/4ba3b2f04df4bcd28a8d6f6cba0b0fd5d9d353cc/services/worker/aether_worker/goal_effect.py、https://github.com/Jerry2003826/aether/blob/4ba3b2f04df4bcd28a8d6f6cba0b0fd5d9d353cc/services/worker/aether_worker/research.py、https://github.com/Jerry2003826/aether/blob/4ba3b2f04df4bcd28a8d6f6cba0b0fd5d9d353cc/services/api/aether_api/control.py、https://github.com/Jerry2003826/aether/blob/4ba3b2f04df4bcd28a8d6f6cba0b0fd5d9d353cc/services/worker/aether_worker/durable_workflow.py、https://github.com/Jerry2003826/aether/blob/4ba3b2f04df4bcd28a8d6f6cba0b0fd5d9d353cc/services/worker/aether_worker/service.py。
- 上游修复报告：https://github.com/Jerry2003826/aether/blob/4ba3b2f04df4bcd28a8d6f6cba0b0fd5d9d353cc/reports/audits/gpt_round2/REPAIR_RESULT.md（其 254 passed 是上游自报，不计入本轮实测）。
- 当前 CI：https://github.com/Jerry2003826/aether/actions/runs/38772351651。
- Temporal 官方安全部署： https://github.com/temporalio/documentation/blob/main/docs/develop/safe-deployments.mdx
- Temporal 官方回放机制： https://docs.temporal.io/workflows
