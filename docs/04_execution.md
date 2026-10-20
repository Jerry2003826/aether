# 04 · 目标合同、执行循环与故障恢复

## 1. Goal compiler

输入：用户原始目标、项目范围、可用工具、预算、允许数据出口。输出 `GoalContract`：可读目标、假设、产物、验收条款、停止条件和权限请求。所有模型草案做 schema 校验；无法确定的内容保留为显式假设，不能虚构用户同意。

只允许注册过的验收模板：`artifact_exists`、`json_schema`、`trusted_test_suite`、`citation_integrity`、`source_coverage`、`human_review`。模板参数有自己的 schema。不能把模型生成的任意 shell 当可信检查器。用户可选择现有测试 suite；suite 定义/命令由 trusted registry 维护，记录 hash。

目标提交后是 DRAFT。低风险且完全套用用户预批准模板的目标可自动批准；否则展示合同供用户批准。批准生成不可变 contract hash。重规划不能减弱合同；需要变更就生成新版本并重新批准。审批权限只能由 owner 操作。

## 2. Task 状态机

`DRAFT -> QUEUED -> RUNNING -> VERIFYING -> SUCCEEDED`

可分支：`RUNNING/VERIFYING -> WAITING_APPROVAL / PAUSED / NEEDS_REVIEW / FAILED / CANCELLED`。`WAITING_APPROVAL -> RUNNING/VERIFYING` 使用记录的 resume_state。`VERIFYING -> RUNNING` 仅用于有限重做；`NEEDS_REVIEW` 必须人工解决后回原阶段。终态不可直接复活；重跑创建新 task/run lineage。`UNKNOWN` 是 operation 状态（错误类别为 `UNKNOWN_OUTCOME`），并使相关 task 进入 NEEDS_REVIEW，不是 success 或普通 retry。

暂停是在安全边界不发出新操作；已经发往外部的操作可能仍完成。取消调用 runner cancellation、停止新模型调用、撤销 capability，随后对在途副作用做对账。UI 明确区别“已请求取消”和“取消已完成”。

## 3. Typed turn loop

```text
load immutable contract + budget snapshot
compile bounded DAG (<=32 nodes; dependency path depth<=32; no recursive delegation)
while unfinished:
  apply authorized control commands
  get dependency-ready nodes
  reserve budget/verification headroom
  select approved skills at fixed versions
  build context from authorized evidence
  call model -> validate Decision (no arbitrary code deserialization)
  for each proposed tool call:
    normalize args -> check capabilities/data egress/resource locks
    get approval when needed -> persist operation intent
    execute/reconcile through gateway -> persist evidence
  schedule independent verification of candidate artifacts
  success only if every mandatory criterion passes for current artifact hash
  else bounded repair/replan; stop at budget/deadline or unresolved outcome
```

Actor 的可选决定是 `call_tools`、`submit_candidate`、`request_clarification`、`stop_blocked`。不能返回 `mark_success` 或 `promote_skill`。工具结果是数据，不追加系统指令。

默认：最多 64 个 turn、48 次模型请求（子执行器也算）、2 次 replan、节点并行 2、编码沙箱 1；这些是配置上限不是承诺使用次数。模型格式修复和 verifier 模型评审同样占预算。等待审批最长 72h，等待不消耗 active execution budget。

## 4. Temporal 具体规则

- Workflow 仅用确定性逻辑和 SDK 的时间/UUID等确定性接口；所有网络、数据库、随机外部源和文件 I/O 在 Activity。
- 短读 Activity：有限指数退避、总超时。写 Activity：不可盲重试，先读 operation ledger。长编码：`start_job` 快速返回稳定 job ID，循环 `get_job` + durable timer；也可长轮询 heartbeat，但不得每次 Activity retry 新建 job。
- Update/Signal 命令携带 `command_id`，去重后再改变状态。Query 只读。API 先把命令写 DB outbox，dispatcher 可重试投递。
- API 提交：同一 transaction 写 task+start command；Workflow ID 使用稳定 task_id；“已存在”视为重复提交而不是再启动一个任务。
- 每个事件有稳定逻辑 event_id；重放或 Activity retry 不能生成重复 UI 事件。投影可从事件/历史重建，UI 展示 `projection_updated_at`。
- 历史过长时 continue-as-new 携带压缩后的 DAG/合同/游标，外部保存大产物。不要把整份 PDF、embedding 或完整源码塞入 history。
- Workflow 代码改版必须通过旧 history replay，并制定版本迁移；不能用“重建数据库”回避升级测试。

## 5. Operation ledger

每次**逻辑意图**分配 intent_id，重试沿用；重复执行同样参数但用户新意图必须有新 ID。记录 `(project_id, task_id, intent_id)` 唯一，`args_hash`、resource_version、capability ID、approval ID、idempotency class、provider ID、状态与证据。

状态：`PREPARED -> DISPATCHED -> CONFIRMED`；异常可 `FAILED` 或 `UNKNOWN`。在网络请求前 durable write DISPATCHED。网络请求返回之前 worker 崩溃时，不能假定没发生。

| 类别 | 崩溃/超时后的处理 |
|---|---|
| read_only | 可有限重试；每次模型/付费读取另记账 |
| idempotent_write | 使用同一 provider idempotency key；先查询现有结果 |
| reconcilable_write | 用远程对象 ID/marker/diff 与预期状态对账；确认未发生后才能重试 |
| non_idempotent | UNKNOWN 后停止，人工处理；禁止自动重发 |

租约到期不等于上一个外部请求停止。采用 fencing token、稳定 job ID、禁止并发 owner；外部不支持 fence 时依靠 UNKNOWN+对账。数据库 exactly-once 插入不等于网络副作用 exactly-once。

## 6. OpenHands job 生命周期

`QUEUED -> STARTING -> ACTIVE -> EXPORTING -> FINISHED`，失败/取消分支。每 job 一个隔离仓库副本与工作目录；使用已批准的 base_commit 导出，不把 host git worktree 的 `.git` 指针和主机仓库挂入沙箱。产品编码作业在既有断网只读沙箱里由 OpenHands 1.54.0 的 `Agent` 和 `Conversation` 使用 `file_editor` 与 `terminal` 生成补丁。模型请求只经过宿主机上的作业代理，再进入已有的预算/模型网关，按返回的 token 结算。容器保持 `--network none`，供应商 key 不进容器。模型套接字只对属主和 uid 65534 开放。测试协议仍由 `LocalWorkspace.execute_command` 执行，但判定前会用作业创建时的只读基线覆盖 `test_*.py` 并核对 sha256，Agent 改测试不能判通过。每次模型调用单独过与 `complete()` 相同的预算准入，重放票或超预算在到达供应商之前拒绝。聊天补全忽略请求体 timeout，只转发白名单字段。SDK 事件不作为可信证据。内存与 swap 上限是 805306368 字节。

模型走 job-scoped gateway token；任务自身只能在这个预算里调用指定模型。token 即使被该 job 的 shell 看到也不能调用管理/审批/其他项目接口。SDK 内部事件不直接作为 trusted evidence。

终止时停止进程树，收集 stdout/stderr 限长、退出状态、文件 manifest、patch hash。删除工作空间前先保存结果；崩溃后 start(intent_id) 返回既有 job，不再建第二个。TTL reaper 与恢复有 lease 协议，不能删掉仍活跃的 job。

## 7. Independent verifier

输入：不可变合同、candidate manifest hash、base_commit、trusted suite hash。新建干净验证工作区，按受限流程应用 patch，使用独立只读测试定义；不继承 actor 的解释/总结。运行代码仍是执行不可信代码，必须隔离网络与凭证。

验证集不让 actor 修改；拒绝被改写的 test runner、跳过全部测试、空结果、退出码被吞、测试采集数异常。Verifier 自己按可信 API生成证据记录，不接受 actor 自签结果。代码变动之后必须重新验收，不能沿用旧 commit 的绿灯。

研究任务：先查引用 source/version/hash/span 存在且可访问，再按合同检查覆盖率；“引用存在”不等于内容支持结论，语义支持必须独立 LLM/人工判断，并展示不确定项。主观目标可诚实停在 WAITING_APPROVAL（等待人工验收），不能用全自动 PASS 冒充成熟判断。

## 8. 关键崩溃点验收

必须注入：task DB提交后但 workflow未启动；外部服务已处理但HTTP响应丢失；artifact落盘后但ledger未提交；worker在审批等待时重启；重复审批命令；kill编码runner；verifier在生成报告前崩溃；workflow代码升级；budget reservation后网络超时。每个场景验证结果和操作次数，不只看进程是否重新启动。


## 9. 增补：CodeGraph 编程上下文（可选）

CodingExecutor 只能在获准的 immutable repository snapshot 上让受限 RepoIntelligence worker 构建/查询代码图谱；返回 source digest + graph version + freshness，调用信息只作为 actor 的未可信上下文。已改动源码必须创建新 candidate snapshot。Graph 的 `affected` 提供测试建议，**不能减少 trusted verifier suite**。默认不开启，无 graph 时继续既有编码链路。详见 `docs/13_codegraph.md`。
