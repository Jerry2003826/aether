# AETHER 第五轮复审：回归已通过，剩余边界仍需修复

审计提交：`aedbdd54dccaba39e79282f87c09bab6a6dcc873`（main，PR #9 合并）。上一轮基线为 `07cc2ae4b0f864618da44cee7df815aef223f7e9`。

## 结论与证据边界

上轮58项场景在当前隔离环境中复查全部通过；新增16项检查中3项通过、13项失败。最终总计 **74项：61通过、13失败、0错误、0跳过**。13个失败归并为下述五类问题，不是13个独立漏洞。

这次不是完整仓库测试。通过GitHub连接器读取PR和源代码，因容器无法解析github.com，未直接克隆；根据父提交快照和本轮变更重建九个选定文件。三个改变的源码与连接器返回的Git blob SHA一致；六个未变文件复用上轮已核对快照，并依据完整PR文件集确认没有改变。`python verify_sources.py` 对九个文件均通过。

本地运行的是固定源码中的实际函数、真实SQLite事务和文件操作。SQL适配移除了PostgreSQL行锁/咨询锁、转换JSONB和类型转换；因此不证明PostgreSQL并发、RLS或Temporal历史重放。模型传输是可控替身。**本环境未安装tiktoken，安装尝试失败，使用明确标记的分词替身；不证明cl100k或供应商真实token计算准确。** 没有模型网络请求、真实密钥或供应商费用。

上轮58项场景的适配有两点：给新增加的分词依赖提供测试替身；超时测试原先硬编码50000的冻结金额断言改为等于对应请求的实际预留金额且大于零。后者配合本轮把余额全部预留的实现，不是删除预算检查。生产源码未修改。

远端CI #227（run `38820727360`）为success；仓库修复报告写有309 passed。这些是远端/作者记录，不算本地74项结果。未把此轮结果用于提升任何readiness状态，也未修改远端仓库。

## 已确认改善的场景

模型回执已经在答案文件之前落账。答案写盘失败后的串行重试以及调用中暂停后的恢复，不再在已测场景重复调用传输。普通未知费用响应保留已知token用量。过长输入、调用返回后超时、显式损坏来源都能在对应场景拦截。异步Activity在阻塞传输期间不再阻塞50ms ticker。上轮旧证据失效、共享答案任务引用、日志尾恢复、上传和归档检查继续通过。

这说明原修复不是无效的；下面是组合场景和未覆盖路径，不能用现有测试全绿代替它们。

## R5-01：数字集合相同仍会错误通过目标验收（高优先级）

位置：`services/worker/aether_worker/research.py` 的 `_covers`、`goal_completion_detail`，以及 `goal_effect.py:527–668`、`1112–1138` 的验证与目标判断。

复现来源：

```text
甲接口每分钟最多接受60次请求。
乙接口每分钟最多接受90次请求。
```

目标要求分别说明甲、乙限额。传输替身返回：

```text
甲接口每分钟允许90次请求，乙接口每分钟允许60次请求。
```

实际任务为SUCCEEDED、证据为PASS。数值都在，但对应对象反了。

额外三种实际复现也为SUCCEEDED：把“每分钟60次”答成“每秒60次”；把“最多60次”答成“可以超过60次”；目标同时要求限额和超时时间，而资料和答案只给限额。最后一种不能靠收集现有资料的重叠行识别遗漏目标。

根因：`_covers`在整句不匹配时，只检查资料行的数字是否都出现在整个回答中；它没有绑定对象、指标、单位、时间范围或比较关系。`required_facts`从已有资料中寻找词重叠，不是从批准目标中枚举所有必需输出。引用检测也只写入report，不能提供独立语义完成证据。

正确转述的阳性对照仍可成功，不能把所有非逐字回答都拒绝来解决。不得硬编码甲、乙、60、90等样例。对明确支持的提取任务，将目标要求、对象/字段、数值、单位和关系绑定到验收合同；无法可靠验证的自由文本不能得到强PASS。保留引用完整性与目标完成度的独立状态。

测试：`test_r501_semantically_wrong_answer_must_not_succeed` 的四个参数；`test_r501_positive_correct_answer_can_succeed[correct-paraphrase]` 为阳性对照。

## R5-02：已知用量仍在空正文或token超额路径漏入总账（高优先级）

位置：`goal_effect.py:879–1041`（空正文提前返回），`packages/storage/aether_storage/budget.py:283–389`（回执与聚合结算）。

场景A：传输正常返回空正文、input_tokens=100、output_tokens=20、cost=0.001美元。任务正确地没有成功，但请求被记成unknown，input_tokens/output_tokens/actual_micro_usd为NULL，dispatch_state仍是dispatched，聚合tokens_used=0。把费用改为未知也复现已知120 token未计入。

根因：`if not output` 在 `record_model_response` 之前调用mark_unknown并返回。正文是否可用不应该决定一次已经返回的调用有没有回执和已知用量。真实供应商是否产生这种响应本轮未验证；这里只证明系统收到这种明确响应时的确定行为。

场景B：输入回报2000、输出20，超出本次预留，任务进入NEEDS_REVIEW，这是正确的；请求行也保存了2000/20。但聚合tokens_used仍为0，因为记账只在tokens_fit时执行。原始数字没有消失，问题是总账与请求事实不一致。

应先记录任何收到的响应（含空正文、拒绝、截断、错误元数据和已知usage），再判断业务答案可用性。已知token应计入实际用量，即使超出预留；分别管理token、费用及未决金额。不得把真实超额强行记为零，也不能超额后继续派发。

测试：`test_r502_empty_answer_still_records_receipt_and_known_tokens` 两个参数；`test_r502_token_overrun_keeps_aggregate_actual_usage`。

## R5-03：任务已经PAUSED/CANCELLED，尚未开始的模型传输仍被调用（高优先级）

位置：`goal_effect.py:879–979`。准备请求之后、`gateway.complete`之前没有重新验证任务控制状态。

复现通过issue_token边界钩子插入实际的`apply_task_control_effect`调用：先完成令牌创建，再把真实SQLite任务状态迁移为PAUSED或CANCELLED，随后返回原令牌。传输替身尚未调用。两个场景下接下来仍出现1次模型传输调用，之后执行函数才返回PAUSED/CANCELLED。

这是“已停止后新请求仍发出”，不是要求撤销已经发出的请求。测试使用串行安排来固定竞态顺序，不是完整HTTP/Temporal并发测试。

应明确RESERVED、派发许可、已派发和已回执的状态及线性化边界。停止操作与新派发需要共享可验证的任务版本/栅栏或原子许可；不仅在执行最开始检查一次。已经发出的请求仍需收取回执并计账，不要求线程被强杀。未派发的请求安全释放与结果未知的请求必须区分。

测试：`test_r503_finalized_stop_before_transport_prevents_new_send[pause/cancel]`。上轮“执行之前就暂停”和“响应期间暂停、恢复不重打”仍通过。

## R5-04：已有答案恢复以及最终提交仍能越过墙钟期限（中高优先级）

位置：`goal_effect.py:879–895` 的已有答案提前返回；`execute_task_effect:163–236` 与 `_commit_terminal:1207–1232`。

场景A：预算active_seconds=1。模型响应与答案保存成功，在调用验证器时注入一次OSError，任务留在VERIFYING。真实等待1.1秒后重试执行。结果仍为SUCCEEDED，模型调用仍只有一次。无重复计费是改进；超时后仍判成功是未修复边界。

场景B：正常生成并验证之后、函数返回验证ID之前真实等待1.1秒，最终提交仍为SUCCEEDED。

根因：调用后的时限检查在`_ensure_live_answer`内部；它在已有答案时直接返回，最终提交也不核对deadline。应以不可变deadline贯穿读取缓存、恢复、验收和终态CAS。迟到产物/证据可以保留为历史，但不能承诺在时限内完成。

对照：在1800秒预算内同样的验证故障恢复能成功，仍只调用一次。PG候选测试使用回溯created_at到2小时前来固定时限，与本地真实sleep分开说明。

测试：`test_r504_retry_with_persisted_answer_rechecks_task_deadline[past-deadline]`、`test_r504_deadline_is_checked_at_final_success_commit`；within-deadline为阳性对照。

## R5-05：原样引用导致内容去重“占用”原资料的来源身份（高优先级，正常使用回归）

位置：`goal_effect.py:452–498`（`_bind_task_artifact`）与`740–793`（`_source_snapshot`）。

来源为R5-01的两行资料。模型正确地原样返回这两行。因为字节相同，ArtifactStore复用上传资料的artifact_id。`_bind_task_artifact`随后仍把artifacts.task_id从NULL写成当前任务。

实际观察：上传资料的字节仍存在、未tombstone、classification仍是project_private，但task_id变成task-1。未指定来源列表时，此资料被筛掉，结果NEEDS_REVIEW/UNVERIFIABLE；显式指定原资料时，来源检查变成invalid并失败。它不是物理文件删除，而是来源选择语义被破坏。模型仅增加一个“资料记载”前缀，就因字节不同而成功；这两个对照有助于排除模型内容或正确性问题。

根因：已经增加task_artifacts，多对多任务引用仍与旧artifacts.task_id占用语义并存。内容去重和资料角色没有彻底分开。

应以task_artifacts或等价显式关系承担任务关联，不因复用字节修改独立上传资料的来源身份。兼容旧数据时应做有测试的迁移/只读fallback，不能简单覆盖旧task_id，也不能关闭内容去重或禁止模型逐字引用。

测试：`test_r501_positive_correct_answer_can_succeed[verbatim]` 首先暴露此正常场景；`test_r505_exact_source_answer_keeps_explicit_source_readable` 验证显式名单；`test_r505_positive_quote_with_prefix_does_not_hide_source` 为阳性对照。

## 文件与运行

- `source/`：九个固定提交的源码子集，不是完整仓库，禁止覆盖项目。
- `audit_harness.py`、`task_harness.py`、`source_loader.py`：隔离适配，注释说明替身边界。
- `test_round5_regressions.py`：本轮16个检查；其余test文件为复查的58项场景。
- `repo_tests/test_audit_round5.py`：15个参数化PostgreSQL候选用例，复用仓库Round-4 setup；仅语法检查，未执行。它们不是生产补丁。
- `evidence/all_checks.txt` 与 `all_results.xml`：最终完整运行证据。
- `evidence/harness_draft_*`：早期测试钩子遗漏位置参数的开发记录。已修正测试钩子签名，未改生产函数；这些TypeError不计入最后13个产品失败。

运行隔离检查：

```sh
python verify_sources.py
python -m pytest -q --tb=short
```

当前固定源码预计退出码1，因为存在13个已说明失败；不允许改成xfail/skip伪装全绿。全量生产环境验收还需PostgreSQL、真实tiktoken、Temporal和受控真实模型测试。

## 审计结论

请求回执优先落账和把阻塞调用移出事件循环的改动有效，应保留。下一批修复重点是语义验收、派发时停止边界、已知用量的所有响应分支，以及源资料与任务引用的分离。不要更换框架，不要增加更多功能来掩盖这些缺口。

## 固定源码链接

- https://github.com/Jerry2003826/aether/blob/aedbdd54dccaba39e79282f87c09bab6a6dcc873/services/worker/aether_worker/goal_effect.py#L879-L1041
- https://github.com/Jerry2003826/aether/blob/aedbdd54dccaba39e79282f87c09bab6a6dcc873/services/worker/aether_worker/research.py#L156-L220
- https://github.com/Jerry2003826/aether/blob/aedbdd54dccaba39e79282f87c09bab6a6dcc873/packages/storage/aether_storage/budget.py#L283-L389
- CI：https://github.com/Jerry2003826/aether/actions/runs/38820727360


## 打包后复核

ZIP已解压到独立目录。九个源文件Git blob检查全部匹配，再次运行同一74项检查仍是61通过、13失败、0错误、0跳过，耗时8.43秒。记录在`evidence/reextracted_checks.txt`、`reextracted_results.xml`和`reextracted_sources.json`。最终包包含这些证据，CRC和SHA256清单已复核。
