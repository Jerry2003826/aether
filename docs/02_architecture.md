# 02 · 架构与唯一权威

## 1. 逻辑分层

```text
Next.js Workbench
       │ HTTPS / REST / SSE
FastAPI API ── PostgreSQL(domain, commands, operations, projections)
       │ durable command outbox
Temporal Server ── TaskWorkflow / ResearchWorkflow / WatchWorkflow / SkillEvaluationWorkflow
       │ activities (all non-deterministic I/O)
AETHER Worker
  ├─ Goal compiler / bounded typed turn loop / DAG scheduler
  ├─ Knowledge & search adapter ── BM25S snapshots + pgvector
  ├─ Trusted Gateway ── model calls / approved tool dispatch / cost & capability checks
  ├─ Runner Manager ── isolated OpenHands coding jobs
  │     └─ Optional CodeGraph repo intelligence ── scoped source graph cache
  └─ Independent Verifier ── clean test job / evidence validation / human acceptance

Ingestion & ML worker ── HTTPX + Trafilatura + feedparser + Docling + FlagEmbedding
Artifact store ── project-scoped content-addressed files
Optional observability ── OpenTelemetry / Langfuse
```

这是逻辑图，不要求每一项成为微服务。API、领域模块、worker 共用一个 Python monorepo。网关、runner-manager、verifier 分开进程/凭证，原因是权限隔离，不是追求微服务数量。ML 和 OpenHands 分环境以控制依赖冲突。

## 2. 权威归属

| 状态 | 权威 | 其他模块怎么读取 |
|---|---|---|
| 执行进度、DAG、等待/取消、重试计划 | Temporal Workflow history | Query/update 与 PostgreSQL 可重建投影 |
| 用户目标、合同、权限、审批、成本账本、知识版本 | PostgreSQL | API/activities；workflow 只引用版本与 ID |
| 某次外部操作实际是否发生 | 外部系统 + operation ledger 的对账证据 | gateway reconcile，不能只看 workflow 状态 |
| 字节产物 | ArtifactStore 的不可变对象 | ID+SHA256，读取时鉴权 |
| 编码会话内的临时执行 | OpenHands job/conversation | 有界执行器，不调度应用级任务 |
| 技能当前启用版本 | PostgreSQL skill deployment record | 固定版本注入；会话中途不悄悄切换 |

PostgreSQL 的 task.status 是投影，不是另一套调度器。禁止 API 直接写 SUCCEEDED。调度器崩溃恢复依赖 Temporal，不从 UI 状态猜测下一步。

## 3. 模块/目录目标

```text
apps/web/                          # Next.js TypeScript
services/api/aether_api/            # auth, REST, SSE, commands
services/worker/aether_worker/      # Temporal workflows/activities
services/gateway/aether_gateway/    # model & tool execution admission
services/runner/aether_runner/      # trusted Docker/remote job lifecycle
services/verifier/aether_verifier/  # deterministic/human evidence checks
services/ml/aether_ml/              # embedding, reranker, parsing
packages/domain/aether_domain/      # typed contracts, no network I/O
packages/knowledge/aether_knowledge/
packages/skills/aether_skills/
packages/evaluation/aether_evaluation/
packages/adapters/aether_adapters/  # only sanctioned third-party boundaries
migrations/                        # Alembic, derived from schema contract
infra/                             # compose, locked image refs, healthchecks
examples/                          # real runnable user demos
```

`reference/` 和 `contracts/` 为规格基准，生产包不得通过修改它们来躲过验收。可以把参考逻辑提取到 domain 包，再让同一组测试同时验证生产实现。

## 4. 核心接口

`RepositoryIntelligence.query/impact/affected_tests(snapshot,scope)` (optional; see `docs/13_codegraph.md`); `ModelGateway.generate(request) -> ModelResult`；`ToolGateway.execute(intent) -> OperationResult`；`CodingExecutor.start/get/cancel(job)`；`Verifier.verify(request) -> VerificationResult`；`Knowledge.search(request) -> SearchResult`；`SkillRegistry.resolve(scope,task_type,version)`；`ArtifactStore.put/get/delete`。

接口详见 `contracts/interfaces.py` 和 `contracts/domain.schema.json`。这些是 AETHER 自己的接口，不是声称上游仓库存在同名方法。

## 5. 调度方式

默认一个 planning actor；任务节点按角色使用 research、coding 或 extraction 执行器。并行最多两个读节点，写同一资源严格串行；编码节点默认一个。验证器只检查结果，不参与规划。禁止 agent-to-agent 无界群聊。

Planner 产生 DAG 时校验节点唯一、依赖存在、无环、并发/预算和最多32步依赖路径上限；Agent递归委派是另一项限制，V1禁用。只有全部依赖已验证通过的节点能被调度。失败重规划不能删除原始合同中的验收条件。

## 6. 禁止隐式控制路径

UI 不直接访问 OpenHands Agent Server、Temporal 管理接口或 LiteLLM。插件不能直连数据库。Actor 不能写技能生产目录。LLM 不能通过工具结果重定义权限。对所有下游 executor 做资源限额与端到端截止时间检查，而不是仅在提示词里写“不要超时”。

## 7. 开工时的最小物理拓扑
后端共享源码而非强制共享身份：api、workflow worker、gateway、verifier各用独立进程与DB角色。runner-manager独占专用Docker控制权；ML按需profile。无需引入单独的IAM/MCP网关产品。Worker通过gateway审批后的工具请求运行，不能为了先做演示临时在worker直接exec。
上下文编译器是worker内的明确模块，候选上下文来自已鉴权Knowledge/Memory/Skills/RepoIntelligence；生成模型只给Decision，不决定权限或最终成功。
