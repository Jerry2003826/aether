# AETHER V1.4 · 完整开工规格

由本包docs章节合并的阅读版。维护时以docs章节为源，冻结裁决由docs/00_FREEZE.md优先。

# 00 · 最终裁决：这一轮之后开始实现

版本：V1.4；裁决日期：2026-10-15。范围是工程设计冻结，不是承诺尚未实现的性能。

## 1. 产品定义
AETHER 是本地/私有部署、面向一个所有者的可持续工作 Agent 工作台。用户可以让它研究资料、修改授权代码仓库、跟踪明确主题；能看见实际过程、审批、成本、证据、失败与恢复；经验以可删除记忆和经过评测的技能保留下来。
“不依赖 Google”指核心检索、索引和任务运行不依赖 Google 搜索或 Google 云服务。用户主动配置的模型供应商是独立、显式的数据出口。初版不是全互联网搜索引擎，而是授权来源集合上的自主采集/索引/检索。

第一版只证明系统工作、边界有效和学习管线可检验；是否优于 Hermes 是另一个实验结论。

## 2. 继承与修正
保留 AETHER 原主干：Temporal、OpenHands SDK、独立验证、自主混合搜索、证据记忆、预算审批和 Skill Lab。
采纳 DeepSeek 的凭证代理、工具渐进加载、消息去重/发件箱、沙箱驱动边界、供应链关卡；不采纳它的 LangGraph 主编排及 APScheduler/Restate/DBOS 换栈路线。

六项收敛：
1. 只保留一个任务清单；原候选实验不是必须安装列表。
2. F00 不再要求先试完所有模型/记忆/沙箱再开工。核心环境锁定后，编码与ML兼容探针可并行；缺密钥只阻塞 live 检查。
3. 安全准入早于真实工具执行，最小界面与纵向链路一起交付，而不是最后再画控制台。
4. 原生 PostgreSQL 记忆先完成产品需求；Hindsight 是以后唯一优先评估的高级记忆候选，Graphiti 等不并列进入当前施工。
5. 纠正 DAG 的默认深度：最多32节点、最多32节点长的依赖路径；这是工作步骤，不是递归委派深度。V1禁止执行器继续孵化下一级自治 Agent。
6. 工程完成、live就绪、增强启用、效果领先分别验收。没有正收益技能并不代表学习管线没实现。

## 3. 不再交给 Codex 猜的选型
| 能力 | 唯一默认实现 | 边界 |
|---|---|---|
| 持久任务 | Temporal Python SDK | 不另建任务重试/恢复权威 |
| 核心逻辑 | AETHER typed loop + Pydantic contracts | LLM提议，可信服务授权与判定 |
| 编码 | OpenHands SDK 有界job | patch是候选，不是自动批准提交 |
| 模型 | AETHER gateway 内的 LiteLLM SDK | 不启用它的其他harness/MCP/调度功能 |
| 搜索 | BM25S + pgvector + BGE | project级索引，原文证据保留 |
| 记忆 | PostgreSQL事实/事件/关系表 | 所有派生索引都可重建、纠正、删除 |
| 网页 | HTTPX/Trafilatura；确需JS时Playwright | 不默认叠加Crawl4AI/agent-browser/browser-use |
| 工具 | MCP官方Python SDK + 本地注册表 | 明确许可、固定schema、独立网关 |
| 编码结构 | CodeGraph选定增强 | 经过独立启用检查；无图谱可降级 |
| 界面 | Next.js + TypeScript + REST/SSE | 不等待AG-UI，未来可转换事件 |
| 评测 | pytest/真实验证器 + 独立pydantic-evals | 测试器不调度产品业务 |
| 存储部署 | PostgreSQL + 私有本地artifact卷 + Compose | 不先引入向量DB/图DB/Kafka/K8s |

## 4. 现在做、后做、明确不做
**完整V1必做**：身份与project隔离；可恢复执行；审批/预算/取消/unknown对账；研究与混合检索；代码快照、OpenHands、独立验收；原生记忆更正/遗忘；审核过的MCP；站内主题跟踪与简单可解释推荐；技能候选/评测/人工批准/回滚；真实UI；备份恢复和发布检查。
**V1选定增强**：CodeGraph实现和接入任务保留，但不是内核上线依赖；安装/隐私/索引探针通过后才可启用。失败不能假装集成完成。
**下一轮才做**：Hindsight高级记忆、DSPy候选优化、AG-UI/A2A/MCP Apps、microVM替代后端、复杂浏览器代理、更多消息/语音渠道、模型训练。
**不在本产品首版内**：公开SaaS多租户、计费平台、全互联网爬虫、通用桌面接管、自动付款/部署/发信、自己改写控制面、无限多Agent群聊。

## 5. 体量控制，不牺牲关键边界
这不是十几个独立微服务团队：同一后端源码镜像可运行API、worker、gateway、verifier不同进程；用不同角色/凭证/网络分权。ML与OpenHands因依赖和执行风险可独立镜像。Postgres物理实例可共用，AETHER和Temporal使用不同数据库与账号。
第一条闭环只需一个研究任务、一个真实本地文档工具和一个可信验收器，不要求先把全部49个API操作做完。后续阶段补齐完整合同，未实现入口明确返回未启用而不是假数据。

## 6. 三种就绪状态 + 一种研究结论
- **内核工程就绪**：真实Postgres/Temporal/网关/证据/最小UI和故障用例通过；模型可为scripted，标签必须明确。
- **产品工程就绪**：全部核心任务实现，真实第三方库/本地服务集成通过；live、平台缺项单列，不能称可交付live版。
- **live可用V1**：真实模型研究和编码演示、检索权重、Linux runner隔离、备份恢复及安全关卡实际通过。没有API key时不勾此项。
- **领先/进化有效**：冻结模型/环境/预算/数据的独立实验得出；不作为启动开发或工程管线完成的前置要求。

初始价格和版本并未由本包虚构锁定；F00/F01在目标环境生成锁。设计冻结不是依赖盲锁，也不是不准修复真实缺陷。


---

# 01 · 产品范围与成功定义

## 1. 产品定位

AETHER 是个人使用、可扩展到小团队的自托管 Agent 工作台。V1 的初始发布是**单账户、多项目**；不同项目仍必须强隔离。研究资料、搜索索引、偏好和执行记录均由自己控制。它以 ResearchOS 式技术资料研究为主要知识场景，并可在隔离环境执行代码任务。

“先进”定义为可以测量的结果，而不是 Agent 数量：任务结果真实可验、失败后可恢复、同等结果成本更低、经验在未见任务上有用。是否超过 Hermes 留给对照实验，不预先宣称。

## 2. V1 必须完整实现的用户故事

| 编号 | 用户故事 | 最终可见结果 |
|---|---|---|
| U01 | 提交自然语言目标，检查系统理解与验收条件 | 可编辑草案、批准后的不可变合同、预算 |
| U02 | 调研某技术并比较多个方案 | 可追溯来源的报告、搜索过程、局限说明 |
| U03 | 在指定仓库快照中实现一个功能 | 独立 patch、测试日志、差异预览；不自动改主分支 |
| U04 | 中断程序/重启机器后继续任务 | 恢复记录；不重复不可逆操作 |
| U05 | 审批需要外部副作用的动作 | 具体目标、参数、影响、有效期；拒绝后不执行 |
| U06 | 纠正记忆、查来源、删除某项目资料 | 冲突解释、历史版本、删除与索引失效可验证 |
| U07 | 搜索自己的技术资料并获得推荐 | 混合搜索、重排、推荐理由与反馈入口 |
| U08 | 订阅授权来源的变化 | 定时增量采集、差异摘要、站内通知 |
| U09 | 看见系统从任务中提议新技能 | 候选 diff、独立评测、批准/回滚 |
| U10 | 看清模型成本和所有执行证据 | 单次请求到任务汇总；未知成本明确显示 |
| U11 | 在无外网和无付费模型时体验演示 | 基于本地夹具与 scripted provider 的真实系统链路 |
| U12 | 接入一个经管理员审核的 MCP 服务 | 受限工具列表，schema 固定，权限与审批不旁路 |

## 3. 不纳入 V1，但要保留清晰接口

不做企业计费、组织 SSO、完整办公 IM 网关、公开互联网全量搜索引擎、社交协同过滤平台、手机/桌面全 GUI 控制、基础模型微调、自动修改自身控制代码、生产集群调度。不是以未来模块替代当期功能：V1 仍要完成上表全部核心闭环。

核心不依赖 Google 的含义：不使用 Google 搜索/API/身份服务/云服务作为必要条件；资料可来自授权站点、RSS、arXiv API、GitHub API及本地文件。公开网页发现范围受已配置来源限制，不把有限语料库称为全球互联网索引。云模型也是可替换依赖；离线模式不承诺达到云模型质量。

## 4. 初始容量边界（设计预算，不是实测）

每项目上限 50,000 chunks；默认最多 2 个活跃工具节点、1 个编码沙箱；单账户；Linux amd64 为完整编码沙箱验收平台，macOS arm64 为控制台/研究链路验收平台。CUDA 仅是 embedding/reranker 的可选加速；控制服务不依赖 GPU。运行资源须由 F01/F13/F25 实测，不给出未经测试的内存或吞吐保证。

## 5. 三种完成状态

- **工程可用**：功能、故障、安全与本地演示验收通过。
- **真实模型可用**：真实模型完成研究任务与代码任务，所有账单和工具调用可追踪。
- **有比较优势**：冻结实验中，在明确任务分布、模型和成本口径下优于基线。

前两项不自动推出第三项。没有真实模型凭证时只能交付“工程验收完成、live 验收被阻塞”，不能标全量 V1 完成。


---

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


---

# 03 · 仓库复用定案

只复用本表列出的需要部分。目标路径是待Codex实现的AETHER适配器位置，不声称上游有同名方法。
仓库首页可访问与安装版本兼容是不同证据；F00/F01/F25生成精确锁与许可记录，本文件不编造版本、SHA和镜像digest。

| 仓库 | 用什么 | 放在哪里 | 禁止混淆 |
|---|---|---|---|
| `temporalio/temporal` (core) | Temporal server，独立数据库/namespace与持久服务 | `infra/temporal/` | 不把开发临时服务当正式持久运行 |
| `temporalio/sdk-python` (core) | temporalio.workflow/activity/worker/client/testing；Workflow/Activity/重放 | `services/worker/aether_worker/` | Workflow中无网络DB文件I/O |
| `fastapi/fastapi` (core) | API routing/Depends/OpenAPI，认证逻辑属于AETHER | `services/api/aether_api/` | 不使用BackgroundTasks代替持久任务 |
| `pydantic/pydantic` (core) | BaseModel/TypeAdapter/严格输入合同 | `packages/domain/aether_domain/` | 类型校验不等于鉴权或深度不可变 |
| `sqlalchemy/sqlalchemy` (core) | session/transaction/query与runtime角色 | `packages/storage/aether_storage/` | 不用superuser连接做应用运行 |
| `sqlalchemy/alembic` (core) | 增量迁移与升级路径 | `migrations/` | 不通过删库解决schema冲突 |
| `BerriAI/litellm` (core) | Python completion/acompletion provider adapter，实际能力探针 | `services/gateway/aether_gateway/providers/` | 不启用agent harness/MCP/独立预算权威；外层含SSRF出口限制 |
| `OpenHands/software-agent-sdk` (core) | openhands-sdk/tools/workspace/agent-server四包；LLM/Agent/Conversation/FileEditorTool/TerminalTool/隔离workspace | `packages/adapters/aether_adapters/openhands.py` | 只做有界job；选中release验证配置/生命周期，不能从README推断恢复和隔离自动成立 |
| `pgvector/pgvector` (core) | 向量类型、cosine距离；小语料先精确召回 | `packages/knowledge/aether_knowledge/vector.py` | 不用ANN速度掩盖授权过滤后的召回下降 |
| `pgvector/pgvector-python` (core) | SQLAlchemy类型绑定 | `packages/knowledge/aether_knowledge/vector.py` | 维数/revision写入metadata |
| `xhluca/bm25s` (core) | BM25索引、retrieve、save/load；冻结分词词表 | `packages/knowledge/aether_knowledge/lexical.py` | 项目级不可变generation+原子切换；不是事务增量引擎 |
| `fxsjy/jieba` (core) | 中文词切分与冻结词典 | `packages/knowledge/aether_knowledge/tokenize.py` | 不能把token offset当引用原文offset |
| `FlagOpen/FlagEmbedding` (core) | BGE-M3 embedding、bge-reranker-v2-m3 re-rank，单独ML环境 | `services/ml/aether_ml/` | 模型权重revision与许可单独锁；CPU/GPU按实测 |
| `encode/httpx` (core) | 受控HTTP客户端/timeout/stream读取 | `packages/adapters/aether_adapters/http_fetch.py` | 自写目标授权/DNS连接绑定/限流；不用trust_env绕过出口 |
| `adbar/trafilatura` (core) | HTML正文抽取 | `services/ml/aether_ml/parsers/` | 下载在网关完成；不让解析库自由抓站 |
| `kurtmckee/feedparser` (core) | parse已下载RSS/Atom | `services/ml/aether_ml/parsers/` | 先限制网络/大小再解析 |
| `docling-project/docling` (core) | 本地文档转换器/结构化文本 | `services/ml/aether_ml/parsers/` | parser隔离，扫描件OCR需显式启用 |
| `microsoft/playwright-python` (core) | browser/context/page与固定浏览器版本 | `packages/adapters/aether_adapters/browser.py` | JS fallback不是自由browse；工具profile默认off |
| `modelcontextprotocol/python-sdk` (core) | 官方client/session/stdio或Streamable HTTP transport | `packages/adapters/aether_adapters/mcp.py` | 固定server/schema并本地分类授权，不自动发现安装 |
| `agentskills/agentskills` (core_spec) | SKILL.md格式与渐进披露规范 | `packages/skills/aether_skills/` | 格式复用，不下载整个公开技能库 |
| `vercel/next.js` (core) | Next.js/React/TypeScript工作台 | `apps/web/` | 不让前端直连model/runner/Temporal admin |
| `shadcn-ui/ui` (core_ui) | 仅选所需基础组件 | `apps/web/src/components/` | 不一键引入整套无用页面 |
| `pydantic/pydantic-ai` (dev) | 仅独立pydantic-evals：Dataset/Case/evaluator，按所选release实测 | `packages/evaluation/aether_evaluation/` | 不引入Pydantic AI Agent运行时替代Temporal |
| `anchore/syft` (dev_release) | CLI扫描实际镜像/文件系统产SBOM | `scripts/supply_chain/` | SBOM是清单不是无漏洞证明 |
| `google/osv-scanner` (dev_release) | CLI依赖漏洞扫描；优先支持的本地数据库/离线模式 | `scripts/supply_chain/` | 不是Google搜索/云runtime依赖；网络查询/隐私要明确，库更新后离线扫 |
| `colbymchenry/codegraph` (selected_optional) | 固定CLI/MCP codegraph_explore，impact/affected由适配器按需调用 | `packages/adapters/aether_adapters/codegraph.py` | 本地索引有遥测开关；任意projectPath不透传；snapshot级权限 |
| `NousResearch/hermes-agent` (baseline_only) | 固定release独立CLI/环境，cold与learned基线 | `packages/evaluation/aether_evaluation/baselines/` | 不是生产依赖或第二调度器；没有实测不写排名 |

## 安装顺序
F00只锁控制面/数据库/前端/Temporal与领域依赖。F01在隔离环境验证OpenHands/ML；不让它阻塞F02–F09本地闭环。F10/F17再按功能引入解析/浏览器/MCP。F15单独CodeGraph，F21独立evals，F25发布扫描。
许可证审计读取实际固定版本的LICENSE/NOTICE和传递依赖；格式规范、软件包、容器及模型权重许可分开记录。公开GitHub代码不自动等于允许任何商用。未审计的第三方代码不复制到主代码树。

Langfuse只保留未来可接的可观测出口；基础日志、事件、成本和审计由AETHER自身提供。Hindsight等全部在tasks/deferred.yaml，不加入默认安装命令。


---

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


---

# 05 · Knowledge Fabric、搜索、记忆与推荐

## 1. 知识对象与来源

每份资料分成 `Source -> SourceVersion -> Chunk`。Source 是稳定入口，SourceVersion 保存抓取时间、内容时间（未知则 null）、原始/规范化内容 hash、语言、解析器版本、可见项目；Chunk 保存 canonical text 的 code-point offset、页号/段落标识和向量模型版本。摘要不能替代原文证据。

每条记忆是 `MemoryRecord`：kind（semantic/episodic/procedural）、category（fact/preference/procedure/episode/project_decision）、claim、epistemic_status（proposed/source_claim/user_confirmed/observed_test/inference/superseded）、来源证据、scope、valid_from/valid_to、recorded_at、置信等级及其产生方式。冲突由conflict_group标记，删除由deleted_at表示，不把来源可信度和生命周期混成一个字段。置信等级是启发式，不假装经统计校准的事实概率。

用户明确陈述、网站声称、Agent 推断、实际测试结果分开。冲突不覆写：产生 conflict group，保留双方证据与时间，让用户或任务消解。重要项目决策优先显式确认；敏感个人信息不自动跨项目共享。

## 2. 采集链路

```text
approved SourceConfig
  -> quota / robots / source permission / HTTP safety checks
  -> conditional GET (ETag / Last-Modified when available)
  -> immutable raw artifact + metadata
  -> isolated parser -> canonical text + locator map
  -> exact hash dedupe / near duplicate grouping
  -> versioned chunking
  -> embedding job
  -> PostgreSQL commit + index update outbox
  -> BM25S generation build + atomic pointer swap
```

默认输入：授权 URL、RSS/Atom、GitHub public API、arXiv API、上传 Markdown/纯文本/文本PDF、WebVTT字幕。私有 GitHub 连接是管理员配置的受限 connector，凭证不传 actor；不在 V1 假装支持所有私人平台。

网络每来源并发1、请求间隔初始1秒、HTML/RSS/VTT body默认10MiB、HTTP总硬上限20MiB、本地PDF上传默认25MiB；遇429服从 Retry-After，拒绝未经授权的登录/付费墙绕过。限值是设计初值。重复源 canonical URL 和内容hash去重，历史版本仍可引用。压缩文件默认不展开；未来展开必须有zip-slip、symlink和解压比/总大小限制。

解析失败保存 error、原始hash、失败阶段，可重试；不要悄悄存空正文。Docling/HTML parser 都按cpu/time/memory上限运行；OCR只给明确扫描件任务，记录费用/耗时，不代替文本解析。

## 3. Chunking

按标题/段落/代码块优先切分，初始目标 embedding-token 512、overlap64、单chunk最多1024tokens。不要将巨大代码块硬塞进一个向量；保留 parent section link。引用 offset 总是针对规范化文本，而不是后续分词文本。对中文、emoji、合成字符、CRLF、HTML实体做 offset 测试。

生产chunk_id使用UUIDv5，由project namespace与source_version_id/chunking_version/span确定性生成；原始摘要单独作为内容hash，不把64位hex塞进UUID字段。重切分产生新版本，不复用旧 chunk_id 指向新内容。embedding 维数、模型revision、归一化方式和 tokenizer version 一并存储；禁止不同embedding模型的数据混用。

## 4. 词法索引

BM25S `method=lucene,k1=1.2,b=0.75`。`Tokenizer` 采用冻结中文jieba词典+英文/代码分词；query词表禁止自增长破坏索引。停用词不要删除有意义的代码符号或否定词。未知词得到空或低召回，不导致异常。

每项目一个不可变generation。构建worker从固定数据库快照导出有序chunk IDs与文本，保存BM25矩阵/词表、映射、source watermark和manifest hash；写临时目录后校验，原子切换active generation。查询拿到generation句柄后整个请求使用同一版本，引用计数结束才GC旧目录。进程并发构建通过数据库advisory lock/lease避免覆盖。

明确限制：BM25S不是我们的事务型增量索引；批量重建默认debounce30秒。UI展示index_lag。新资料只有当chunk/vector/lexical符合最低合同才显示ready；向量失败可显式lexical_only，不假装完整hybrid。采集作业构建索引时，测量表里已有的向量照常写入。未进入测量表的正文，若本机没有已固定修订的嵌入权重，或 ML 环境不能离线导入，这条作业不下载模型，该段向量留空；词法索引仍用已抽取的文本发布。显式重建仍可启动编码进程。等待上限仍是 90 秒。

删除/撤权立即进入tombstone，所有搜索结果在返回、重排、组装LLM上下文之前再次查权限/有效性；随后重建index并清理磁盘。在V1项目内权限一致，不承诺细粒度每段ACL；跨项目索引绝不合并再先检索后过滤。

## 5. Hybrid retrieval

查询输入先鉴权。原query必保留；可最多产生2个受预算限制的改写，不让改写替换用户真实意图。默认每路top50：BM25与dense cosine。融合用 RRF：`sum(1/(60+rank))`，rank从1开始、同一路重复ID只计一次。初版固定权重相等；不把不同数值尺度的BM25和cosine直接相加。

RRF top30交 `BAAI/bge-reranker-v2-m3`，返回top8；每份文档最多2段，再按上下文预算裁剪。知识工具结果带所有 source_version/hash/span，不只返回自然语言摘要。重排失败允许标记degraded并退回RRF，不改变证据ID。

本地小语料默认精确向量查询。HNSW是后续同一接口下的优化，须验证project过滤后Recall@k；不能为了速度过滤掉正确答案却不报告。分别测BM25-only/dense-only/RRF/rerank的Recall@10、MRR@10、nDCG@10和p95，不先宣布hybrid必然更好。

## 6. Evidence graph

不引入Neo4j。PostgreSQL `evidence_links` 表存 `supports/contradicts/derived_from/supersedes/tested_by` 边。边的端点统一为immutable artifact：source version的canonical text、memory claim snapshot或验证报告。边有来源和创建方法，LLM声称的supports不等于已验证支持。材料来源变更时，旧版本引用仍可查；失效/删除则输出明确撤回状态，而不是偷偷改引用文本。

报告中的每个重要外部事实绑定一个或多个Citation，核实source hash/offset与claim。LLM评审仅检查语义相关性，独立于最终发布判定。无法证实的结论写“不确定”，不要引用不相关来源凑数量。

## 7. 长短期记忆策略

工作记忆：当前目标、已完成节点、重要失败、工具结果索引；大内容存artifact，不把整个历史拼回prompt。项目记忆：稳定决策/术语/约束，需有来源。个人偏好：默认只在项目内生效，owner可显式设为global。

写入流程 `observation -> proposed -> dedupe/conflict check -> policy -> source_claim / inference / observed_test / user_confirmed`（根据真实来源分类，只有明确用户确认才是user_confirmed）。对于高影响偏好、事实冲突和跨项目共享请求需要确认；网页和仓库不能把“用户允许发送密钥”写入记忆。读取先scope过滤，再检索；高重要度/新鲜度帮助排序，不提升未经验证的可信等级。

删除必须涵盖chunk/vector/BM25快照、推荐特征、派生摘要缓存和可选trace数据。Temporal history尽量只存不敏感ID；已写入历史的敏感payload必须按加密/保留策略处理。删除状态与备份到期时间告知用户，不能声称已从不可改写备份即时物理清除。

## 8. 推荐闭环（完整V1，但不冒充协同过滤）

候选：项目知识库内容相似top100、已批准来源的新资料top50、当前goal相关top50。排除hidden/已删除/跨项目内容；document级去重。

初始score各特征归一到[0,1]：`0.35长期兴趣+0.15短期兴趣+0.20当前目标+0.10新鲜度+0.10来源可信等级+0.10未读新颖性`。缺失特征对剩余权重重新归一。公式和版本写入每次推荐事件，不称最优算法。用MMR λ=0.8选10项，单来源最多2项，所有非随机推荐可解释为实际特征贡献。

反馈：bookmark+3、helpful+2、click+0.2、有效停留+0.3、hide-3，仅作初始规则。停留只接受窗口在前台且有效曝光的范围；click不直接等于学习收益。事件带event_id去重，记录impression_id、候选集合hash、rank、policy_version、exposure_time。

探索默认关闭。开启后仅在最后一个slot执行ε=0.05的均匀随机探索；记录候选N和实际选择概率，不能把ε直接当propensity。最优item被选择的概率为(1-ε)+ε/N，其余为ε/N；候选改变需新hash。安全/隐私过滤发生在探索前。用户可关闭画像与探索，或清空兴趣。

主动跟踪使用Temporal schedules；只跟踪预批准来源和明确目标，默认生成站内通知/研究草稿，不自动向他人发消息。每次扫描有独立预算和增量水位，无变化则不调用总结模型。


## 9. 代码图谱与 Knowledge Fabric 的区分

通用网页/文档/RSS 仍采用本章 BM25S + pgvector + BGE；CodeGraph 仅处理代码仓库的 symbols/imports/calls/impact，不替代个人项目记忆、事实证据图或验收。代码图谱索引是版本化的派生缓存，受 project ACL/仓库快照限制。详见 `docs/13_codegraph.md`。

## 10. 冻结决定：原生记忆先做完整
当前生产MemoryEngine固定native_postgres，不等待Hindsight/Graphiti对比。已有事实/事件/技能/来源关联足以实现可追踪更新和遗忘。只定义一个薄MemoryBackend接口，不为每个候选实现空适配器。上下文编译和并发删除要求见docs/14_BOUNDARIES_AND_FAILURES.md。未来Hindsight只能读取授权副本，其派生记忆不能覆盖原始证据/权限。


---

# 06 · 权限、隔离与提示词注入防线

## 1. 威胁模型

把网页、RSS、PDF、代码仓库、MCP响应、第三方skill和LLM输出全部视为不可信。攻击可能诱导泄露API key、读取其他项目、修改验收/日志、扩大权限、反复花钱、运行恶意依赖或伪造完成报告。提示词过滤只能辅助，不是边界；隔离容器也不是无条件安全承诺。

评分器的父进程以 root 运行，从不导入候选代码。能归约成输入/输出协议的套件（每个断言都是候选探针与字面期望值的相等比较）由父进程持有断言和期望值。期望值不进入子进程、不写入 `protocol.json`。降权到 nobody 的子进程只计算探针并返回值，不跑 pytest。套件目录在子进程启动前收成 nobody 不可读；子进程若仍能打开套件、提前退出或返回值对不上，结果都是 FAIL。这种通过可以记为安全认证（`security_certified`），但评分结果的 `auto_promote` 恒为 false，不能据此自动发布。

任意 import 式白盒 pytest 仍与候选同一个解释器。它只验证合作代码：即便 JUnit 看起来通过，评分也不是 PASS，`trust` 为 `cooperative`，`security_certified` 为 false，不能自动发布。宿主只接受恰好一行 `ISOLATION` 和一行 `GRADE`。容器内的 `os.kill(-1, SIGKILL)` 只作为该容器 PID 1 清理本命名空间的残留进程，禁止在宿主机执行。

未覆盖、也不能称为已修复：容器逃逸、ptrace 父进程、候选猜中期望值、以及不能归约成字面协议的白盒断言。容器边界本身也不是对恶意代码的无条件安全承诺。

可信域：owner身份系统、policy/tool registry、gateway、runner-manager、verifier控制进程、release evaluator。应用开发者能修改这些代码，因此自我进化只允许改skill/受限workflow DSL，不允许Agent自动替换可信控制代码。

## 2. 网络和凭证分区

control网络：API/DB/Temporal/worker/gateway的受认证通信。runner-admin接口只允许trusted worker调用。每个编码job使用单独sandbox网络，默认无互联网，只能访问其job-scoped model gateway端点。沙箱不能访问control网段、其他job、云metadata、宿主机管理端口、Docker socket或DNS外传通道。HTTP工具由网关代表调用。

runner-manager是高权限基础设施。Docker socket仅它可持有，而且使用专用rootless daemon/专用runner VM；不能挂给API或Agent容器。不得声称只加`read_only`就防止越权。对恶意不受信任仓库的公开多租户部署，容器边界不够，V1明确不支持；应在后续使用更强隔离并重新威胁评审。

数据库key、provider key、GitHub token、审批签名key和verifier身份分别分发；任何一个actor不能获得全局credential。job-scoped token限制模型、project、task、预算、期限、调用次数，只能访问特定endpoint。不要把provider原始key透传给OpenHands。

## 3. 工具分类与能力授权

工具的分类、参数schema与影响由本地管理员维护的registry决定，**不信任MCP annotations**。类别：

- R0：授权项目内读取、搜索、无需外部变更的分析。仍要检查数据出口。
- R1：隔离工作空间文件改写/代码执行；无宿主机和外部写权限。
- R2：外部可撤销写操作，例如新建草稿PR；逐次绑定审批或明确限域预授权。
- R3：付款、正式发送、删除外部资源、部署、权限变更。V1默认disabled；只有管理员显式启用已实现且有对账策略的connector后逐次审批。没有实现的工具不能变成一个万能HTTP接口。

`CapabilityGrant`包含owner/project/task、tool、allowed_resources、args hash或限域规则、max_uses、expiry、data_class、egress destinations。Actor只能提出request，不能签发grant。流程为normalize -> resolve target -> check policy -> approval -> reserve -> commit intent -> dispatch。

## 4. 审批绑定与TOCTOU

Approval显示真实收件人/仓库/分支/域名、规范化参数、payload/patch hash、资源版本、费用上限、有效期和可逆性。批准后的参数、文件内容、base_commit、目标变化必须重新审批。不要使用“允许Agent干任何事”复选框。

审批token服务端签名、单用途、短TTL；数据库CAS消耗；重复点击不能重复动作。签名比较恒时；签名key不在sandbox。服务端确认owner身份，不接受请求体里自报的approver_id。审核事件单独记录。过期与撤回都阻止新的dispatch，但不假装撤销已经发生的外部操作。

## 5. SSRF与网页安全

下载只接受http/https、默认80/443，无URL userinfo、无file/gopher/unix等scheme。解析并规范化IDN/IPv4/IPv6；禁止loopback、private/link-local/metadata、reserved等非公开目标，判断以“是否全局可路由”为准，100.64.0.0/10共享地址段（含其中的云厂商metadata地址）、fec0::/10 site-local以及IPv4-mapped/NAT64内嵌的上述地址同样拒绝（本地fixture通过单独测试配置定点允许）。每次redirect重新检查，限制跳数、请求时长、解压后body大小和content type。

DNS检查必须与实际连接目标绑定并配合网络出口规则；“先查一次IP，再让库任意重解析”不能过安全验收。防DNS rebinding、IPv4-mapped IPv6、重定向到metadata、代理环境变量绕过、服务名称解析逃逸。HTTPX本身不提供这些授权语义；网络策略在真实容器内做探针验证。实现不了就禁用对应live网页功能，不降低测试。

JS浏览器与parser都不能在control网络。浏览器下载/弹窗/第三方请求受限，禁用复用用户cookie、自动扩展和未批准写请求。网页HTML在UI中作为清洗后的内容展示，禁止原样innerHTML；Markdown renderer禁止任意script/危险URL。提示词注入样本展示为引用数据而非执行指令。

## 6. 文件、代码和依赖

只挂载显式工作卷，non-root、drop capabilities、no-new-privileges、资源限额、默认只读rootfs、限制pids、独立tmp。真实可用字段以runner镜像smoke验证。tar/patch/file操作拒绝绝对路径、`..`、symlink/hardlink逃逸、设备文件和过大产物。

代码提交使用base snapshot与patch，不直接挂主机源码根、home或`.ssh`。安装依赖属于代码执行风险：默认镜像预装工具；临时新增包需批准的registry出口/锁定hash，不能让网页给出任意curl|bash后直接跑。Git hooks和仓库自带启动脚本不视为可信。

trusted test suite只读且独立；actor无法写测试定义、评测标签或verifier结果表。只读测试文件也不能解决被测代码运行时攻击，所以测试sandbox仍无密钥与网络。

## 7. MCP接入

只有管理员添加transport和server executable/URL，禁止模型动态安装MCP server。首次拉取schema保存hash并审核；server升级或工具schema变更进入quarantine，暂停旧授权。stdio使用固定argv而非shell拼接；argv检查会拒绝shell程序、env/sudo等启动包装器和解释器的内联代码参数（如`sh -ec`、`node -e`、`python -c`），但它只是配置错误的兜底，不是安全边界，边界仍是管理员审核的可执行文件与沙箱；远程服务固定TLS和允许域名。读超时、body限制、cancel和重连必须测试。

每个MCP tool映射到AETHER tool ID、风险/幂等分类与JSONschema。任何未分类工具默认deny。远程返回的instructions或“这是只读操作”不能修改分类。OAuth token仅gateway存储，按最小scope申请；未实现OAuth的provider只提供管理员配置token方式，UI如实标明。

## 8. 身份、审计与删除

V1单owner启动：仅loopback绑定，通过本机一次性enrollment建立登录；无默认公开密码。使用HttpOnly+SameSite cookie，state-changing请求检查CSRF/Origin；不在URL/localStorage存长期admin token。非loopback发布需要TLS和正式密钥配置，缺失则拒绝启动。数据库有 `one_owner_v1`，第二次创建所有者会失败。

`/v1/admin/*` 依赖这个单所有者不变量：任何已登录会话都属于那一个所有者，所以这些路由没有单独的角色检查。一旦支持多用户，必须先核对调用者是所有者，不能只凭登录态放行。

`packages/storage/aether_storage/db.py` 的 `Database` 使用 SQLAlchemy `NullPool`。每个事务单独建立连接，提交或回滚后关闭，不把连接放回池里复用。行级安全依赖 `set_config('aether.owner_id', ..., true)` 和 `set_config('aether.project_id', ..., true)`，第三个参数使设置只在当前事务有效。连接池在异常或会话状态没有清干净时，仍可能把上一个 owner 或 project 留在同一条物理连接上。不用池是为了避免这种串项目。代价是每次事务都要重新握手，吞吐低于常驻连接池。这是有意的行级安全与性能取舍，不是漏写 `pool_size`。

服务身份与用户会话分离。所有project请求服务端查访问权限，不能信header里的project_id。对所有list/detail/SSE/artifact/search/feedback/skill入口做越权测试。

审计默认存摘要、hash、工具参数的脱敏版本；避免原始秘密。hash chain可发现部分修改，但不对有DB管理员权限的人提供不可篡改保证。真正防篡改需外部只追加存储，V1不虚假宣传。审计保留期限可配置，敏感payload独立加密并支持删除。

## 9. 强制安全回归

至少包含prompt注入、跨项目检索、审批重放/参数漂移/过期、SSRF与DNSrebinding、路径逃逸、伪造工具分类、unknown effect、预算竞态、secret输出脱敏、恶意测试修改、公共skill自动加载、MCP schema漂移和sandbox网络连通探针。安全失败阻止发布和技能promotion；不能通过给提示词加一句“请安全”来代替修复。

## 10. 威胁情报浏览器与 Tor

浏览器抓取使用 Playwright `1.64.0` 和 Chromium 修订 `1248`（`156.0.8078.4`）。浏览器镜像按摘要固定为 `mcr.microsoft.com/playwright@sha256:06a9939e57531807f8d5fd76ce44b53165ffb7d7501d87ab10e285c20b1e971f`。派生镜像带上 `aether_worker`、`aether_domain` 和 `config/threat_browser.yaml`。这份配置里的浏览器、Tor 和 `.onion` 默认关闭。开关以部署的配置文件为准，请求里的开关不能超过它。打开 Tor 必须同时把 `owner_confirmed` 设为 true。

所有者审批来自账本里已有的 `approvals` 行。verifier 角色只核对，不写审批，也不持有 worker 能写的账本权限。worker 核对通过后写入 `operations`，并把 `consumed_by_operation_id` 填上，同一条审批不能第二次使用。调用方自己构造的许可对象会被拒绝。worker 进程不持有 verifier 的数据库地址，也不调用 Docker。Docker 只由 runner 进程执行。

明文访问只把浏览器容器接到出口网络，不创建 Tor 容器。Tor 打开时，浏览器容器只接入 `--internal` 网络，这条网络里只有 Tor 容器；Tor 容器再单独接入出口网络。SOCKS 只绑定这次访问在内部网络上分配的地址，策略只接受该 /28，并拒绝其他来源。出口网卡不监听 9050。夹具页也只绑在这个内部地址上。浏览器容器不挂 Docker socket，不发布端口，关闭 JavaScript 和下载。经 Tor 时只通过 `socks5://aether-tor:9050` 出去。进程环境里的 `HTTP_PROXY` 不传给浏览器，调用方也不能把代理改成别的地址。Tor 镜像由 `infra/tor.Dockerfile` 从 `debian@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f` 构建，安装 `deb.torproject.org` 上的 `0.4.9.14-1~d13.trixie+1`。构建时核对官方签名密钥指纹 `A3C4F0F979CAA22CDBA8F512EE8CBC9E886DDD89`，进程用户是 `debian-tor`。原先的 `osminogin/tor-simple` 不再使用。构建文件放在 `/var/lib/aether-tor-build`，安装结束后删除。

网页目标仍交给既有的 `assert_public_target`。每次文档请求连到这次校验返回的地址，有 IPv4 时用 IPv4。HTTP 的 Host 和 TLS 的 SNI 仍用原来的主机名，Chromium 不再自己解析或拨号。重定向每一跳重新授权。子资源和子 frame 的 document 请求直接中止，不发起抓取。附件和可执行内容在交给页面前拒绝，不写入下载目录。每次访问把脱敏记录写入 `audit_events`，按项目内的 sequence 接上一条哈希；调用方丢掉内存列表也不会丢掉这条记录。Tor 容器启动后，浏览器要等这个内部地址上的 9050 完成 SOCKS 握手。夹具模式因为 `DisableNetwork` 不接受 SOCKS，改为等同一地址的 8080 接受连接。`.onion` 不走明文 DNS，而且要另开 `.onion` 开关。经 Tor 或 `.onion` 得到的正文标成 `tor` 或 `onion`，不能作为 verifier 的可信证据。正文去掉双向控制符后放进不可信引用，页面文字不会变成工具调用。用途只有 `threat_intelligence`。交易、非法下载和联系卖家在代码里拒绝。进程内 Playwright 只在夹具开关 `AETHER_INGEST_ALLOW_FIXTURES=1` 打开时可用。

攻击回归使用本地恶意页面和本地假服务，不连接真实暗网。需要 Docker 的集成测试会构建并启动浏览器容器和 Tor 容器；Tor 在该测试里设置 `DisableNetwork 1`，因此不接受 SOCKS 连接，测试确认主进程是 `tor`。夹具页只在 Tor 容器内通过 TLS 提供。监听套接字保持明文，每个已接受的连接再握手，明文探测不会拆掉监听。证书在夹具容器启动时生成，私钥只出现在 `debian-tor` 的 0700 目录，不写入镜像层。浏览器把 `AETHER_FIXTURE_CA` 的 base64 解码成 PEM 文本再交给 `load_verify_locations`；该参数的字节形式是 DER，不能直接传入 PEM 字节。runner 只运行本进程环境 `AETHER_THREAT_BROWSER_IMAGE` 与 `AETHER_TOR_IMAGE` 里的镜像，并且只有 runner 自己打开 `AETHER_INGEST_ALLOW_FIXTURES` 时才接受夹具。`AETHER_REQUIRE_SERVICES=1` 且没有 Docker 时，这个测试失败，不跳过。进程内攻击回归仍直接调用同一个 Playwright 读页函数。


## 增补：CodeGraph MCP/CLI 不能获得主机级权限

CodeGraph 有机会读取整个仓库源码并把源码返回给模型，因此它是有真实数据泄露风险的**只读工具**。工作进程必须仅挂载获准 snapshot，不挂宿主 home、其他项目仓库、credentials 或 Docker socket；关闭遥测/默认网络；LLM 不能传递任意 projectPath。源码注释/README/工具响应全部视作不可信。图谱影响分析和相关测试列表不构成可信安全验收。完整规则见 `docs/13_codegraph.md`。


---

# 07 · Skill Evolution Lab 与可信评测

## 1. 学什么，不学什么

V1不更新基础模型权重，不自修改control plane。学习对象是有适用范围的操作技能、检索策略配置和可解释的工具workflow；不接受任意代码自动进入高权限路径。用户反馈帮助发现候选，但最终是否更好由独立任务结果决定。

可学习的例子：某类依赖升级先检查lockfile；针对某类检索任务保留原query再改写；某类失败需要运行一个已批准的诊断工具。不可学习的例子：绕过审批、放宽验收、删除失败日志、把测试答案抄进skill。

## 2. Skill bundle

```text
skill-name/
  SKILL.md              # standard YAML frontmatter + human-readable procedure
  aether.skill.json     # scope, version, required capabilities, hashes, eval metadata
  references/           # reviewed supporting material
  scripts/              # optional; disabled until explicitly reviewed and hash-approved
```

按标准name/description规范校验。manifest记录parent version、training trace IDs、compatibility、tool schema hashes、test family、allowed scopes、max_step/tool budget、risk class和bundle hash。manifest里的权限是**请求**，不是自动授权。外部skill导入一律quarantine。

## 3. 候选生成和去污

只读取已授权、已脱敏、验收有结论的trace。成功例和失败例均保留原因；agent自述成功不算成功轨迹。训练数据和测试数据按任务来源/模板family/时间切分，避免同一仓库补丁变体同时在训练和验证里。

学习器输出CandidateSkill+具体假设+预计适用域+反例。进行secret检查、许可证来源检查、重复/冲突检查、过宽权限检查、拒绝包含答案/用户私人原文片段的skill。每批最多3个候选、学习预算独立受限；没有足够证据则什么都不更新。

## 4. Promotion 状态机

`DRAFT -> QUARANTINED -> STATIC_PASSED -> EVALUATING -> ELIGIBLE -> CANARY -> ACTIVE`

失败状态 `REJECTED`；已部署可 `ROLLED_BACK`。STATIC_PASSED不意味着效果更好。ELIGIBLE需要独立evaluator产物；CANARY默认需要owner确认。在线任务运行固定skill版本，部署切换只影响新run；旧版始终可回滚。

## 5. 评测门槛（初始研发策略，不是已取得结果）

候选必须先过全部安全/契约回归，然后在至少30个独立task family代表任务上与父版本配对比较。每个任务同model/config/toolset、重置环境、相同预算。对family做配对bootstrap置信区间；多个seed是同一任务的重复测量，不能冒充更多独立样本。

初始自动eligibility：成功率提升>=5个百分点、paired95%CI下界>0、成本比<=1.10、无新的安全失败、mandatory任务无回归。样本不足/CI跨0则INSUFFICIENT_EVIDENCE，不用“多数票”硬升。阈值版本化，改阈值不得重用同一holdout挑结果。人可选择试验性canary，但UI显示未证实提升，不能标“已学会”。

对于零成本scripted测试，成本比没有商业意义，只验证管线。离线重放是测试策略或解析的一种方法，不能替代真实执行的skill有效性比较。

## 6. Canary 与回滚

只在预批准的低风险新任务上启用5%（可设0）的canary。风险类不匹配不分流。保存task到version的分配、时间、selector policy和结果。发现任一安全失败立即停用；连续功能退化或成本越界按预先写入的窗口规则暂停并回滚。历史ACTIVE指针与bundle hash可原子切回，已运行任务保持原版本或显式取消。

不要把不断窥视线上小样本当严格统计显著。门槛用于工程保护，发布优越性需要另行冻结实验。

## 7. Hermes 对照实验设计

四组：Hermes cold、Hermes learned/frozen、AETHER cold、AETHER learned/frozen。每组使用独立home/workspace、同一基础模型及provider revision、相同数据/工具能力和输出token/成本限制；记录任何无法匹配的差异。基线也有机会使用自身学习功能，不能只让AETHER见训练样本。

模型路由先关闭，防止用更贵更强模型冒充架构优势。然后可以另做cost-aware router实验，明确是另一个比较。训练阶段预算同时报告；技能推理token也算成本。衡量：objective task success、false completion、恢复正确性、unsafe attempts blocked、实际unsafe effects、p50/p95时延、总token/费用、每成功任务成本。

学习消融：关memory、关skills、关verification辅助、无rerank、无经验选择器。消融不能关闭基础安全权限；安全不是可牺牲的实验变量。

## 8. 数据集隔离

本包 `evals/dev_cases.jsonl` 是**公开开发验收场景**，Codex可以看，不能叫heldout。产品提供独立evaluator加载release dataset的接口，但真实最终集应由owner或独立流程在冻结release后提供/封存，存在代码/actor容器之外。训练/学习器不给它文件系统访问权。

合成隐藏标签若生成器/seed/答案已让开发Agent看到，不等于真正未知任务。发布报告记录dataset hash、family split、版本、评测次数、是否窥视；同一holdout用作选型后就不再是最终holdout。

没有最终集时只生成实验工具和开发验收报告，不捏造“提升20%”。上一轮提到的成功率/成本目标是愿望，不是对本项目的先验事实。

## 9. 统计与结果格式

报告包含每任务结果、实际验证证据、失败类型、费用known/unknown、模型请求数、wall time、approval wait time、timeout和abstention。abstention不算成功但不等于false completion。false completion分母写清：所有声称完成的任务或全部任务，两个口径都可给但不能混用。

至少同时给raw counts与CI。0次失败不证明失败率为0；小样本不能保证1%以下。比较优越性只对测试分布成立。失败任务和unknown账单不可以从均值中偷偷删掉。


## 10. CodeGraph 消融实验（新增）

在同模型、同代码快照、同预算与验收集下做 CodeGraph On/Off 消融，比较客观任务完成率、token、成本、时延、文件读取数量与遗漏关键调用路径。冷启动索引工作单独计入成本，warm 查询重复测；失败/timeout照计。项目官网的节省比例仅作为假设，不能直接复制进 AETHER 的结果。额外记录 residual context tokens：CodeGraph 官方自测提醒长会话残留上下文可能更多。见 `F15`。

## 11. 冻结决定：管线完成不要求实验结果必须为正
V1采用普通模型生成最多3个受限候选，再由独立pydantic-evals/可信验证器调度测试用例，结果交SkillLab判断。pydantic-evals不参与产品主任务调度。DSPy/GEPA和其他进化框架延期，初版不训练权重。
所有候选因样本不足或退化被拒绝，是正确运行结果；不得为演示“学会了”降低阈值。工程验收要求拒绝、eligible校验、人工canary和回滚机制真正工作；合成分数只测试状态机，并显著标注，不作为真实收益。30个family及现有阈值是初始研究协议，不是统计充分性的普遍保证；真实实验仍需按方差/最小效应做功效分析并限制反复试验。


---

# 08 · API、控制台与用户交互

## 1. 通用协议

API前缀`/v1`，JSON字段snake_case，时间UTC RFC3339；UI按Australia/Melbourne展示并标注时区。ID使用UUID，示例/reference中允许可读测试ID，生产层统一转换。分页使用opaque cursor和limit<=100，不用无界列表。

对创建task、发布skill和外部操作请求使用`Idempotency-Key`，作用域是owner+project+route+key，保存规范化payload hash；同key不同body返回409。更新资源使用If-Match版本控制；审批重新绑定具体artifact/intent，不依赖UI缓存。

错误统一`{code,message,request_id,retryable,details}`。401未登录、403越权、404不可见或不存在、409版本/幂等冲突、422不合法合同、429限流/预算、503依赖不可用。不把异常包装成200+success:false。健康检查`/health/live`与`/health/ready`分开。

## 2. 资源与路由

| 路由 | 方法 | 契约/行为 |
|---|---|---|
| /v1/projects | GET/POST | owner项目列表/创建；不暴露其他项目 |
| /v1/projects/{project_id}/tasks | GET/POST | 分页任务；POST输入原始目标+预算/资料范围，202返回task_id |
| /v1/projects/{project_id}/tasks/{task_id} | GET | 状态投影、合同版本、budget known/held/unknown、产物 |
| /v1/projects/{project_id}/tasks/{task_id}/contract | GET/PUT | 草案合同；已批准合同变更新建version，旧版只读 |
| /v1/projects/{project_id}/tasks/{task_id}/commands | POST | approve_goal/pause/resume/cancel；command_id去重，202异步接收 |
| /v1/projects/{project_id}/tasks/{task_id}/events | GET SSE | event_id游标重放，不丢断线期间事件 |
| /v1/projects/{project_id}/approvals | GET | 待处理审批列表 |
| /v1/projects/{project_id}/approvals/{approval_id}/decision | POST | approve/reject；body含expected_intent_hash，server查owner |
| /v1/projects/{project_id}/artifacts | POST multipart | owner上传受限文件，返回ArtifactRef，默认25MiB |
| /v1/projects/{project_id}/artifacts/{artifact_id} | GET/DELETE | 鉴权后读取/删除；download不接受任意path |
| /v1/projects/{project_id}/sources | GET/POST | source config，域名/权限/抓取间隔明确 |
| /v1/projects/{project_id}/sources/{source_id} | GET/PATCH/DELETE | 版本控制；删除立即tombstone |
| /v1/projects/{project_id}/ingestions | POST/GET | ingest command与状态，内容未索引完明确显示 |
| /v1/projects/{project_id}/search | POST | query、filters、mode；返回hits/来源/索引版本/degraded |
| /v1/projects/{project_id}/memories | GET/POST | 搜索或用户明确添加记忆；proposed不能自动confirmed |
| /v1/projects/{project_id}/memories/{memory_id} | PATCH/DELETE | 用户纠正/删除；生成新revision或tombstone |
| /v1/projects/{project_id}/recommendations | GET | 只读推荐列表。不写画像、不写曝光；impression_id 为空，直到显式的 rank 写入 |
| /v1/projects/{project_id}/feedback | POST | idempotent event；user不在body中自报 |
| /v1/projects/{project_id}/skills | GET/POST | 列表/导入quarantine；脚本不自动运行 |
| /v1/projects/{project_id}/skills/{skill_id}/evaluate | POST | 独立evaluator job；actor不能指定最终分数 |
| /v1/projects/{project_id}/skills/{skill_id}/deployments | POST | promote/rollback，要求eligible eval hash+审批 |
| /v1/projects/{project_id}/watches | GET/POST | 授权来源跟踪、schedule、预算 |
| /v1/projects/{project_id}/watches/{watch_id} | PATCH/DELETE | 暂停/更新/删除schedule |
| /v1/projects/{project_id}/evaluations | GET/POST | 明确dev/validation/release类别与dataset hash |
| /v1/projects/{project_id}/costs | GET | 时间范围与任务账本；未知成本单独字段 |
| /v1/admin/connectors | GET/POST | owner-only registry和连接器配置；密钥仅写不可回显 |
| /v1/admin/connectors/{connector_id}/probe | POST | 非副作用能力测试，不能自动扩大scope |
| /v1/admin/models/probe | POST | model/tool/structured output能力与价格配置检查 |
| /v1/projects/{project_id}/coding-jobs | POST | 受理 202。worker 在断网、只读、限资源的沙箱里跑到 FINISHED 或 FAILED。容器内由 OpenHands 1.54.0 的 Agent 用 file_editor 和 terminal 生成补丁，模型调用经本机代理进入网关并按 token 结算；测试协议仍由 LocalWorkspace.execute_command 执行。供应商 key 不进容器。失败后同一作业键可以重试。任务取消只有在容器确认消失后才写成 CANCELLED；仍持有租约的作业不会被 TTL 回收。终态写入任务事件 coding_job.terminal，沿用现有事件流查看。受理不是通过 |

`/v1/admin/*` 依赖 V1 只有一个所有者。已登录会话就是该所有者，这些路由因此没有第二层角色检查。支持多用户时必须先核对所有者角色，不能只凭登录态放行。第二次创建所有者会被拒绝。

`contracts/openapi.yaml`提供上述资源与认证路由的设计合同；实现时由真实FastAPI生成OpenAPI并做契约差异测试。不得只实现少数示例就宣布API完成。`apps/web`通过生成的typed client调用真实API。

## 3. SSE

EventEnvelope包含event_id、task_id、project_id、sequence、type、time、payload、schema_version。持久event序号在每task单调增加，不用时间戳替代唯一ID。客户端`Last-Event-ID`重连，从数据库读遗漏事件后再进入live tail；心跳与数据事件区分。

events必须脱敏。不同task的event ID不能拿来绕过项目权限。历史已清理时返回明确cursor_expired和重新拉取snapshot方法，不静默从现在开始。UI不能因最后一个LLM token写“完成”就把task标成success。

## 4. 控制台页面

**Overview**：项目切换、活跃任务、待审批、最近失败、今日成本（unknown单列）。没有数据时显示空态与新建任务，不用假曲线填满页面。

**Task workspace**：左栏DAG/步骤列表，中间目标合同与运行事件，右栏预算/权限/技能版本；底部Artifacts与Verification。暂停/取消有请求中状态；显示恢复次数和为什么blocked。有限长度执行理由即可，不要求记录模型隐藏chain of thought。

**Approval inbox**：目标、规范化参数/patch、影响、费用、expiry、资源版本、来源与变更diff。高影响动作不能默认checked；不提供误导性的“永久允许所有”。

**Knowledge**：来源状态、索引延迟、版本、删除；Search结果可展开命中段落与source。Memory页面显示谁说的/何时/冲突/有效范围，支持纠正与forget。

**Discover**：推荐卡片、真实推荐理由、bookmark/helpful/hide；探索状态可见、可关闭，禁止用伪随机结果冒充学习模型。

**Skills lab**：候选和parent diff、训练来源数量、验证集类别、raw结果/CI/费用、部署与回滚。没有足够样本明确“证据不足”，不显示虚假的能力增长百分比。

**Evaluations & settings**：四组baseline实验配置与报告；模型alias、key状态、预算、connector权限、隐私/保留设置。

## 5. UI验收

支持中文长文本、键盘操作、明确label、焦点管理、可读对比度。至少测试窄屏和桌面；错误、loading、offline、empty、stale projection、approval expired都要可视化。禁止暴露原始HTML/密钥。Playwright测试使用真实API与fixture provider；mock-only组件测试不能代替端到端。


---

# 09 · 数据模型、事务与保留

`contracts/schema.sql`是数据库初始结构合同，不是声称已经在PostgreSQL运行过的迁移。Codex将它转为Alembic并执行clean upgrade / rollback / upgrade / data-preserving upgrade测试。

另有auth_sessions、node_dependencies、ingestion_jobs、coding_jobs和connector_configs辅助表；身份表为全局，业务实体均有project_id。evidence_links以不可变artifact为边的端点，verification结果可附在tested_by边上。

## 实体组

| 组 | 表 | 关键约束 |
|---|---|---|
| 身份/项目 | owners, projects | V1单owner；project域是所有业务的隔离边界 |
| 目标与运行 | tasks, goal_contracts, task_nodes, command_outbox, task_events | contract版本不可覆盖；event/command去重；status为投影 |
| 权限与操作 | capability_grants, approvals, operations | 同intent唯一；approval绑定hash；未知结果保留 |
| 证据 | artifacts, verification_results, evidence_links | immutable hash；验证关联contract/产物/验证器 |
| 成本 | budget_accounts, model_requests | 整数microUSD；reservation与actual分开；unknown不是0 |
| 知识 | sources, source_versions, chunks, index_generations, memories | 来源版本、offset、scope、向量revision一致 |
| 学习 | skills, skill_versions, skill_deployments, evaluation_runs | 同scope活动版本原子切换；候选无自升权限 |
| 个性化 | feedback_events, profile_snapshots, recommendation_impressions | 幂等反馈；policy/propensity版本；能删除与重算 |
| 自动化 | watches, notifications | schedule ID稳定、站内通知默认无外部副作用 |
| 审计 | audit_events | 只追加应用权限；不声称可抵抗数据库管理员 |

## Scope与数据库权限

所有业务表含project_id，引用采用(project_id,id)复合FK，避免把其他项目的对象挂进来。应用层鉴权+数据库RLS双层；DB会话每transaction设置授权后的project上下文，连接池复用不得遗留scope。只给服务所需权限；API账号不能写verification结果、skill评测成绩和最终task成功状态；actor无数据库账号。

`schema.sql`中的RLS是project predicate基准，实际迁移必须加入角色权限与项目列表访问策略。不能因为表owner通常绕过RLS而在集成测试中只用superuser；必须用真实runtime角色验证。项目创建等全局操作走专用已认证路径。

## 四个关键事务

1. **提交**：task+contract draft+command_outbox原子提交；Temporal dispatcher用固定workflow ID，失败可重投。
2. **审批与动作**：锁定approval和operation，核对intent hash/version/expiry，消耗一次性授权、写PREPARED/DISPATCHED；远程网络调用不在长数据库transaction内。
3. **预算**：锁budget account，验证spent+held+new<=limit与verification headroom，插入唯一request reservation；结算actual时保留超支事实，不能拒绝记账以伪装没超额。
4. **技能切换**：校验独立eval evidence与expected previous version，更新active deployment，append audit；写入版本不修改已运行任务。

## 大对象

数据库保存metadata/ref，内容在ArtifactStore。路径只能服务端生成`project_id/sha256-prefix/sha256`，不得接受用户相对路径作为最终存储路径。先临时写入+fsync+hash验证，再rename；DB记录引用，后台GC回收无引用对象。下载必须经过鉴权，public URL不默认开放。支持将来S3兼容adapter，但V1默认本地volume不依赖第三方对象存储。

## 数据一致性

index、profile、UI projection都是可重建派生数据；每次查询记录水位/版本。缓存key至少含project_id+权限版本+source/embedding/tokenizer版本+query。删除和撤权增加visibility epoch，旧缓存立即失效。RRF与reranker不能绕过tombstone检查。

## 保留与备份

默认原始模型prompt不落日志；可配置脱敏trace保留30天、task metadata90天、artifact30天、知识资料直到owner删除。所有默认值在配置可见，不是法律要求。backup单独加密，记录保留与删除到期；测试pg dump/restore及artifact hash核对。恢复后先停外部dispatch，对UNKNOWN和未完成操作对账，不能一恢复就重放全部写操作。
备份覆盖全部业务表（schema出现未登记的表即拒绝备份/恢复），在一个可重复读快照里读取；可以在空库重建，也可以合并进在用库。合并不删除任何行或文件：缺的行补回；内容类行（项目、产物元数据、资料、记忆等）回到备份值，但备份后已删除的不复活，行内的生命周期与安全决定（记忆的 superseded 等状态、索引代的 ACTIVE/RETIRED、产物与任务产物的分类）保留当前值；账本与生命周期类行（任务、命令、审批、操作、预算、合同、验收、会话、部署等）在备份后变化的保留当前值，并在结果里列出。产物先复制一份到同级暂存目录，在副本里写回备份字节，只存在于当前目录的文件和 journal/locks 记录保留；数据库事务提交前才整体改名换入，暂存或数据库任一步失败，两边都保持原样。暂存期间需要与产物目录同等的额外磁盘空间，恢复前要停掉写产物的 worker。


---

# 10 · 运行栈、模型、成本与发布

## 1. 运行模式

**reference-only**：本包的schema/纯函数自检，不能证明产品能运行。

**offline demo**：完整API、PostgreSQL、Temporal、真实本地文件/HTML工具、独立verifier、真实UI；只有模型换成显式ScriptedProvider。本地fixture站点在专用测试网络定点允许。ML权重不存在时可以测试lexical-only，完整hybrid gate仍标blocked，不能用随机向量冒充真实embedding。

**live local**：owner配置模型key和price table后启用真实推理；编码使用隔离runner，研究来源通过白名单。默认不开公网、不自动收费、不发送真实外部消息。

Live 探针只认这三个环境变量，缺任何一个就记 `blocked`，不用脚本模型代替：

- `AETHER_LIVE_MODEL_KEY`：供应商 API key。
- `AETHER_LIVE_MODEL_BASE`：OpenAI 兼容 API 根地址，例如 `https://api.example/v1`。
- `AETHER_LIVE_MODEL_ALIAS`：请求里的模型名。

**release**：镜像与依赖均固定、启用认证/TLS、runtime用户非superuser、完成备份恢复/网络隔离/真实模型测试。个人部署，不声明公开多租户安全。

## 2. 基础技术版本策略

control plane Python3.13；Node24用于前端；PostgreSQL17 major+pgvector；Temporal选择与Python SDK兼容的稳定release。OpenHands四个包保持一个已验证release/revision集合。ML可独立Python环境，如3.13无所需wheel则固定3.12并记录，而不是降全项目或随意升级所有包。

F00输出`uv.lock`（各独立workspace如有）、`pnpm-lock.yaml`、`infra/images.lock.json`、`config/upstream.lock.json`、`config/models.lock.json`。此处没有猜测具体未验证的package patch版本或image digest。确认license/security后才拉取/构建镜像；仓库main/head只用作来源观察，不当生产浮动依赖。

## 3. Compose目标

必要服务：postgres、temporal-server、api、worker、gateway、web；编码功能再启动runner-manager与按job创建的沙箱；ML worker通过profile启用真实检索。Temporal UI为开发工具，可选且不对公网开放。基本event/cost可视化直接来自AETHER API，不依赖Langfuse。

开发进程里的 worker 是 `uv run --locked --no-build python -m aether_worker`。它使用已有 Temporal 和 `command_outbox`，不另起调度器。环境变量、命令种类和停启方式见 README「安装与运行」。

操作者仍然导出 `AETHER_VERIFIER_DATABASE_URL` 和 `AETHER_LIVE_MODEL_KEY`。入口把验收库地址交给验收进程，把供应商 key 交给网关进程，把 Docker 探测交给 runner 进程，然后从本进程环境删除这两项并重新执行。重新执行后的 worker 看不到验收库口令、供应商 key，也不打开 Docker 套接字。验收进程只使用 `aether_verifier`。网关进程持有供应商 key，并用 worker 库角色核对任务令牌。runner 进程探测 daemon：`docker info` 返回非空 ServerVersion 才是 enabled，否则作业记录 `runtime=unsupported`。worker 角色不调用 docker。编码沙箱的启动、停止和日志由 runner 按固定动作经 Docker SDK 执行。运行镜像在 `python@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f` 上安装 OpenHands 1.54.0；`JOB_IMAGE` 常量仍是 ADR-F40 的 Alpine digest。编码容器仍是 `--network none`。它只能通过一个挂载进来的 Unix socket 访问本机模型代理，再由网关带上供应商 key 去访问模型。该套接字不对 other 开放，只经 POSIX ACL 允许 uid 65534 连接。编码对话的每次调用都消耗一张一次性子票。单轮预留默认 4096 token，不够时按次追加，但不超过任务预算里留给非验收调用的上限；超上限或重放在上游之前拒绝。实测量超过预留时按实际 token 结清并释放冻结；`/v1/chat/completions` 的超时由服务端固定，请求体里的 timeout 不生效。协议判定使用作业创建时的测试文件。内存与 swap 上限是 805306368 字节。安装命令是 `uv run --locked --no-build python scripts/build_openhands_sandbox.py`。runner 在 Docker 可用且标签 `aether.openhands_sandbox=1.54.0` 还不存在时执行同一次构建。技能评分在验收进程执行，版本状态仍由 worker 写回。沙箱和评分基座镜像用摘要固定，评分依赖 `pytest==8.3.5` 以哈希校验安装。威胁情报浏览器使用 Playwright `1.64.0` 与 Chromium 修订 `1248`，镜像摘要见 `infra/images.lock.json`。`config/threat_browser.yaml` 默认关闭浏览器、Tor 和 `.onion`。打开 Tor 还要所有者把 `owner_confirmed` 写成 true，并且每次访问单独审批。Tor 容器由 `infra/tor.Dockerfile` 构建：基座是 `debian@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f`，包版本 `0.4.9.14-1~d13.trixie+1` 来自 `deb.torproject.org`，签名指纹 `A3C4F0F979CAA22CDBA8F512EE8CBC9E886DDD89`，运行用户 `debian-tor`。没有 Docker 时容器路径失败关闭，集成测试在 `AETHER_REQUIRE_SERVICES=1` 下也不会跳过。不会改在本机直接抓取。子 frame 的文档请求直接中止。访问记录写入 `audit_events`。Tor 打开后要等内部链路地址上的 SOCKS 端口完成握手，夹具模式则等同一地址的 8080 接受连接。出口网卡不提供这个代理。启动时对已见项目调用产物 `recover`。`get` 会读满账本声明的长度，不把一次短读当成哈希不一致。

PostgreSQL为AETHER与Temporal使用独立数据库/用户，持久volume。Temporal开发模式可以使用独立测试服务，但release必须使用持久服务配置，不能把start-dev的临时状态冒充生产恢复能力。Compose健康检查等待真实ready；迁移作为显式job；命令不能默认删除volume。

全栈镜像digest/arm64支持由实际拉取验证。控制/研究链路的开发目标支持macOS arm64，实际支持以探针为准；完整Linux sandbox安全与故障验收在Linux amd64环境执行。Windows通过WSL2/远端Linux控制，不假设原生Docker安全行为完全相同。

## 4. 模型路由

逻辑alias固定：`planner`、`actor`、`coder`、`judge`、`extractor`。初期planner/actor/coder可共享同一个用户提供的强工具调用模型；judge尽量不看actor总结，评审以确定性测试优先。更换模型厂商不是架构创新。

生成模型不写死商业型号或假设免费额度；`config/models.example.yaml`要求管理员提供真实provider/model_id/api_key_env/base_url（如需）和价格。缺值时live启动失败，offline仍可用。不得提取Codex/Cursor登录凭据给产品当API key。

每个alias能力探针：基本生成、JSON schema校验、tool call、usage字段、超时/取消、并发上限、长上下文边界、错误归一。用户配置模型不能自动获得更高权限。未知模型价格和usage标unknown并停止进一步自动花费，不能按0计。

F00不需要购买任何新模型。优化router在完成同模型baseline后启用：先rule-based按任务复杂度路由，记录route reason；能耗/延迟/费用以实测选型，不因为“小模型”就假定更省。

## 5. 预算账本

货币单位整数microUSD，1美元=1,000,000microUSD；若展示AUD需要显式配置汇率时间和来源，不随意用固定汇率。本包不提供实时价格。

默认单task上限2,000,000microUSD、100,000tokens、48model requests、active执行1800秒；20%费用和token预算保留给verification/收尾。用户批准合同后才可提高。学习批次单独budget，不偷偷从任务剩余额度无限学习。

每请求先按输入token上界+max_output与已批准价格版本reserve；结果按actual usage结算，cache tokens/reasoning tokens按该provider计费规则记录。estimate与actual分列。超时且可能已计费时保留reservation为unknown，人工/provider对账后结算；强行释放会导致重复花费。

预算是尽力约束，若provider返回超过估算的实际费用必须真实记账、阻断后续请求、告警。不能承诺分布式外部账单永远绝对不超出估计，但本地并发reservation决策必须原子且无竞争漏洞。

## 6. 观察与日志

每任务trace_id；每模型请求、工具operation、runner job、verification都有关联ID。基本日志JSON格式、默认脱敏；记录错误类别、耗时、token、成本known/unknown。不要存隐藏chain-of-thought；保存简明决策理由、输入/输出hash与工具证据即可。

指标：queue depth、active jobs、workflow retry、unknown operations、approval age、budget held/actual、parser errors、index lag、retrieval latency、verification failure、skill rollback。为每个故障提供UI和诊断命令，不能只有日志里一个traceback。

## 7. 禁止默认危险的运维行为

不使用`docker compose down -v`作为普通重启/升级；不自动清空DB；不把任何admin端口映射0.0.0.0；不在前端bundle注入provider key。备份恢复、数据导出、真实PR发布需要显式命令和授权。安装脚本不改机器范围防火墙/安全设置；需要这些权限的部署步骤先生成配置供owner执行/批准，并把相关验收标清。

**备份恢复与 Temporal**：备份只含 AETHER 数据库与产物目录，不含 Temporal 的工作流历史。恢复点之后 Temporal 里发生的事不会随数据库回退：备份后已经完成的任务在恢复时保留当前状态（结果里列在 `kept_live_tasks`），不会被改回排队；恢复把所有 PENDING/DELIVERING 命令改成 STOPPED，不自动重新派发，由所有者核对后重新提交。在用库上恢复前先停 worker 与派发进程。重建到空库时，Temporal 若也是新的，旧工作流不存在，未完成的任务需要重新派发；Temporal 若仍是原来的，工作流仍按原历史运行。

可重复的崩溃演练是 `scripts/run_crash_recovery_drill.py`。它使用名称以 `aether-drill-` 开头的 Temporal 容器和该容器自己的 sqlite，不停止共享开发服务。顺序是备份 Postgres 与 sqlite、只删除演练标记指向的 sqlite、按 `restore-aether` 合并恢复、再读取任务状态和 Temporal 工作流。两边不一致时报告 `relation=diverged`、`action=human_review`，不改写终态。恢复仍是合并，不是把 Temporal 历史回退到备份点。演练写入的备份、sqlite 和报告必须位于当前工作目录内。工作流编号只接受字母、数字、点、下划线和连字符。Postgres 与 sqlite 在容器停止时一起复制。确认值不是 `restore-aether` 或备份哈希不符时，不停止容器，也不删除 sqlite。容器以宿主机 uid 运行，并设置 `USER=temporal` 与 `HOME=/state`，这样镜像 passwd 里没有这个 uid 时仍能创建 sqlite。


---

# 11 · 唯一施工顺序和完成定义

当前只有F00–F27这套队列；其中F15为选定增强、F26为live验收，其他是核心工程任务。不是28个独立大平台，也不是新的工期承诺。

| 阶段 | 任务 | 可见交付 |
|---|---|---|
| G0 | F00–F01 | 实际工程/核心依赖可启动；SDK与ML独立探针 |
| G1 | F02–F06 | 存储/身份/产物/网关/预算/审批；真实副作用前先完成 |
| G2 | F07–F09 | 真实Temporal任务→工具→独立基础验收→最小UI，故障恢复 |
| G3 | F10–F12 | 授权采集、真实混合检索、上下文与原生记忆 |
| G4 | F13–F15 | 受限OpenHands编码、独立测试；CodeGraph单独增强 |
| G5 | F16–F21 | 遗忘/MCP/跟踪/推荐/技能候选与可拒绝的学习闭环 |
| G6 | F22–F27 | 完整UI/安全/对照工具/备份发布/live矩阵 |

依赖是DAG，不是一条把所有工作锁死的链。F01阻塞时继续F02–F09；没有CodeGraph不阻塞其他核心；没有付费key仍可完成不依赖live的工程和测试。当前阶段过关后继续下一个未阻塞任务，不自动停止于demo。F15在F14完成后安排实施；selected_optional表示不阻塞核心发布，并非默许永远跳过。真实受阻时明确记录原因、未启用状态和恢复条件。

## 目标命令（由Codex实现）
`make doctor bootstrap dev demo-offline test-unit test-integration test-security test-e2e demo-live acceptance-report backup restore`。
本包目前只有check-pack/test-reference，不能把未实现目标命令当作已存在脚本。所有命令失败非零退出，不能吞异常。restore为显式高影响本地操作，默认不覆盖现有数据。

## 交付边界
内核工程：F00、F02–F09真实通过；产品工程：所有scope=core验收通过；live_ready：产品工程+F26通过；CodeGraph_enabled：F15通过及owner启用；comparative_evidence：独立真实实验有记录，不由任务done自动推断。
产品工程可包含明确scripted模型的E2E，但数据库/Temporal/本地工具/SDK/ML等相应集成不能全mock后标通过。没有真实权重/runner的平台用例必须blocked；小模型/CPU可用性按实测而非硬件印象。
工程版交付包允许live_not_run，但必须明确不是已验收的live V1。未完成F15可以交付核心产品，同时标CodeGraph未启用，不能勾已接入。

## 三段产品演示
研究：导入真实中英资料，执行混合检索并生成带来源版本和引用位置的比较报告，缺证据处明确标注。
编码：授权仓库快照→OpenHands产生patch→独立测试workspace验收；中断后不能重复创建job或使用旧验收。
升级注意（2026-10-17，ADR-F42 修订）：编码作业的 objective 从 operation 参数读出并原样交给沙箱 Agent。升级前已经排队、但 operation 参数里没有 `objective` 的旧作业，升级后会以 `objective_missing` 失败，不会退回旧的 demo 提示；需要用同一作业键带上 objective 重新提交。没有 Agent 改动（补丁为空）的作业判 `agent_no_change` 失败，候选文件本来就通过也不算成功。
学习：可追踪训练trace→候选→独立评测→拒绝/证据不足/eligible→人工canary/回滚。合成分数只证明机制；没有正收益候选是允许的真实结论。
跟踪/推荐作为产品功能验收：无变化不调用总结模型；所有推荐有实际特征理由且能清除画像。

## 测试证据与发布
每条结果包含check_id、task_id、类别、命令、cwd、git commit、工具/镜像/模型/dataset版本、退出码、时间、日志、产物hash、status和原因。`not_run/blocked/skipped/failed`均不等于passed。
所有最终成功状态必须来自当前不可变候选的可信验证。发布report应从实际JUnit/JSON记录生成，并列出未测平台、unknown账单、缺失live检查和可选功能状态。
安全/角色/SSRF/网络/预算/幂等/恢复/回滚/删除竞态必须有实际集成证据，不能以纯函数参考测试代替。
测试场景公开给Codex是开发集，不叫holdout。Hermes与AETHER使用相同模型/权限/预算，双方可训练并冻结；无法相同处记录。提供driver是工程成果，跑出领先是研究成果，两者严格分开。


---

# 12 · 集成细节、认证与初始参数

## 服务间协议

所有内部调用仍做服务身份认证，不因为处于Compose网络就信任请求。V1使用安装时随机生成、按服务分开的凭据与明确scope；不写进仓库或传给actor。非本机跨主机连接要求TLS。生产轮换和撤销记录在audit。

**runner → model gateway**：为OpenHands提供经过能力探针确认的OpenAI-compatible chat-completions端点；若所选模型不支持该协议则由LiteLLM适配。job token仅能访问推理端点，服务器从token查project/task/job，不接受body改变scope。请求中model只能是授权alias，max_tokens受服务端限额；每次流式请求同样先reserve，流关闭后处理usage/unknown；不能用流提前终止绕过账单。

**worker → runner**：使用contracts/interfaces.py定义的start/get/cancel。start以(project,task,intent)唯一，返回job_id；get返回真实状态/最后心跳/产物引用；cancel返回accepted而非“外部动作已停止”。API/actor不直接得到容器地址。runner把upstream事件转换为脱敏AETHER event，不接受上游直接修改应用状态。

**worker → verifier**：发送immutable input manifest、contract hash、registry中trusted suite ID/hash，独立worker检查；只允许verifier凭据创建最终evidence，API/runner/actor均不能仿造。验收schema来自本包contracts/acceptance.schema.json，不是自由的shell字符串。

**worker → knowledge/ML**：文本先在受限parser转canonical text再进入embedding/rerank。首版内网HTTP或同进程协议均可，但返回embedding revision、维数、输入hash；一次batch内部不能混模型revision。状态不可用明确degraded/blocked，不能返回随机向量。

## 单owner认证流程

安装命令生成随机一次性bootstrap secret，终端只显示一次，禁止日志。`POST /v1/auth/bootstrap`只允许loopback、无既有owner、有效secret，创建owner并设置持久登录口令（服务端Argon2id哈希）；立刻作废bootstrap secret。公网不能开放bootstrap。

`POST /v1/auth/session`用口令登录，限流，返回HttpOnly/SameSite会话cookie；cookie为随机不透明值，数据库仅保存其哈希。`GET /v1/auth/session`返回身份/CSRF token；`DELETE`注销并撤销服务端session。状态变更要求CSRF token+Origin检查；跨站CORS默认关闭。release cookie Secure，localhost开发例外显式配置。

服务端从session取owner，不相信body中的owner字段。session表有expiry/last_seen/revoked，默认12小时绝对有效期、30分钟idle，可配置。恢复口令只能走本机显式CLI，撤销所有session。此方案不等于企业SSO。

## 具体时间/重试边界（初始值）

| 操作 | 默认值 | 处理原则 |
|---|---|---|
| HTTP静态抓取 | connect5s、read20s、total30s，最多3次重定向、HTTP硬上限20MiB（HTML/RSS默认10MiB） | redirect/每次真实连接重做目标策略；非幂等写不套此重试 |
| 读Activity | 单次30s、总90s、最多3次、backoff1/2/4s | 只有允许幂等重试的读操作 |
| LLM请求 | total120s，stream idle30s | 超时可能计费；unknown保留reservation，不能无条件重发 |
| 模型结构修复 | 最多2次 | 占同一task预算和总请求上限 |
| 代码job | 默认15分钟且不超过task剩余active budget | 以job状态/实际开始时间计，poll5s；取消grace10s后隔离进程树终止 |
| 文档parser | 每文件120s、默认内存1GiB | kill并报告parser_timeout；大文档可显式扩额，不默默跳页 |
| 审批 | 72小时 | expired后PAUSED，不继续执行旧授权；新的审批需新grant |
| SSE | 15秒heartbeat | DB为游标恢复来源，不能只依赖内存广播 |
| BM25 generation | 30秒debounce，单builder租约 | 查询可使用明确标记的旧generation；删除立即过滤 |

资源值是设计起点，F00在目标机器测量后写platform report，可按证据调整；不是性能承诺。OpenHands官方SDK是否有同名参数须实测，本表由外围服务执行，不据此虚构上游参数。

## 工作流拓扑细化

`TaskWorkflow`为用户task的唯一parent，保存contract版本、node cursor、pending command ID和剩余预算摘要。Research/Code/Extraction节点可用受限child workflow；Verification用独立task queue。`WatchWorkflow`只比较源变化并在预算内提交普通research task；`SkillEvaluationWorkflow`只运行受控评测，不是递归创建无界agent。

workflow history软上限5000 events时在安全边界continue-as-new；有未决update handler/child结果时先完成或明确转交，保持task的logical ID。外部UI event序列不随run ID重置。Activity读取DB合同后返回版本和hash，workflow固定使用该版本，不能每次重放重新读“最新”。

暂停/取消命令可随时接收，但应用在安全边界，预算不得因重启/continue-as-new重置。active elapsed应持久计算，排队/审批等待不扣active budget，已启动的远程job即使worker离线也继续计时。48次模型请求上限同时覆盖OpenHands内部调用和judge；验证预留按共享账本原子检查。

## 参考代码边界

Pydantic frozen仅阻止字段重新赋值，不深冻结list/dict。真正不可变合同需：规范化序列化、hash、只追加DB版本、写权限和每次使用校验，不能靠frozen=True实现安全边界。reference BudgetLedger是单进程算法参考；生产必须用事务。validate_relative_export_path仅做词法校验，不解决symlink、hardlink、mount或TOCTOU。SHA只能证明字节一致，不能证明来源可信。

参考Identifier允许可读fixture值，生产所有实体ID统一UUID；example中sha256用于合成内容身份，不是声称真实线上来源。schema文档的ID限制不能代替鉴权。

## 本地文件与仓库输入

UI使用`POST /v1/projects/{project_id}/artifacts`上传本地文件，默认25MiB，先写受限临时目录后校验hash和类型；Source.kind=local_document时locator只能是本项目artifact ID，不能是服务器路径。编码仓库由owner显式导出/上传快照或受限Git connector按批准revision取得；压缩包导入必须检查zip-slip/symlink/hardlink/文件数量/解压总大小，且在隔离worker中处理，不通用展开用户任意附件。

技能评测/部署等非聊天控制操作使用有类型的control task关联审批和审计；不能伪造任意既有task作为approval scope。若无需外部动作，control task仅使用其审批能力，不获取执行器权限。


## CodeGraph 运行时协议（新增）

受信 RepoIntelligence adapter 在 isolated runner 的专用工作进程中执行固定 CLI/MCP；可按 immutable snapshot manifest 缓存 index。只暴露 `repo.code.explore`, `repo.code.impact`, `repo.code.affected_tests` 经受信 gateway 过滤的结果；前后端/actor 不能调用全局 MCP server 或选择路径。版本与 status/freshness 写入 artifact metadata；取消任务时停止 watcher/daemon、关闭本次 graph 句柄。默认禁用，降级需显式记录。详见 `docs/13_codegraph.md`。


---

# 13 · CodeGraph：选定增强，不再继续换图谱引擎

上游：`colbymchenry/codegraph`。本次查看官方README确认有CLI、MCP与程序接口；默认MCP工具主要为 `codegraph_explore`，其他工具可配置。官方也明确存在遥测及退出开关。因此“本地索引”不能被写成“默认绝不联网”。参见 docs/SOURCES.md 的 S03。

## 两个互不混淆的用途
**开发本产品时**：Codex可在用户明确同意后使用项目级CodeGraph；这不会自动构成AETHER产品内的CodeGraph集成。禁止执行会改所有IDE/全局AGENTS/MCP配置的自动安装过程。
**产品运行时**：可信RepoIntelligence adapter为指定project/task/snapshot启动隔离进程。OpenHands只看到经网关过滤的结构查询结果，不获得任意主机路径或全局服务句柄。

## 定案接口与边界
AETHER自己的接口：`explore(snapshot_ref, query, limits)`、`impact(snapshot_ref, symbols, limits)`、`affected_tests(snapshot_ref, changed_files)`。不要把这些方法名假装成上游SDK签名。
优先用固定版本CLI/stdio MCP进行进程隔离；对选中release检查 `--help`、schema和工具可见性。只允许explore先进入模型的工具列表；impact/affected可由适配器固定调用。
服务端由snapshot ID映射私有目录，丢弃actor提供的projectPath。每个进程只能见到批准的代码快照，单独可写缓存；禁止仓库hooks、忽略secret目录、限制文件数/CPU/内存/输出量。

缓存键至少含project_id、repo ID、snapshot内容digest、indexer版本、配置hash和授权epoch。git commit不涵盖未提交修改，所以candidate必须新建content snapshot。query中途源变化应拒绝/重建，不盲信watcher自称实时。
设置 `CODEGRAPH_TELEMETRY=0` 和 `DO_NOT_TRACK=1` 并配合阻断公网；测试实际连接，不只看配置。源码仍可能作为工具结果送给所选模型，须经过数据出口授权和token裁剪。

## 开发顺序与失败方式
F15实现探针、适配器、失效和取消测试，再做同任务graph on/off对照，包含冷索引成本和上下文占用。不承诺vendor自测节省比例。
增强默认off；启用须owner确认、固定release、许可/隐私/真实集成检查通过。2026-10-15 所有者确认启用已安装锁：`config/codegraph.example.yaml` 和功能矩阵仍保持关闭，避免新检出自动打开。`config/codegraph.lock.json` 的 `product_enabled` 为 true。遥测、断网、快照副本、可信套件不缩减这些安全默认值不变。CodeGraph不可用时fallback为项目内有界文本/文件搜索，事件明确记degraded；不得fallback为宿主机全盘搜索。
CodeGraph只建议影响范围与测试，不能减少trusted suite或批准代码。增强未完成时产品可单独声明“CodeGraph未启用”，不能把这个任务标done。


---

# 14 · 必须先跑通的跨组件接缝

## A. 命令、工作流与投影
POST任务在同一DB事务写task、不可变合同版本、command_outbox；返回202表示已接收，不表示已执行。dispatcher把同一个command_id投递到固定task_id的Workflow，确认接受后标记发送。投递成功但ACK丢失可重投；Workflow处理过的command_id/业务版本必须持久去重。
用户审批先写可信审批记录/命令；Workflow在安全点接收后通过Activity核对。不要直接用DB的task.status启动/重试工作。task状态由Workflow产生的单调事件投影；UI显示command pending与projection watermark。
重要事件与相应领域写入在同一DB事务落盘；event_id和(project,task,sequence)唯一。LISTEN/NOTIFY或内存队列仅是唤醒，SSE重连从DB补齐，不能以是否收到推送判断操作是否发生。
HTTP鉴权/资源ACL是DB权威，任务进度是Temporal权威，外部效果最终由远端证据确认；不要声称存在一个覆盖全部分布式世界的万能事实源。
`resource_heads` 上的 `FOR UPDATE` 只覆盖派发事务里的版本预检。事务在外部写入之前就提交，锁随之释放，所以它不是跨进程的并发保护，也不能代替之后的 `note_resource_version` 与对账。

## B. 编码执行器不是主调度器
OpenHands内部的工具循环可以继续，但必须受任务级token、费用、时间、文件和网络上限约束；不能再创建AETHER根任务/自行委派无界子Agent。首选控制worker持有SDK编排，Agent Server只在隔离workspace执行工具；选中release的实际进程拓扑必须探针确认。若SDK逻辑与工具都在sandbox，使用单用途模型token，不给管理权限。
start(project,task,intent)具备持久幂等性；恢复时get旧job而非重新起会话。只重放SDK事件不等于恢复外部进程。F13必须证明API超时、worker死亡、runner死亡各自如何处理。
原始provider key永不进入job。token窃取仅允许消耗本job剩余预算；取消后撤销。黑盒SDK若绕过gateway产生账单，就判接入失败，不能用本地粗略计费遮掩。

## C. 沙箱部署的可用性与安全性
开发目标：macOS arm64可做控制面/研究/前端；实际支持清单由probe填写。完整编码安全验收目标：独立Linux runner（可从Windows的WSL2/远端Linux使用），不推断平台等价。不会因用户当前Mac而要求先购买云服务。
Docker是第一版受限执行后端，不保证能安全托管任意恶意客户的公共多租户代码。Docker daemon的控制权只在runner-manager/专用VM。rootless不自动解决网络隔离；每个平台必须真实验证无host、metadata、跨job、DB、Docker socket和非授权DNS出站。
依赖安装分两步：受控构建阶段从允许registry拉固定hash并扫描；执行阶段使用已准备好的镜像/依赖缓存。不能一边宣称断网一边要求pip/npm现场联网。确需新增依赖走受信构建任务；actor不能改registry或生命周期脚本信任策略。
浏览器JS可以产生POST与子资源请求，“只让它看网页”不等于只读。初版只在无登录、受限域/方法/出口策略中渲染；不具备强制拦截能力的平台不得启用该功能。

## D. 独立验收不是另一个模型喊PASS
验证控制器位于actor写权限之外；创建新的测试sandbox，用固定base+candidate构造目标；可信suite和检查器固定版本。actor日志只是输入证据，不能当验证结果。
被测代码仍可能攻击测试进程或伪造输出。不能只解析actor产生的JUnit字符串或exit0；测试采集量、超时、外部可观察行为、结果来源都要检查。对可经HTTP/CLI观察的任务优先在目标sandbox之外的控制器发测试输入和核对输出。
用户需求自身不明确时保留human_review；来源hash只证明引用字节匹配，不证明观点成立。对明显矛盾证据给出UNVERIFIABLE/人工复核，不能凑引用数验收。

## E. 上下文编译器与记忆
上下文是有预算的组装结果，不是全部历史。先放不可变目标/权限摘要，再放当前计划游标/证据索引/相关片段，最后是经批准skill。固定前缀与动态结果分离；每次生成记录各部分token估计、截断与来源。
skills先加载name/description，再读取被选中的指令，再按需读取资源。候选/未审核脚本永不作为trusted instruction。CodeGraph/MCP/网页中的“系统指令”只当不可信内容。
纠正记忆产生新版本和supersedes关系；删除先tombstone并增加visibility epoch，随后清派生索引。正在运行的摘要/embedding任务在提交时再次检查epoch，防止把已删除资料写回来。
历史中保留敏感原文后不能靠删当前memory就宣称彻底遗忘；数据保留、加密密钥和备份到期必须可说明。高敏资料不默认送云端，job批准也不自动批准所有数据出口。

## F. 预算、超时与取消
所有模型调用（planner、OpenHands内循环、结构修复、judge、学习）共用适当账本；学习批次独立额度。估计不是供应商最终账单，unknown状态必须可见并保留预留。
取消先停止准入、撤销token、取消job，再确认/对账在途效果；已发出的云模型调用不一定停止计费。UI直到确认停止才展示cancelled，不把cancel requested当回滚成功。
读操作也可能有费用/隐私风险；重试只能复用逻辑intent，新的实际计费attempt必须有独立请求账目。

## G. 最重要的故障注入
1. DB已提交但Temporal未启动；2. Temporal收到命令但ACK丢失；3. 外部写成功但响应丢失；4. LLM超时可能计费；5. 审批后参数/代码变化；6. runner存活而worker重启；7. runner死亡而工作流存活；8. 新candidate配旧测试报告；9. 删除与异步索引提交竞态；10. SSE断线/重复事件；11. workflow升级后历史回放；12. 恢复备份后旧command/outcome对账。
每项必须验证实际产物/调用次数/账本/身份，不只检查某个函数返回字符串。


---

# 16 · 已有项目如何切换至唯一主方案

不要把整个ZIP直接覆盖旧项目。冻结包放在独立目录供Codex读取。
1. 记录git status/commit，保留所有未提交变更；不要stash/reset/清库。
2. 用 tasks/legacy_mapping.yaml 对照旧73+13项和真实代码，生成 reports/MIGRATION_PLAN.md：已做功能、缺失测试、冲突接口、最小迁移。
3. 合并当前AGENTS/文档/唯一backlog；旧文档进archive，旧指令不再作为当前任务源。已完成状态只有重跑对应验收后才沿用，不能因为旧STATUS写done就盲信。
4. 保留已验证依赖锁；冻结包没有真实产品lock可覆盖它。检查目标API/数据合同，不兼容时用Alembic增量迁移；不得删用户数据来满足DDL。
5. 当前REST/SQL合同沿用V1.2字段基线；新增ACK/幂等/水位等实现可扩字段，必须明确版本和兼容。接口变更写ADR、更新消费者和测试。
6. 原生记忆已存在就补足来源/删除/冲突；不要搬新记忆服务替换能工作的主线。已装的可选组件先关闭，再各自验证，不直接删除代码。
7. 原有公开开发测试保留并映射。DAG默认深度的纠正是已记录的规格修订：仍测试自定义深度3的拒绝，同时新增默认长链和环/节点上限测试，不借此移除安全检查。
8. 当前工作接续只看本版backlog/STATUS/HANDOFF；不要同时执行V1.3增量任务。


---

# 冻结决策记录

| ID | 裁决 | 原因/验证 |
|---|---|---|
| ADR-F01 | 主方案延续AETHER；DeepSeek只补充 | 用户指定；不同调度栈不拼装 |
| ADR-F02 | Temporal唯一执行权威；PG权威领域账本 | 防止重复调度；F06/F07故障测试 |
| ADR-F03 | 安全准入先于实际工具，UI提前至纵向闭环 | 纠正旧阶段顺序；F04–F09 |
| ADR-F04 | 一个typed planner，最多2读并行/1编码，无递归委派 | 控制复杂度和成本 |
| ADR-F05 | DAG依赖路径默认32，而非旧默认3 | 正常长链不是Agent嵌套；显式depth3仍受测 |
| ADR-F06 | 原生PG记忆默认，高级Hindsight延期 | 先完成来源/删除/纠正，不把记忆选型当开工前置 |
| ADR-F07 | HTTPX/Trafilatura+Playwright；其他浏览器框架延期 | 每类只有一个执行路径，减少复合重试/隐私面 |
| ADR-F08 | CodeGraph选定opt-in，schema/telemetry真实探针 | 不是全局安装；不可降低测试；F15 |
| ADR-F09 | SkillLab可全部拒绝候选 | 不制造正收益，不改变验收去演示进化 |
| ADR-F10 | core/live/optional/research分别完成 | 开工不等待最终holdout或付费key；结论不混用 |
| ADR-F11 | 不同时执行旧86项与新28项 | 全量映射，旧ZIP归档不自动加载 |
| ADR-F12 | 不把public SaaS/多租户/付费/语音列P0 | 单owner多项目产品边界明确 |
| ADR-F13 | 包自检跳过 `.venv`、`node_modules`、`.git`、`.next`、`data`、`secrets`、`__pycache__`、`.cache` | 2026-10-15：安装控制面后 LiteLLM/Temporal 自带 `AGENTS.md`，`jsonschema` 的 `issue.json` 含外部 `$ref`。依赖树不是本包合同；仓库内规格文件仍全量检查。Hugging Face 权重缓存里有空的 `added_tokens.json`，同样不属于规格 |
| ADR-F14 | `aether_api` 不再获得全表 DML，新表默认不继承写权限 | 2026-10-15：Codex P1。权威账本写操作留在 worker/verifier/dispatcher；非 loopback 绑定还要求 TLS 证书与 Secure cookie。测试见 `tests/integration/test_privileges.py` 与 `tests/unit/test_bind.py` |
| ADR-F15 | 词法分词使用冻结的 jieba 0.42.1 词典做正向最大匹配，不把词典提交进仓库 | 2026-10-15：jieba 0.42.1 只有源码包。CI 的 `uv sync --locked --no-build` 装不上它。运行时使用包内 `dict.txt`，缺失时按 `config/tokenizer/jieba-0.42.1.json` 的 URL 下载并核对 sha256。英文和 `!=` 这类代码符号另切，停用词表为空，避免删掉 `不` 和 `not` |
| ADR-CG1 | CodeGraph 固定 `@colbymchenry/codegraph@1.6.2` 与 linux-x64 包的 npm sha512；CLI 只在 `--network none` 的容器里跑；产品默认关闭 | 2026-10-15：GitHub runner 上的非特权 `unshare` 不能当隔离证据。容器镜像摘要是 `debian@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587`。不执行 `codegraph install`。快照副本丢弃 actor 路径、secret 目录和越界符号链接。图谱建议不能改可信套件。不可用时只在快照内搜索，不声称厂商节省比例 |
| ADR-CG2 | 所有者确认后打开已安装锁，示例和功能矩阵仍默认关闭 | 2026-10-15：启用不等于新检出自动打开。`codegraph.example.yaml` 与 `feature_matrix.yaml` 的 `enabled` 保持 false。锁文件 `product_enabled` 为 true。遥测仍是 0，容器仍是 `--network none`。`readiness.codegraph_enabled` 只在 `reports/f15_product_result.json` 的检查全部通过后改为 passed |
| ADR-F16 | 删除先做 tombstone 并增加 `visibility_epoch`，派生索引跟着失效 | 2026-10-15：worker 只能更新 `projects.visibility_epoch` 这一列。晚到的向量和记忆文本写入必须看见同一个 epoch，而且行还没有 `deleted_at`。备份和 Temporal 历史不在这次删除里消失 |
| ADR-F17 | 产物日志的 committed 以 PostgreSQL 提交为准；提交结果未知时保留对账，不把回滚或 tombstone 重新公开 | 2026-10-15：审计 D02/D03。`put(conn=...)` 在外层事务提交前只记 `prepared/awaiting_commit`。提交成功后再读行并写 `resolved` 与 `committed`。提交异常记 `unknown`。显式回滚记 `rolled_back`。`recover` 只返回这次新插入且能读到的规范 id。旧日志里“已 committed 但库中无行”对账为 `stale_commit`，不插入。快照完成标记写在 `snapshot-meta/`，归档成员不能充当完成标记 |
| ADR-F18 | 常驻任务路径用脚本提供者读真实资料，验证者独立读盘后才写终态 | 2026-10-15：审计 R2-01 与 C01。`goal_artifact` 不能把候选哈希同时当作观察值，也不能按任务复用上一条证据。没有来源资料时是 `NEEDS_REVIEW`，检查失败是 `FAILED`，通过才是 `SUCCEEDED`。脚本提供者不是 live 模型，不改变 `live_ready` |
| ADR-F19 | 当前观察决定验证结果；离线摘录不能代替目标完成；任务引用和内容去重分开；暂停会挡住新的副作用 | 2026-10-15：审计 R3-01 至 R3-05。缓存键包含候选、权威合同、验收版本、观察到的摘要和来源快照。历史 PASS 保留，但不作为失效观察的结果返回。脚本提供者只摘录对得上目标的资料，做不到时是 `NEEDS_REVIEW`。`task_artifacts` 让两个任务共用字节而不改写第一条 `task_id`。日志在追加前截到完整前缀，不先清零。`QUEUED` 也可以进入 `PAUSED`，这样还没开始的任务不会被启动。取消不是回滚，不改变 `live_ready` |
| ADR-F20 | 研究任务默认走 live 模型；预算在调用前预留；来源列表按字面执行 | 2026-10-15：沿用网关里的 LiteLLM，不另写客户端。`AETHER_RESEARCH_PROVIDER=scripted` 才使用离线摘录，并在报告里标明。缺 key 是 `NEEDS_REVIEW`，不是成功。费用未返回时保持 unknown，预留继续占用额度。空的 `allowed_source_ids` 表示没有来源，非法 id 不会退回读取其他资料。不改变 `live_ready` 的交付结论；本机没有 key，不把 key 放进 GitHub secrets |
| ADR-F21 | 一次逻辑研究调用只派发一次；预算按将发出的文本估算；短引用不是目标完成 | 2026-10-15：审计 R4-01 至 R4-05。`active_seconds` 是从 `tasks.created_at` 起的墙钟，排队和暂停都算在内。（ADR-F35 取代这一句：active elapsed 只累计 RUNNING 与 VERIFYING，排队、审批、暂停和复核不计入。本条其余裁决仍有效。）调用前用本地 cl100k 估算输入并加上输出上限，不再用固定 512 token 或 0.05 美元代表任意长请求。已知 token 在价格未知时入账，价格未知只继续冻结金额。回报超过预留时记下 reported 值，任务不成功。已派发但没有回执则停止对账，不自动再打供应商。显式来源有一份不可读就整次 `NEEDS_REVIEW`。十二字重叠只记引用，不单独判 `SUCCEEDED`。验收版本改为 `goal-artifact-5`。阻塞调用放到线程里，不占用 Temporal 的事件循环。不改变 `live_ready` |
| ADR-F22 | 数字必须带着单位和主语；引用失败不能成功；派发前要有费用上限和聊天包装；控制命令在调用期间生效 | 2026-10-15：PR #9 合入 `aedbdd5` 后，Codex 仍指出八项 P1。覆盖按「数字 + 紧随单位 + 邻近主语」核对，六十次和九十天对调、摄氏度写成华氏度都失败。空正文仍先记下已知 token 和费用，再进入 `NEEDS_REVIEW`。派发前用本地上限估算最坏费用（输入每 token 100 micro-USD，输出每 token 400），装不下就不发；这不是供应商价目。token 预留按 chat 包装计算，而不是裸 prompt。`observed` 超额进入预算快照和费用接口，不并进已知花费。`execute-interruptible` 让暂停和取消在活动进行时写入数据库，旧历史仍走原路径。控制投递使用单独线程池。剩余时间不大于 0 时不派发。显式来源文件缺失与损坏一样停止。降级迁移先把 `observed` 行映射为 `unknown` 并保留回报金额。不改变 `live_ready`。`comparative_evidence` 仍是 `not_run` |
| ADR-F23 | 目标完成要绑住对象、单位、时段和比较关系；来源身份不因字节去重消失 | 2026-10-15：第五轮审计 `aedbdd5`。同一组数字对调对象、每分钟改成每秒、最多改成超过，都不能成功。目标明确要两件事而资料只支持一件时，结论是 `unverifiable`，不是强 PASS。正确转述仍然可以成功；十二字引用继续写在报告的 `citation` 里，但不单独否决已经对上的转述。验收版本改为 `goal-artifact-7`。空正文和 token 超额都把已知 token 计入 `tokens_used`。令牌发出之后、传输之前若任务已是 PAUSED 或 CANCELLED，就不发送，并释放未发出的预留。已有答案的重试和最终 SUCCEEDED 提交都再核对 `active_seconds`。`project_private` 资料在被任务引用后仍可被来源选择读到。不改变 `live_ready` |
| ADR-F24 | 多对象答案必须带主语；停发许可在真正派发前可以被取消 | 2026-10-16：PR #11 合入 `967b55f` 后 Codex 仍指出的 P1。两个对象的事实不能被没有主语的数字对上。目标拆开的每一部分要碰到自己的对象，共享的「限额」不算。控制命令排在非控制命令后面时仍要抽出来处理。`apply_task_control` 使用单独的控制线程池。派发先取得 `permitted`，暂停或取消提交后把它撤销，`consume` 失败就不再调用供应商。尚未派发就到期的预留改为释放，而不是标成 unknown。输出上限按剩余金额缩小。验收版本改为 `goal-artifact-8`。不改变 `live_ready`，`comparative_evidence` 仍是 `not_run` |
| ADR-F25 | 记忆 claim 不是资料；逻辑产物可以共享字节；一条坏命令不能停掉 worker；终态不能改写 | 2026-10-16：系统审计第一轮里本批负责的项。记忆 claim 使用 `memory_claim`，删除和更正会给旧字节打 tombstone，来源发现和显式名单只接受 `project_private`。显式把记忆 id 当资料会得到 `allowed_sources_invalid`，不把记忆文本送去当证据。同一 `storage_key` 的活行按分类和媒体类型区分，tombstone 不挡住以后的新行；第一次绑定仍会写 `artifacts.task_id`，以保持既有的任务引用测试。命令应用按条使用 savepoint，非暂时错误标 FAILED，`route_pending` 和 `serve` 吞掉单次失败后继续。同一任务的信号失败时，后面的命令回到 PENDING。`resume` 不清除 `cancel_requested`。公开合同哈希取自 `current_contract_id`，终态任务不能改草案。状态变化和 `task.status` 事件在同一事务里写入。事件序号在锁住任务行后分配，冲突则重试。新工作流用 `bounded-activity-retry` 把活动重试限制为 3 次并设置心跳超时，`ArtifactError` 记成 `NEEDS_REVIEW` 而不再抛出。数据库拒绝离开 SUCCEEDED、FAILED、CANCELLED。不改变 `live_ready`，`comparative_evidence` 仍是 `not_run`。活动超时后两次尝试互相改写预算的部分留给派发状态机那一批 |
| ADR-F26 | 未发出的预留可以接管或释放；验收只看模型当时看到的资料；评分不取自候选进程；令牌绑定账本中的单个请求；作业取消要确认容器已停 | 2026-10-16：系统审计第一轮 P1-04、P1-05、P1-06、P2-01、P2-05、P2-07，基线 `d6ed382`。每次执行带 `attempt_id`，只有 `sending` 的预留才记为 unknown；`dispatched`/`permitted` 由重试重新定价接管，网关在联系供应商前拒绝时释放并转人工复核。生成记录写入 `source_manifest`，验收器按清单重读资料，清单缺失、字节变化或提示哈希不符都是 `UNVERIFIABLE`；同一主语、单位、时段、比较关系的资料数值不同（中文数字归一化后）为 `conflicting_sources`，不判 PASS。验收版本改为 `goal-artifact-9`。评分器父进程不导入候选代码，pytest 在降权到 nobody 的子进程里跑，以退出码和随机路径 JUnit 报告为准，每个受信测试都要通过。报告的模式取自生成记录；未报用量是 unknown 并继续冻结，不判超额；LiteLLM 金额标为 `local_price_estimate` 并带 `pricing_version`。模型令牌要有 `task_id`、`request_key`、`attempt_id`，网关在同一事务核对账本后单次作废，任务停止即撤销。作业取消删除令牌和容器，核对标签下无残留才是 `confirmed`，已取消作业不再启动。不改变 `live_ready`，`comparative_evidence` 仍是 `not_run` |
| ADR-F27 | 旧工作流历史按版本标记回放；Alembic 链与建库脚本同形；备份覆盖全部业务表、恢复只合并不删除；技能晋升绑定本次审批与可核验的评测报告、回滚只回到曾部署过的版本；抓取只放行全局地址、MCP argv 拒绝 shell 与内联代码；重跑建库不轮换口令 | 2026-10-16：系统审计第一轮 P1-07、P1-08、P1-09、P2-04、P2-06（SSRF 网段与 MCP argv；终态守卫在 ADR-F25）、P2-08，基线 `bbf2811`；P2-04 的收紧经所有者批准后在基线 `b735f28` 上完成。P1-07（任务工作流按版本标记回放旧历史）：4ba3b2f 的工作流把每条命令都当 start；75ec05b（PR #7）加控制分支、0359da5（PR #11）加 `execute-interruptible`、48281f3（PR #12）改为抽出全部控制命令，控制分支和抽取都没有 patch，4ba3b2f 时期收到过 pause 的历史回放报 `NondeterminismError`。现在新启动的工作流记 `task-command-versions`，直接用当前语义；没有该标记的历史在首个版本与之后版本分叉的点（控制命令、prepare 未就绪、prepare 后有待处理控制）依次询问编号 patch `task-command-legacy-N`：回放时保持首个版本的行为，升级后才到的命令走当前处理；`intake-pause-interruptible`、`execute-interruptible` 或 80f1d39（PR #14）的 `bounded-activity-retry` 出现即证明是之后的版本；80f1d39 在第一次 prepare 就记 `bounded-activity-retry`，所以它与 start 同时排队的控制命令也能回放。五个已发布版本各有回放夹具。剩余风险：没有任何标记能区分以下历史，它们在分叉点按首个版本回放、会报不确定性错误，只能人工 reset 或终止后重新派发——75ec05b 在接收停顿为 0 时收到的任何控制命令；0359da5 / 48281f3 在第一次 execute 之前就排队的控制命令。0359da5 与 48281f3 在「控制命令排在非控制命令之后」时的差异按 48281f3 回放。以后任何工作流改动都要加 patch 和回放夹具。任务结束后是否关闭工作流留给 P2-09。 P1-08（Alembic 链与建库脚本给出同一个库）：`0005_runtime_patches.sql`（`budget_accounts.tokens_held`、`resource_heads` 及其策略和授权）只由建库脚本执行，按 Alembic 升到 head 的库缺这些对象。新增 Alembic `0013`（接在 `0012_artifact_identity` 之后）幂等执行同一份 SQL，建库脚本把 head 记为 `0013`；已被 stamp 成 `0011` 或 `0012` 的库升级后补齐。`0013` 的降级不删对象，因为 `0011`/`0012` 时期由建库脚本建的库本来就有它们。结构比对测试保留，防止再漂移。 P1-09（备份覆盖全部业务表，恢复是不删除任何东西的合并）：原备份只有 4 张表，恢复按备份覆盖并整体替换产物目录：备份后写入的字节被删、备份后完成的任务让恢复撞上终态守卫而整体中止、新库因缺所有者等表无法重建。现在备份在可重复读只读快照里导出全部业务表，schema 出现未登记的表或外键顺序不符就拒绝；旧的 4 表备份以 `backup_tables_missing` 拒绝。恢复规则见 docs/09「保留与备份」：内容行内的生命周期与安全列（记忆状态、索引代状态、分类）保留当前值；产物先在暂存目录合并完整，数据库提交前才换入（Codex 在 PR #16 的审查意见）。限制：Temporal 状态不在备份里，见 docs/10「备份恢复与 Temporal」；在用库上恢复前先停 worker 与派发；恢复后被 STOPPED 的命令目前靠所有者核对后重新提交，还没有自动对账入口。 P2-04（技能回滚经 API 可达且只回到曾部署过的版本；晋升绑定这次晋升的审批、这个版本的评测和可核验的评测报告）：回滚不再要求请求体里没有的 `task_id`：必须写明当前 canary 版本和部署版本号，目标只能是该 canary 的父版本或 active 版本，评测与审批要与该部署一致；回滚只降低暴露面，不另要审批。晋升要求评测由 verifier 写、状态 ELIGIBLE、候选正是目标版本。审批意图摘要必须等于 `{"action":"promote","skill_id","version_id"}` 的摘要，并在部署所在事务里写一条 `skill.promote` 操作（以一条即时撤销的 capability 记录为载体）、设置 `consumed_by_operation_id`；已被部署引用过的旧审批同样拒绝。所有者在 PR #16 合并后（基线 `b735f28`）批准了以下收紧，并批准相应修改验收用例：一、回滚目标还必须曾经部署过：是 active 版本，或操作账本里有一条已确认的 `skill.promote` 把它放上过 canary；只是父版本而从未部署（例如导入后仍是 QUARANTINED 的 v1）则拒绝。回滚后目标版本状态记为 CANARY。二、晋升要求不可变的评测报告并核验真实哈希。`record_evaluation` 传入产物存储时，在 verifier 事务里写规范化 JSON 报告（评测 id、候选版本、数据集哈希、用途、划分、状态、指标、阈值、评测者身份），记入 `report_artifact_id`，并把报告哈希写进 `configuration.report_sha256`。`evaluation_runs` 只有 verifier 能插入、没有角色能更新，所以以它为准；产物账本行 API 与 worker 角色都能更新，不能单独作为依据。晋升时命令里的 `report_sha256` 必须同时等于评测行记录的哈希和产物账本哈希，worker 经 ArtifactStore 重读字节、重算哈希，报告内容必须指向本次评测、本候选版本、同一状态与评测者。没有报告、报告已删除、哈希不符、字节被改、或 worker 没拿到产物存储，一律拒绝，审批不被消耗。之前不写报告的评测不能再用于晋升。三、`skill.evaluate` 保持现有契约：202 受理、记一条 QUEUED、不带分数的评测（`test_console_api` 断言），本次不实现评测执行器。QUEUED 评测不能用于晋升，可晋升的评测只来自 verifier 的 `record_evaluation`。为让验收用例能「先部署父版本」，晋升现在可以替换已有 canary：必须给出当前部署版本号与当前 canary 版本，新审批照常消耗，被替换的版本回到 ELIGIBLE。验收用例 `test_owner_can_roll_a_canary_back_through_the_api` 按批准改为：v1 经真实评测报告与审批先上 canary，v2 用自己报告的真实哈希替换它，再回滚到 v1；原有两条断言不变，另加两个版本状态的断言。新增反例：回滚到从未部署的父版本；无报告晋升；借用另一份报告的真实哈希；worker 改写产物账本哈希；替换磁盘上的报告字节。剩余风险：「曾部署」以 worker 写的操作账本为证据，持有 worker 数据库口令即可伪造；报告字节在产物根目录，能写该目录的人可以改字节，但晋升时重算哈希会发现。 P2-06（抓取只放行全局可路由地址；MCP argv 拒绝 shell 与内联代码）：地址检查以 `is_global` 为准，并显式拒绝 100.64.0.0/10、fec0::/10，以及 IPv4 映射和 NAT64 形式的被拒地址。MCP stdio 的 argv 拒绝 shell、`env`/`sudo` 等转启动器和解释器的内联代码参数（`-c`、`-e`、`--eval` 等）。这仍是兜底，不是安全边界；修复计划要求的「只接受管理员审核过的可执行文件白名单」未在本次实现。 P2-08（重跑建库默认不轮换口令）：`ensure_database` 默认会轮换全部角色口令，升级时照 README 重跑就让在跑的服务断库。现在默认 `rotate_passwords=False`，轮换必须显式传参，README 分开写「升级重跑」和「轮换口令（先停服务）」。集成测试的会话夹具显式轮换。 |
| ADR-F28 | 研究报告单次输出上限是 2048 token；`finish_reason=length` 不是完整回答 | 2026-10-16：F26 研究调用的 512 token 上限会在写完两条 sha256 引用前截断（`dac5c49` 的 `completion_tokens` 正好是 512，报告停在 `src-en`）。编码和协议调用仍是 512。网关现在读取 `finish_reason`；`length` 不把截断正文当成完整回答，研究任务记 `output_truncated` 并进入人工复核，已知用量照常入账。版本字段接受 `version=N`、`version is N`、`version 为 N`、`版本 N`、`版本号：N`，数字必须跟在对应的 source id 上。提示要求写成 `version=N`。不改变 `live_ready`。本机没有 live key，监督者需要重跑 `scripts/run_f26_acceptance.py` 和 `scripts/run_live_task_check.py` |
| ADR-F29 | 研究报告缺少数值 `100` 时原因是 `missing_required_fact`，不是 `citation_mismatch` | 2026-10-16：PR #18 合入后的 live F26 运行里，比较报告没有写出数字 `100`。`_report_cites` 因此失败，`_research_verdict` 把它记成 `citation_mismatch`，看不出是缺事实。报告仍然必须包含 `100`，中文数字不算。缺这个数字时原因改为 `missing_required_fact`；引用不齐或版本不对仍是 `citation_mismatch`。`finish_reason=length` 仍优先。研究提示要求每条来源写出笔记里的数值和单位，并写明 src-zh 与 src-en 都要写出数字 `100` 和单位。不改变 `live_ready`。本机没有 live key，监督者需要重跑 `scripts/run_f26_acceptance.py` |
| ADR-F30 | 协议评分把断言留在候选碰不到的进程；白盒 pytest 不能认证或自动发布；技能指标拒绝非有限值 | 2026-10-16：复审 `8b90363` 的 SYS-02 / SYS-07。旧的继承 fd 伪造已经失败，但候选与 pytest 同解释器时，替换 `Function.runtest` 仍能让错误实现得到 PASS。现在能归约成「探针 == 字面期望值」的套件由父进程比较期望值；nobody 子进程只返回探针的值，套件目录对其不可读。正确实现 PASS，错误实现、pytest 补丁和旧 fd 伪造都 FAIL。不能这样归约的白盒 pytest 标记为 `cooperative`，即使 JUnit 通过也不是 PASS，不记安全认证，不自动发布。`record_grade` 拒绝没有隔离认证的 PASS，也拒绝 `auto_promote`。技能 `judge` 对 families / ci_lower / success_delta / cost_ratio 做类型、有限数和范围检查：字符串 `"NaN"`、NaN、Inf、-Inf、bool、缺失和越界都不是 ELIGIBLE。范围与 `PromotionMetrics` 一致（families 为非负整数，ci_lower 与 success_delta 在 [-1, 1]，cost_ratio ≥ 0），原有门槛不变。不改变 `live_ready`。未覆盖：容器逃逸、ptrace、猜中期望值、不能归约的白盒断言。`os.kill(-1, SIGKILL)` 只在评分容器的 PID 命名空间内执行 |
| ADR-F31 | 确认记忆不降低敏感度；测试运行要有验证者账本；批准过的验收条款被冻结 | 2026-10-16：外部审计基线 `8b90363` 的 SYS-06 与 SYS-04。确认和更正继承 `provenance.sensitivity` 与 `source_artifact_ids`，请求体里的更低敏感度或空来源列表被忽略。显式降级是单独的 `declassify`，必须有一条一次性 `memory_declassify` 授权，范围写明记忆 id、原级别和目标级别；授权在使用时撤销。已经是目标级别时是 `declassification_rejected`，不消耗授权。授权缺失、过期、已用或绑到另一条记忆时是 `declassification_unauthorized`。降级后仍保留来源，认识状态保持原值，不自动变成 `user_confirmed`。`_is_project_test_run` 不看 JSON 的 `kind`。产物必须已提交、媒体类型是 JSON、重算哈希等于账本，且正文没有把运行记成失败（`failed`/`fail`/`error`、非零 `exit_code`、`passed` 为 false）。还要有验证者 `aether_verifier` 写入的 `criterion_id=test_run` 行，`artifact_sha256` 与 `observed_sha256` 都等于这些字节；绑定上的结论必须全部是 PASS，PASS 与 FAIL 并存则拒绝。`goal_artifact` 和 `aether_api` 的行不算。没有草案时合同仍是 `{criterion_id: goal_artifact, goal}`。有草案时冻结其中的验收条款和 `required_artifacts`，目标对不上就报错，不插入更弱的自动合同。已有合同不改写。条款存在时，目标摘录通过之后仍要过这些条款：`human_review`、`json_schema`、`trusted_test_suite` 以及未知模板是 `unverifiable`；`source_coverage` 若列出 `required_source_ids` 也是 `unverifiable`，因为这条路径没有来源 id。验收版本保持 `goal-artifact-9`。不改变 `live_ready`。剩余风险：降级授权由 worker 写入，持有 worker 数据库口令的人可以发授权；失败正文即使另有 PASS 行也不能升成 `observed_test`。变基到 `85d27d3` 时 ADR-F30 已被 SYS-02/07 占用，本决定改记为 ADR-F31 |
| ADR-F32 | 发布报告只认当前 JUnit 与绑定证据；候选快照不跟随符号链接 | 2026-10-16：云端复审 8b90363 的 SYS-08 与 SYS-03。`build_report` 聚合整棵 JUnit，核对 suite 计数与实际用例，区分 pass、fail、not_run、blocked。空套件、全跳过、后段失败、计数不符都不是通过。`live_ready` 还要当前 commit、环境、模式、配置哈希和产物哈希与 JUnit 属性、STATUS 引用的验收结果一致；结果文件 `live_ready` 为 false 时不能变成 true。`reports/f26_acceptance_result.json` 仍是 d694f89 且 `live_ready` 为 false，未改字节。`STATUS.json` 的 `readiness.live_ready` 改为 pending，`f26.live_ready` 改为 false。`export_candidate`、`prepare_workspace`、`_stage` 只用 `O_NOFOLLOW` 和目录描述符读取授权根里的普通文件。符号链接、设备节点和越界根目录直接拒绝。写出的目录是 `0o750`，其他用户没有权限；评分子进程 uid 65534 只通过命名用户 ACL 获得目录的读和执行。协议评分仍由 `_publish_protocol` 把套件收成所有者独占，这份 ACL 不会让 nobody 读到期望值。runner.cancel 的撤销与确认没有改。没有改 contracts、reference，也没有放宽 F26 的 `100` 检查。本机没有 live key，所有者重跑之前 live 保持 pending |
| ADR-F33 | 技能评测和编码作业在公开入口返回 `unsupported`；已批准的 Watch 用 Temporal schedule 循环；重启后续跑仍在 QUEUED 的采集 | 2026-10-16：SYS-01。变基到 `e2e4c6d` 时 ADR-F30 已被 SYS-02/07 占用，ADR-F31 已被 SYS-04/06 占用，ADR-F32 已被 SYS-08/03 占用，本决定改记为 ADR-F33。ADR-F27 把 `skill.evaluate` 留成 202 受理加一条不能晋升的 QUEUED 评测，编码作业同样只插入 QUEUED 行。这次不补一个会伪造通过结果的执行器。`POST .../skills/{skill_id}/evaluate` 和 `POST .../coding-jobs` 在格式合法且项目属于当前所有者之后返回 422，`code` 为 `unsupported`，不写 outbox、评测或编码作业。控制台和代码页直接写明不支持。`docs/08_api_ui.md` 的编码作业行改为 unsupported。采集（url、rss、local_document）由生产 worker 执行到 SUCCEEDED 或 FAILED。已批准来源的 Watch 在 worker 自己的任务队列上创建 Temporal schedule，间隔取 `interval_seconds`，`trigger_immediately` 跑第一次；水位变化才写通知和 `watch-run` 产物，水位不变不外发。工作流用 `watch-run-activity` 标记，旧的单参数历史仍只返回 watch id。worker 把见过的 project_id 与 owner_id 写进索引根目录的 `projects.json`。重启后重新加载；命令已经是 DELIVERED、采集仍是 QUEUED 或 RUNNING 时继续执行。`AETHER_DEFER_PROGRESS=1` 只用于把命令留在已交付、采集留在 QUEUED，默认关闭。不改变 `live_ready`。剩余风险：github/arxiv 采集仍显式拒绝；水位没有变化时不发通知；持有 worker 数据库口令的人仍可以伪造账本 |
| ADR-F34 | GitHub 与 arXiv 走真实抓取；已批准工具由 worker 经 MCP 和操作账本执行；来源删除递增 visibility epoch；推荐 GET 不写画像和曝光 | 2026-10-16：P2-09 的公开采集、S-060、S-048、S-006，基线 `cc471e3`。`github_api` 与 `arxiv_api` 不再在 `POST /ingestions` 返回 unsupported。worker 仍用钉住地址的 HTTP 客户端。GitHub 只连接 `api.github.com`；只有 `AETHER_INGEST_ALLOW_FIXTURES=1` 时才接受 `AETHER_INGEST_FIXTURE_HOSTS`。仓库根改写成 `/readme`。只解码内联 base64，目录列表和缺少正文都是 `parse_rejected`，不跟随 `download_url`，也不跟随重定向。arXiv 编号展开为 `https://export.arxiv.org/api/query`。Atom 在原有隔离进程里读取 entry 的 title、summary 和 author，空白标题跳过。正文走既有切块、嵌入和索引，之后 `POST /search` 能检索。`route_pending` 在采集之后调用 `dispatch_approved_tools`。它只使用已审核 manifest 里的 `argv` 和 `bindings`。旧的 `{"tools":["echo"]}` 没有 bindings，probe 仍返回 unavailable，不打开传输。风险和幂等以注册表为准，忽略 MCP annotations。授权必须已经存在，本路径不发放 capability。MCP 未知结果记为 UNKNOWN。幂等工具在同一次调用里用新进程对账；`non_idempotent` 不重试。`_mark_task` 只把非 SUCCEEDED、FAILED、CANCELLED 的任务改成 NEEDS_REVIEW，终态保持不变。成功的 `source.delete`（恰好更新一行）给该来源的 chunk 打 tombstone，并只递增 `visibility_epoch`、丢掉该项目的检索缓存。不调用 `invalidate_derived`，因此不退休其他索引代，也不分配画像版本。重放删除不再递增。`GET /recommendations` 改为只读预览：不插入 `profile_snapshots`，不插入 `recommendation_impressions`。没有画像时 `profile_version` 为 0，`impression_id` 为 null。这与 OpenAPI 里 impression_id 必填 UUID 不一致；合同没有改，偏差记在本条。`rank_candidates` 仍是显式写入。反馈可以不带 impression_id。有意改动的既有断言：`tests/integration/test_console_api.py` 的 `test_recommendations_feedback_and_costs_read_the_ledger` 不再要求 GET 写出曝光行，反馈请求也不再带 impression_id。标题、exploration 为 false、policy_version、反馈条数和检索 503 仍在。不改 contracts、reference，不把 `live_ready` 改成 passed。编码作业和技能评测仍是 unsupported。剩余风险：BM25 文件里可能还留着已删除文本的字节，返回前会按 tombstone 滤掉；持有 worker 口令的人仍可写账本；夹具主机只在显式开关下可用 |
| ADR-F35 | 复核有出口；合同只由所有者批准；活动时间不计等待；工作流可结束并 continue-as-new；投递有租约；审批失败不卡住幂等键；超额可对账；成功任务的答案和验收报告不能删除 | 2026-10-16：P2-09 的 S-071、S-032、S-015、S-016、S-010、S-019、S-033、S-043，以及 docs/04 §2 的 NEEDS_REVIEW 出口。变基到 `6f223f7` 时 ADR-F34 已被公开采集占用，本决定记为 ADR-F35。NEEDS_REVIEW 记下原阶段（QUEUED、RUNNING 或 VERIFYING），经现有审批决定批准后回到该阶段，拒绝则取消并撤销未发出的预留。不新增公开命令。`budget_unreconciled` 批准时确认账单，拒绝时释放冻结。没有预批准模板登记，因此不实现低风险自动批准：worker 不得把 start 命令的 owner_id 写成 `goal_contracts.approved_by`。执行前必须有所有者的 `approve_goal`，且 `owner_id` 等于项目所有者；可选的 `expected_contract_sha256` 必须等于冻结合同哈希。`scripts/run_live_task_check.py` 在看到合同哈希后显式提交 `approve_goal`。active elapsed 由 `task.active` 的 start/stop 累计，只含 RUNNING 与 VERIFYING；未闭合的一段用当前时间减去起点，所以已开始的调用在 worker 离线时继续计时。排队、审批、暂停和复核不计入。continue-as-new 不重置这笔账。这取代 ADR-F21 里墙钟那一句。新工作流用 `task-run-bounded`：SUCCEEDED、FAILED、CANCELLED 结束本次 run；历史达到 5000 或服务端建议时在命令边界 continue-as-new，游标只带 task、已见命令和待处理命令，不带大产物。没有该标记的旧历史仍等到 close。工作流 ID 复用改为 `REJECT_DUPLICATE`；已结束的 run 不再被启动成新 run，后续命令标为已投递。投递器领取时写租约，退避为 2 的已尝试次数次方秒、上限 60 秒，第 8 次失败标 FAILED。同一任务上租约未到期的更早命令会挡住后面的命令。审批决定失败后，幂等键写入真实的 `response_status` 和错误体，不再停在 0。只有决定接口允许同一载荷恢复未完成的键；不同载荷仍是 409，且不覆盖原键。相同决定且意图哈希相同的重复 `decide` 视为成功。超额对账在 worker：`confirm` 释放冻结并计入回报金额，`release` 只释放冻结；同一决定可重复，相反决定不改口。确认之后不再仅因 `book_result=overrun` 进入复核。S-043 选择禁止删除而不是把 SUCCEEDED 降级：终态不能复活，ADR-F25 与数据库触发器拒绝离开 SUCCEEDED、FAILED、CANCELLED。已成功任务的 `task_answer` 与 `task_verification_report` 删除返回 409 `evidence_retained`。`project_private` 上传和来源仍可删除。不改 contracts、reference，不把 `live_ready` 改成 passed。剩余风险：被终止且任务尚未终态的工作流不会自动再启动；没有模板登记，所以规格里的低风险自动批准仍未实现 |
| ADR-F36 | 编码作业和技能评测跑到可查询终态；编码成功状态是冻结的 FINISHED | 2026-10-16：P2-09。变基到 `5fa78f7` 时 ADR-F35 已被复核出口占用，本决定改记为 ADR-F36。ADR-F33 把 `POST .../coding-jobs` 和 `POST .../skills/{skill_id}/evaluate` 临时收成 422 `unsupported`。那条只覆盖这两个入口；Watch 循环和采集续跑仍按 ADR-F33。任务、基线产物或技能版本不存在时现在返回 404 `not_found`，不写 outbox。格式合法则 202 受理。编码作业由 worker 消费 `coding.create`，在 `python:3.12-alpine` 里跑所有者放进基线产物的候选和测试。容器是 `--network none`、`--read-only`、`--cap-drop ALL`、`no-new-privileges`、`--pids-limit 64`、内存与 swap 各 268435456 字节、`--cpus 1`、`/tmp` 为 tmpfs、用户 `65534:65534`。不挂 Docker socket，不继承 worker 环境，启动参数里没有 `AETHER_LIVE_MODEL_KEY`。测试通过、`network_errno` 不是 0、环境里没有凭证名时，状态是 `FINISHED`。测试失败是 `FAILED`。`coding_jobs.state` 的检查约束没有 `SUCCEEDED`，所以成功终态不用这个词。失败后同一 `task_id` 和 `job_key` 再提交走 `coding.retry`：`fencing_token` 加一，新建 operation 和 capability（`intent_key` 与 `token_hash` 不能复用），清掉上次产物，回到 `QUEUED`。还没失败的作业是 409 `job_not_retryable`。预算沿用该任务 start 命令里的预算；没有则打开与目标执行相同的默认账户。每次尝试预留 1 micro-USD 和 1 token，沙箱结束后按实际 0 结算，因为这次不调用模型。预留之后进程失败则 `mark_unknown`，未决账单继续占用。产物分类是 `coding_patch`、`coding_test_result`、`coding_events`，并挂到任务。公开合同没有单独的编码作业查询，终态从任务产物和作业行读取。这次不启动 OpenHands，也不发作业范围的模型调用。技能评测不插入不能更新的 `QUEUED` 行；`evaluation.create` 仍只插入没有分数的 `QUEUED`。`skill.evaluate` 留在 PENDING，直到 verifier 按数据集清单用 SYS-02 隔离评分器逐项评分，再由 `record_evaluation` 写入不可变评测和报告。指标沿用 SYS-07，拒绝 NaN 和无穷。`baseline_cost` 为 0 且 `candidate_cost` 不为 0 时，在启动容器之前记 `REJECTED`，`metrics` 为 NULL，`configuration.error_code` 为 `non_finite`。一个 family 是 `INSUFFICIENT_EVIDENCE`，版本变为 `EVALUATING`，不晋升。development 用途遇到 holdout 是 `dataset_isolated`。公开技能列表读 `manifest.status`，`apply_verdict` 同时改状态列和 manifest。终态用 `GET /v1/projects/{id}/evaluations` 查询。不改变 `live_ready`。剩余：没有模型网关；规格第 6 节的取消确认和 TTL 回收这次没有做；持有 worker 数据库口令的人仍可以伪造账本 |
| ADR-F37 | 编码作业取消要确认容器已消失；TTL 回收不能碰还持有租约的作业 | 2026-10-16：规格第 6 节，接在 ADR-F36 之后。任务命令 `cancel` 把 `tasks.resume_state` 写成 `cancel_requested`。编码作业在轮询里看到这个状态，或任务已经是 `CANCELLED`，就先收集限长 stdout/stderr，再用 `docker stop -t 10` 停掉带 `aether.coding=<job id>` 和 `aether.role=coding` 标签的容器，然后第二次按标签列出。只有列表为空才把 `coding_jobs.state` 写成 `CANCELLED`，撤销这次 capability，并把该次预留按实际 0 结算。事件记下 `cancel_state=confirmed`、`external_effect=unknown`、文件 manifest、patch sha256 和日志尾巴；工作区目录在这些结果落盘之后才删除。作业行不删除。容器还在，或 Docker 没有给出列表时，状态留在非终态，租约清掉，已预留的账单 `mark_unknown`，下一轮只重试停止，不另起一个容器。容器名由作业 id 生成且稳定，启动时如果同标签容器还在就附着，不建第二个。租约写在 `operations.lease_until`，领取和每次轮询续 30 秒。回收只处理截止时间已到、且租约为空或已过期的未终态作业；条件更新抢不到租约就不调用 docker，也不删除行。确认停掉之后，截止时间到达记 `FAILED` / `deadline_exceeded`。同一 `job_key` 只有 `FAILED` 可以重试，`CANCELLED` 仍是 409 `job_not_retryable`。ADR-F26 的 `LinuxRunner.cancel` 没有改。不启动 OpenHands，也不发作业范围的模型调用。不改变 `live_ready`。 |
| ADR-F38 | 审查意见收紧编码作业和技能评测的围栏、预算、基线和可查询终态 | 2026-10-16：PR #26 的审查。编码作业每次状态迁移和终态写入都带上当次 `fencing_token` 与 `operation_id`，过期尝试不能改写重试后的行。同一 `task_id` 与 `job_key` 在受理事务里用 `pg_advisory_xact_lock` 加 `hashtextextended` 串行；已有未消费命令时再次 202 返回同一个资源，不再插入第二条命令。沙箱日志只保留尾部。`async def test_*` 记为失败，不计入通过。`skill.evaluate` 只在请求带了 `budget.active_seconds` 时把这个秒数当作整次评测的截止时间，到期停掉具名评分容器并记 `deadline_exceeded`；没有该字段的请求不另加期限。`baseline_version_id` 必须属于同一技能，否则 404 且不写 outbox；合法基线写入不可变的 `evaluation_runs.baseline_version_id`。`apply_verdict` 不覆盖 `CANARY` 或 `ACTIVE`。`security_failure` 先于样本量门槛，小样本的安全失败是 `REJECTED`。终态追加任务事件 `coding_job.terminal`，代码页用现有 `streamTaskEvents` 查看并保留作业编号，不新增合同路由。不改变 `live_ready`。 |
| ADR-F39 | 采集遵守已批准域名并读取本项目产物；抓取失败进入终态；已激活的跟踪可以暂停，没有批准人的 ACTIVE 跟踪不执行；词法检索不依赖向量进程；来源过滤发生在截断之前 | 2026-10-16：PR #23 合入 `cc471e3` 后仍开放的八条 Codex 意见。变基到 `cfbc65d` 时，ADR-F34 已被 GitHub/arXiv 采集占用，ADR-F35 已被任务流程复核占用，ADR-F36、ADR-F37 与 ADR-F38 已被编码作业和技能评测占用，本决定改记为 ADR-F39。URL、RSS、`github_api` 与 `arxiv_api` 在首次连接前按 `approved_config.allowed_domains` 做大小写不敏感的精确主机名比对；URL 与 RSS 在每一次重定向上同样比对。空名单拒绝这些远程抓取。`github_api` 与 `arxiv_api` 仍只连接官方主机，或在 `AETHER_INGEST_ALLOW_FIXTURES=1` 且主机属于 `AETHER_INGEST_FIXTURE_HOSTS` 时连接夹具主机；白名单是额外条件。这两类来源仍不跟随重定向。非官方主机在白名单之前就被拒绝，错误码仍是 `source_host_rejected`。这不放宽 SSRF：只有夹具开关打开时，已批准域名才交给既有夹具放行。`local_document` 的 locator 必须是本项目产物 id，由 ArtifactStore 读取；不存在是 `not_found`，不是合法 UUID 是 `artifact_missing`。httpx 与 httpcore 的超时、连接失败和协议错误记为 `fetch_failed` 并结束该次采集，不再把 RUNNING 留到下一轮。编码作业和技能评测仍按 ADR-F36、ADR-F37 与 ADR-F38 跑到可查询终态；本决定不改这两条入口的合同和路由。控制台把 ACTIVE 跟踪改为 PAUSED，并在激活的同一条 UPDATE 里写入 `approved_by`；ACTIVE 上的其他修改仍拒绝。PAUSED 与 DELETED 会删掉 Temporal schedule。执行器拒绝 `approved_by` 为空的 ACTIVE 行。词法检索不调用编码和重排进程。非空 `source_ids` 先限定候选，再做向量与 BM25 的 Top-K 和重排；缓存键包含模式和来源。过滤检索只对允许的 chunk 位置累加已建索引里的 BM25 分，不再向 BM25 请求整库的 k。`local_document` 按产物媒体类型解析：`text/plain`、`text/markdown`、`text/vtt` 按 UTF-8 解码，`application/pdf` 只抽出 `Tj`/`TJ`/`'` 显示的正文（含 FlateDecode，解压上限 8MiB，正文上限 100 万字符），其他媒体是 `parse_rejected`，不把 PDF 容器或压缩字节当正文。PAUSED 与 DELETED 的跟踪在确认 Temporal schedule 已不存在后，于同一 `version` 把 `schedule_removed` 写入 `schedule_spec`；worker 循环跳过已记过的行，不再每 250ms describe。从 ACTIVE 暂停或再次激活时清掉这个标记，所以重新启用后的 schedule 仍会被删一次。不改变 `live_ready`，不改 reference 和验收门槛。持有 worker 数据库口令的人仍可以改账本 |
| ADR-F40 | 验收库口令、供应商 key 和 Docker 控制权分属独立进程；沙箱镜像用摘要固定；产物在启动时恢复并容忍短读；崩溃演练与 Temporal 对账 | 2026-10-16：P2-09 的凭证、镜像、产物和恢复项（S-041、S-042、S-064、S-077、SYS-09），基线 `cc471e3`。变基到 `6f223f7` 时 ADR-F34 已被 GitHub/arXiv 采集与工具账本占用。变基到 `5fa78f7` 时 ADR-F35 已被任务流程占用。变基到 `cfbc65d` 时 ADR-F36、ADR-F37、ADR-F38 已被编码作业、取消回收和审查收紧占用。变基到 `b75fb36` 时 ADR-F39 已被采集域名、抓取终态和跟踪批准占用，本决定改记为 ADR-F40。编码作业使用的 `JOB_IMAGE` 改为同一 digest，取代 ADR-F36 正文里的 `python:3.12-alpine`。操作者仍然导出 `AETHER_VERIFIER_DATABASE_URL` 和 `AETHER_LIVE_MODEL_KEY`。`python -m aether_worker` 把验收库地址经管道交给验收进程、把供应商 key 和 worker 库地址经管道交给网关进程、把 Docker 探测交给 runner 进程，然后从本进程环境删除验收库地址和供应商 key，再用去掉这两项的环境重新执行自己。`/proc/<pid>/environ` 只记录启动时的环境，所以必须重新执行，弹出变量不够。验收进程用 `aether_verifier` 写 `verification_results`。网关进程持有供应商 key，并用 worker 库角色核对已经发出的任务令牌后再调用上游。runner 进程执行 `docker info --format {{.ServerVersion}}`，非空才是 `enabled`，否则是 `unsupported`。worker 角色调用 `_docker` 得到 `docker_forbidden`，探测不再接触 daemon。编码沙箱在 worker 角色下不调用本进程的 docker，改由 runner 按固定动作启动和停止容器。技能评分在验收进程执行，worker 只把返回的结论写回版本状态。进程内测试仍可构造带 `verifier_url` 和 `live_model_key` 的 `WorkerEndpoints`，行为不变。`CodingJobResult.status` 仍是合同里的 `ACTIVE`；`unsupported` 记在作业记录的 `runtime` 字段。沙箱、隔离和 F26 校验镜像都是 `python@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4`。评分镜像用 `--require-hashes --only-binary=:all:` 安装 `pytest==8.3.5` 及其依赖轮子，运行时引用构建出的镜像 id，不引用 `aether-grader:local` 或 `python:3.12-alpine`。`serve` 在打印 `aether-worker-ready` 之前对 `projects.json` 里的项目调用 `ArtifactStore.recover`。`get` 循环读到账本长度加一字节或文件结束，一次短读不再报 `hash_mismatch`。崩溃演练脚本备份 Postgres 与演练容器的 Temporal sqlite，只删除该演练标记指向的 sqlite，恢复后对账。恢复仍是 ADR-F27 的合并：`tasks` 不在内容表里，备份后变成 `SUCCEEDED` 的行保留；Temporal 历史不回退。对账结果是 `diverged` / `human_review`，不改写终态。演练写入的备份、sqlite 和报告必须位于当前工作目录内。工作流编号只接受字母、数字、点、下划线和连字符。Postgres 与 sqlite 在 Temporal 停止期间一起复制，确认口令和备份哈希通过之前不替换现场状态。删除前把数据库路径解析到名为 `aether-drill-` 加 8 位十六进制的目录。`SUCCEEDED` 只和 `COMPLETED` 对齐，和其他终态记为 `diverged`。worker 从 `goal_effect.ENDPOINTS` 读取后来配置的验收地址。覆盖 `role-pids.json` 之前先结束上一组角色进程，并且只结束命令行仍是验收、网关或 runner 的进程。回环 HTTP 不跟随 `HTTP_PROXY`。演练容器以宿主机 uid 运行，并设置 `USER=temporal` 与 `HOME=/state`，因为镜像 passwd 没有 CI runner 的 uid 时进程会在打开 sqlite 之前退出。不改变 `live_ready`。剩余风险：同一操作系统用户仍能自行打开 `/var/run/docker.sock`；网关进程持有 worker 数据库口令才能核对令牌；子进程崩溃后要重启 worker，不会自动拉起；演练不能停掉共享的内存 Temporal |
| ADR-F41 | 采集的解析、切分和重试改用固定版本的开源库；SSRF、产物账本和隔离边界仍手写 | 2026-10-16：R8-A，基线 `a2b9313`。origin/main 的最后一条已合并决定是 ADR-F40，本决定记为 ADR-F41。不改 `contracts/`、`reference/`，不改 CI 的 `uv sync --locked --no-build`，不把 `readiness.live_ready` 改成 passed。RSS 与 Atom 仍在子进程里解析，保留 `RLIMIT_CPU`、`RLIMIT_AS`、`AETHER_PARSER_SPIN` 超时和 pid 检查。解析器换成 feedparser 6.0.14。feedparser 会展开自定义实体，所以在调用它之前按 BOM、UTF-8、UTF-16 与 UTF-32 解码（载荷里有 NUL 时这些宽编码都试一遍），再拒绝解码文本中的 `<!DOCTYPE` 或 `<!ENTITY`；空 channel 仍是 `empty_body`。arXiv 仍跳过空白标题，读取 summary 和 author。网页正文换成 trafilatura 2.3.1：看起来像 HTML 的 URL 取主文本，纯文本 URL 仍按 UTF-8 解码。script 正文不进入抽取结果；script 的 src 用 lxml 读取。SSRF、钉住 IP、`trust_env=False`、`follow_redirects=False` 和传输层 `retries=0` 仍在 httpx 客户端里，每次尝试重新 `assert_public_target`。采集传输本来就是 httpx，这次不另写客户端。本地 PDF、DOCX、PPTX、XLSX 换成 docling-slim 2.140.0 的 `format-pdf` 与 `format-office`，不装完整 `docling`，避免 torch。PDF 使用 `NativePdfPipelineOptions`，`parser_threads=1`，不生成页面图，不 OCR，不下载布局模型。正文拼接 `TextItem` 和表格单元格，不用 markdown 表。空白、超过 100 万字符，或 docling 报 `ConversionError`，都是 `parse_rejected`。解析留在 worker，不搬到 `services/ml`，也不为每个文件再起子进程。中文分词改为 jieba 0.42.1 的 `lcut`。jieba 0.42.1 只有源码包，CI 的 `--no-build` 装不上，因此提交由该 sdist 打出的 wheel `third_party/wheels/jieba-0.42.1-py3-none-any.whl`，`pyproject.toml` 用 `[tool.uv.sources]` 指向它。运行时核对包内 `dict.txt` 的 sha256，必须等于 `config/tokenizer/jieba-0.42.1.json`，不再下载词典。英文词和 `!=` 这类运算符在中文段之前切开，因为 `jieba.cut` 会拆开 `!=`。`TOKENIZER_VERSION` 改为 `jieba-0.42.1-cut+code-symbols`。这取代 ADR-F15 的正向最大匹配；停用词仍为空。技能和带闭合 frontmatter 的 markdown 改用 python-frontmatter 1.3.0。技能文件必须以 `---` 开头且 metadata 非空，缺 name 或 description 仍是 `metadata_invalid`，正文是 content 去空白后再补一个换行。没有 frontmatter 的 markdown 原样返回；缺闭合分隔时 metadata 为空，文档路径返回原文，技能路径是 `frontmatter_missing`。切分改用 langchain-text-splitters 1.1.3 的 `CharacterTextSplitter`：分隔符为空、`chunk_size` 为调用方宽度、`chunk_overlap` 为 0、`strip_whitespace` 为 false，长度用 `len`。这样码点窗口和既有 offset 测试一致。不用 Docling HybridChunker，因为它要 DoclingDocument 和 HF tokenizer，不能对规范化纯文本给出码点 offset，还会另起一套索引。`CHUNKER_VERSION` 保持 `codepoint-1`，`PARSER_VERSION` 保持 `text-1`。规格里的 512 token 与 overlap 64 仍是目标，这次不改已经测过的码点合同。采集 HTTP 的重试换成 tenacity 9.2.1。429、503 和 `httpx.TransportError` 最多 3 次。Retry-After 只接受有限数字秒，采用该值并上限 8 秒；NaN、正负无穷和非数字走 `wait_exponential(multiplier=0.2, min=0.2, max=2)`。429 与 503 耗尽后是 `fetch_failed`。传输错误用 `reraise`，`pending_jobs` 仍记 `fetch_failed`。`IngestError` 不重试。不替换 outbox、幂等或 fencing。ArtifactStore 只增加三种 OOXML 媒体类型，并用 PK 魔数核对；账本不改。zip 仍是 `parse_rejected`。有意变化：没有页树的玩具 PDF 变为 `parse_rejected`，测试改用 docling 能读的 PDF，断言文字仍是「苹果派」「a(b) A」「苹果派烘烤说明」，且不含 `%PDF` 和 `endstream`。HTML URL 的正文改为 trafilatura 主文本，纯文本 URL 不变。持续的 429 或 503 最终是 `fetch_failed`，不再把错误页当正文。分词从正向最大匹配改为 `jieba.cut`，「研究生命起源」切成研究、生命、起源。script 正文不再进入网页抽取文本，安全测试仍要求「锁文件存在」和 script src。嵌套 XHTML 的 RSS description 使用 feedparser 的 summary。审查收紧仍记在 ADR-F41：以 `---` 开头但 YAML 非法的 markdown，`yaml.YAMLError` 变成 `IngestError("parse_rejected")`。`_run_ingestion` 捕获 `IngestError` 后把作业写成 FAILED，PyYAML 异常不再把作业留在 RUNNING。ACTIVE 索引代在读缓存和做 BM25 之前核对行上的 `tokenizer_version` 与 manifest 里的 `tokenizer_version`，不等于当前 `TOKENIZER_VERSION` 时是 `RetrievalError("tokenizer_mismatch")`，检索接口仍映射为 503。search 没有产物库，这次不在查询里重建；下一次 `force=True` 的采集会重建。非 ACTIVE 代仍返回 stale，不检索。重定向留在同一次 `_fetch_attempt` 内，整条链共用 3 次预算；目标上的传输失败从原 URL 重试，不再对每一跳套一层 tenacity。网页在判断 HTML 之前跳过 BOM、空白、XML 声明和已闭合注释；未闭合注释仍按纯文本。技能 `load_metadata` 只读到第二个 `---`，正文留给 `load_body`。剩余风险：docling 在 worker 进程内运行，没有单独的 rlimit；解码后的实体闸仍会拒绝正文里碰巧含有该字样的 feed；含 NUL 的载荷会多试宽编码，理论上可能误拒；换库不放宽读到云模型前的数据目的地约束。jieba 的缓存写在 `.cache/tokenizer`，ADR-F13 已把该目录排除在包检查之外。2026-10-16 补记：push run 38964656426 里 `test_text_pdf_is_indexed_as_extracted_text` 在 90 秒时作业仍是 RUNNING，`source_version_id` 已写入，`error_code` 为空，worker 未退出，日志只有 `aether-worker-ready`。抽取结果「苹果派烘烤说明」不在测量表，卡在索引构建调用的 `encode_texts.py`（先 `HfApi().model_info`，再加载 BGE-M3）。这不是 docling 冷启动，也不是 worker 未领任务，没有吞掉的异常。采集路径传入 `allow_download=False`：测量表中的向量仍写入；只有未测量正文在缺少该修订快照或不能离线导入时跳过实时编码，词法索引照常发布。不把 90 秒加长。显式 `build_generation` 仍默认允许编码进程访问 Hub，并用 `model_info` 的 sha 核对修订。整批跳过向量并强制 `HF_HUB_OFFLINE` 会丢掉已测量短语，该做法已收回。 |
| ADR-F42 | 编码执行、技能用例调度、请求体、非密钥配置、密钥扫描和容器调用改用固定版本的开源库；沙箱策略、凭证分离、预算账本和独立验收边界保持手写 | 2026-10-16：R8-B。变基到 `13bc096` 时 ADR-F41 已被采集解析占用，本决定改记为 ADR-F42。本决定取代 ADR-F36 与 ADR-F37 里「不启动 OpenHands」这一句，只替换编码作业的执行器。OpenHands 四个包钉在 1.54.0，只装在 `environments/openhands`，不进入控制面依赖。容器仍由手写的 `container_arguments` 规定 `--network none`、`--read-only`、`--cap-drop ALL`、`no-new-privileges`、`--pids-limit 64`、`--cpus 1`、`/tmp` tmpfs 和用户 `65534:65534`。内存与 swap 都从 268435456 调整为 805306368（768 MiB，swap 与内存相同，没有额外交换空间）。原因：一次单元测试采样到入口父进程 VmRSS 峰值 283888 KiB，连同协议子进程的树峰值是 306484 KiB，都超过原来 256 MiB cgroup 的 262144 KiB。`JOB_IMAGE` 常量仍是 ADR-F40 的 Alpine digest。runner 在 `python@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f` 上构建带标签 `aether.openhands_sandbox=1.54.0` 的本地镜像。容器入口用 `openhands.sdk.Agent` 和 `Conversation`，工具是 `file_editor` 与 `terminal`（`terminal_type=subprocess`），由模型返回的编辑动作生成补丁，然后再用 `LocalWorkspace.execute_command` 跑测试协议。不使用 DockerWorkspace。容器保持 `--network none`。唯一的模型通道是挂进容器的 Unix socket，指向宿主机上的作业代理。目录和套接字先收成属主权限（0700 / 0600），再用 POSIX ACL 把目录遍历和套接字读写交给 uid 65534；ACL 写不进去就拒绝启动，不退回 0777 或 0666。容器进程的工作目录沿用镜像的 `/opt/openhands`，不再设到公开可写目录；`HOME`、`TMPDIR` 和 `XDG_CACHE_HOME` 仍指向 `/tmp` tmpfs。代理核对作业令牌后调用已有的 `ModelGateway.relay_chat`。供应商 key 只在网关进程，经 stdin 交接，不进入容器环境。没有上游时，网关的脚本化替身先用 terminal 工具执行一次固定编辑（`SCRIPTED_EDIT_COMMAND`：把 `app.py` 的 `return 0` 改成 `return 42`），收到工具结果后再返回 finish，每轮用量是 8 个输入 token 和 4 个输出 token；作业是否通过由 Agent 改过的代码决定（2026-10-17 修订，见本行末尾）。每次尝试预留 4096 micro-USD 和 4096 token。上游没有美元价格时，结算金额等于输入 token 加输出 token。没有用量（包括已确认的取消）调用 `mark_unknown`，不再按 0 结算；这取代 ADR-F37 里编码尝试按 0 结算的那一句。编码对话每次调用都新开一张 `coding_call` 子预留（金额 0，不增加账户冻结）。子票核对 `coding_jobs` 的 fencing 和 `operations.lease_until`，作废一张一次性子授权，不调用 `_admit`，也不读写 `tasks.status`。同一提示重放是 `token_rejected`。父预留里已测 token 加在途上限再加单次输出上限 256 超过 `reserved_tokens`，或调用次数达到 4，是 `budget_exceeded`。这两类拒绝发生在上游之前。会话令牌留在 scope 里的 `owner_id` 上继续有效，供同一次尝试的后续不同调用；网关不读 `projects`，避免在没有 owner 设置时被行级安全滤掉。结算仍落在父预留上，并且优先使用网关记下的 token，而不是容器自报。研究调用的 `_admit` 仍只接受 `RUNNING` 与 `VERIFYING`。`/v1/chat/completions` 忽略请求体里的 `timeout`，上游超时固定 30 秒；转发字段只有 `model`、`messages`、`tools`、`tool_choice`，`max_tokens` 由服务端写成 256。`/v1/complete` 仍读取请求体里的 timeout。协议判定前，入口用只读基线覆盖作业副本里的 `test_*.py` 并核对 sha256，删掉新增的测试文件。Agent 改过的测试不能判通过；补丁仍可以包含这次修改。作业必须处于 `STARTING`、`ACTIVE` 或 `EXPORTING`，令牌里的 `attempt_id` 等于 `fencing_token`，并且租约未到期。对不上是 `token_rejected`，发生在上游之前。重试把 fencing 加一后，旧令牌不能再调用。`WAITING_APPROVAL`、`DRAFT`、`NEEDS_REVIEW` 和 `FAILED`、`SUCCEEDED`、`CANCELLED` 都保持原状态。编码作业不把任务改成 `RUNNING`，也不把终态任务重新打开。`_bounded_logs` 里删掉的 `assert process.stdout is not None and process.stderr is not None` 原先是在 `Popen(stdout=PIPE, stderr=PIPE)` 之后收窄类型。现在两条日志都由 Docker SDK 的 `container_logs` 返回字节，空日志是 `b""`，不是缺失的管道。`logs` 显式传 `follow=False`，因为 docker-py 在 `follow is None` 时把 `follow` 设成 `stream`，取消作业读取日志会一直阻塞。没有 SDK 失败后的手写 runner。技能用例由 pydantic-evals 2.57.0 的 `Dataset.evaluate_sync` 按顺序调度；`grade`、`judge` 和 `record_evaluation` 仍在 `aether_verifier` 进程。API 请求体改为 Pydantic 模型，错误码和中文消息保持原样，合同文件没有改。API 设置和非密钥 worker 运行配置改用 pydantic-settings 2.16.0，布尔仍只认 `"1"`；验收库地址、供应商 key 和回环令牌仍直接读环境变量。轨迹密钥改用 detect-secrets 1.5.0，CI 用 Gitleaks 8.30.1（sha256 `551f6fc83ea457d62a0d98237cbad105af8d557003051f41f3e7ca7b3f2470eb`）。容器动作改用 docker 7.2.0，进程命令行改用 psutil 7.2.2。worker 角色仍是 `docker_forbidden`。CodeGraph adapter、ArtifactStore、SSRF、审批、预算账本和沙箱策略没有替换。`f27.codegraph_enabled` 改为 true，因为锁文件 `product_enabled` 已是 true，`readiness.codegraph_enabled` 已是 passed，证据是 `reports/f15_product_result.json`。`f27.live_ready` 仍是 false，`readiness.live_ready` 仍是 pending。不改 `contracts/` 和 `reference/`。`func-timeout==4.3.5` 只有 sdist，锁文件改指向仓库内由该 sdist 构建、并由 uv 记下哈希的 wheel，安装使用 `uv sync --locked --no-build`。runner 在 Docker 可用且标签镜像不存在时构建 OpenHands 镜像，安装命令同时是 `scripts/build_openhands_sandbox.py`。容器日志按流只保留尾部。`exec` 和 `kill` 走 Docker SDK。本机没有 Docker、Postgres 和 Temporal，集成测试没有跑。2026-10-16 补充：维护者用真实 DeepSeek（`deepseek-chat`）跑 `99411ce` 时，公开编码作业在有 key 时 `held_micro_usd` 停在 8192，作业却是 FINISHED，结果 `model_requests=0`、token 为 0；两个父预留 `usage_status=unknown`，两张 `coding_call` 已 `response_recorded` 但 token 为 0 且没有 `model_revision`。同一会话的研究调用有 usage。原因是沙箱别名 `coder` 被原样发给上游，DeepSeek 返回 4xx；网关把错误收成不含状态的 `upstream_error`，响应里没有可用 usage 时 `_add_measured_usage` 直接返回，`_settle` 再 `mark_unknown`，候选文件仍把作业判成 FINISHED。现在设置了 `AETHER_LIVE_MODEL_ALIAS` 时，上游 `model` 换成这个别名，准入仍核对沙箱别名；该别名进入网关进程环境，供应商 key 仍只走 stdin。客户端 `stream` 为 true 时转发 `stream` 并强制 `stream_options.include_usage` 为 true；提示哈希仍不含 `stream`。非流式 JSON 和 SSE（含用量只在最后一块、`choices` 为空的 DeepSeek 形状）都读取 `prompt_tokens`/`completion_tokens`，也接受 `input_tokens`/`output_tokens`。用量累加到父预留的 `measured_*`，写入 `model_revision`，并回写该次 `coding_call` 的 token 与 `model_revision`。4xx/5xx 记下 `upstream_http_<status>`、`upstream_status` 和通过 `^[a-z0-9_]{1,64}$` 的 `provider_error`，不保存错误正文。上游错误、响应没有可用 usage、或 `model_requests` 小于 1 且网关没有记到用量，都不能让作业变成 FINISHED。补丁以 `---` 开头时，发布的补丁是 Agent 编辑，事件 `patch_source` 为 `agent_edit`；否则是 `candidate_files`（2026-10-17 起不再要求 `edit_calls` 大于 0，见本行末尾）。网关和 worker 各打一条日志，只含作业、状态、模型别名、token 数和错误码，不含密钥和正文。不改变 `live_ready`。2026-10-16 再补充：维护者用真实 DeepSeek 跑 `310eddc` 时，两次调用约为 2751/75 和 2875/168 个 token，第三次因为父预留只有 4096 token，在已测量加上在途再加 256 处被 `budget_exceeded` 拒绝。作业仍是 FINISHED，`model_requests=0`、`edit_calls=0`、`patch_source=candidate_files`；父预留停在 `usage_status=unknown`，账户 held 4096、spent 0。原因是一次 4096 盖不住多轮约 2800 的输入，Agent 抛错后入口返回空白报告，结算又把超出预留的实测量当成未知并继续冻结。现在每次编码尝试先按可配置的单轮预留冻结。`AETHER_CODING_CALL_RESERVE_TOKENS` 默认 4096，按实测约 2800 输入再留输出余量。`AETHER_CODING_CALL_RESERVE_MICRO_USD` 未设置时与 token 数相同，因为编码结算仍是 1 micro-USD 对应 1 个 token。父预留的 `usage_detail` 记下 `call_reserve_tokens` 和 `call_reserve_micro_usd`。下一次调用若已测 token 加在途 token 加单轮预留超过父预留，就在同一事务里追加差额，同时增加 `reserved_micro_usd`、`tokens_held` 和 `held_micro_usd`，不增加 `requests_admitted`。追加后的冻结加上已花费不得超过任务账户里非验收部分的上限，也就是 `token_limit` 和 `limit_micro_usd` 乘以 `1 - verification_reserve_fraction`。超上限仍是 `budget_exceeded`，发生在上游之前，并在父预留记下 `admission_error`。没有 `call_reserve_tokens` 的预留不追加，仍按已测加在途再加 256 拒绝。调用次数达到 4 仍拒绝。已经记下的 token 即使超过预留，也用 `settle_measured` 结成 `known`：`actual_micro_usd`、`input_tokens`、`output_tokens` 写实际数量，冻结按预留额释放，花费按实际 token 计入。超出部分写入 `book_result=overrun`、`overrun_tokens`、`overrun_micro_usd` 和 `reconciliation=recorded`。这不再 `mark_unknown`，也不把冻结留在账户上。没有用量时仍 `mark_unknown`，未知账单继续冻结。普通 `settle` 仍拒绝 `actual_exceeds_reservation`。发布的 `model_requests` 是网关成功的 `coding_call` 行数：`usage_status=known`、输入加输出大于 0、没有 `upstream_error`。沙箱在异常时返回的 0 不覆盖这个数。Agent 对话没有正常结束（`agent_completed` 不是 true，包括预算拒绝后的空白报告）时作业是 FAILED。父预留有 `admission_error` 时错误码用该值，否则是 `agent_interrupted`。协议已经通过、磁盘上的文件已经改过，也不能把这次中断判成 FINISHED。~~脚本化 finish 会正常结束且 `edit_calls` 为 0：候选测试失败仍是 FAILED，候选本身已经通过时仍是 FINISHED，补丁来自 `candidate_files`。~~ 这一句已被 2026-10-17 修订取代。不改变 `live_ready`。2026-10-17 修订（R8-B P1-a/P1-b）：改了什么——编码作业判 FINISHED 现在还要求 Agent 真的改了代码：发布补丁为空、缺失或不是 `--- ` 开头的 diff 时，作业是 FAILED，错误码 `agent_no_change`，候选文件本来就能通过也不再算成功；`agent_completed` 必须显式为 true，缺失时不再按 `model_requests>=1` 推断为已结束，按 `agent_interrupted` 失败。`edit_calls` 只统计 file_editor 调用，只记日志和事件，不参与发布与判定；Agent 用终端改的文件同样作为 `agent_edit` 发布。发布补丁改在 `stop_agent_processes()` 回收全部 Agent 进程之后、恢复基线测试之前生成，后台进程在 Agent 结束后的改动也进补丁，补丁与评分的代码一致。作业 objective 从 operation 的 `normalized_arguments` 读出，以单个 `-e AETHER_OBJECTIVE=...` 参数（不经 shell）进入沙箱，入口原样作为唯一的用户消息；system prompt 是通用编码指令，不含 objective；缺少 objective 时作业以 `objective_missing` 失败，不再发送写死的 demo 提示。为什么改——维护者用真实模型跑公开编码作业时，Agent 没有改任何文件（`edit_calls=0`、补丁来自 `candidate_files`），作业却因为上传的候选文件本来就能通过测试而判 FINISHED，是一次假通过；同时沙箱发给 Agent 的总是写死的 demo 提示，与作业 objective 无关。公开验收随之改为让无上游替身或假模型真正改 `app.py`：首次尝试的测试要求 43，编辑后仍失败；重试的候选为 `return 0`、测试要求 42，由 Agent 编辑后通过。 |
| ADR-F43 | 威胁情报浏览器用固定的 Playwright 与一次性容器；Tor 和 `.onion` 默认关闭，经 Tor 的正文不能当验收证据 | 2026-10-16：R8-C。基线 `a2b9313`，变基到 `13bc096`。ADR-F41 已被采集解析占用，未合并的 PR #31 已使用 ADR-F42，本决定记为 ADR-F43。子 frame 的 document 请求直接中止，不抓取。每次访问把脱敏记录写入 audit_events，哈希链按项目内 sequence 接上一条，调用方丢掉内存列表也不会丢掉这条记录。Tor 容器启动后，浏览器要等内部链路地址上的 9050 完成 SOCKS 握手；夹具模式因为 DisableNetwork 不接受 SOCKS，改为等同一地址的 8080 接受连接。SOCKS 只绑定该内部地址，策略只接受这次的 /28 并拒绝其他来源，出口网卡不监听 9050。浏览器库固定 `playwright==1.64.0`，Chromium 修订 `1248`（`156.0.8078.4`）。镜像摘要 `mcr.microsoft.com/playwright@sha256:06a9939e57531807f8d5fd76ce44b53165ffb7d7501d87ab10e285c20b1e971f` 来自当天的 manifest list。`infra/threat-browser.Dockerfile` 以这个摘要为 `FROM`，并安装同一个 Python 包。既有的 `render_controlled_page` 仍执行页面脚本，因为 `test_playwright_profile_renders_javascript` 要求正文变成 `rendered-3`；威胁情报访问是另一条路径，关闭 JavaScript 和下载。SSRF 仍调用 `assert_public_target`，没有改那套地址判断。每次文档请求连到已经校验过的地址，有 IPv4 时用 IPv4。HTTP 的 Host 和 TLS 的 SNI 仍用原来的主机名，Chromium 不再自己拨号。重定向每一跳重新授权。附件在交给页面前拒绝。原先记录的 `osminogin/tor-simple` 不再使用。Tor 改由 `infra/tor.Dockerfile` 构建：基座摘要 `debian@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f`（`trixie-slim`，2026-10-16 的 manifest index），从 `deb.torproject.org` 安装签名包 `0.4.9.14-1~d13.trixie+1`，构建时用 gpg 核对官方指纹 `A3C4F0F979CAA22CDBA8F512EE8CBC9E886DDD89` 和 `InRelease`。apt 的 sqv 会拒绝这把密钥里的 SHA-1 绑定，所以安装不把该源写进 apt，而是校验签名后再安装这个 deb。进程用户是 `debian-tor`。`policy-rc.d` 退出 101，构建期间不启动 Tor。测试容器设置 `DisableNetwork 1` 和本地夹具页，不连接公网。这个开关下 Tor 不接受 SOCKS，测试确认主进程是 tor。夹具页只在 Tor 容器内通过 TLS 提供。监听套接字保持明文，每个已接受的连接再握手，明文探测不会拆掉监听。证书在夹具容器启动时生成，私钥只出现在 debian-tor 的 0700 目录，不写入镜像层。浏览器把 `AETHER_FIXTURE_CA` 的 base64 解码成 PEM 文本再交给 `load_verify_locations`；该参数的字节形式是 DER，不能直接传入 PEM 字节。runner 只运行本进程环境 `AETHER_THREAT_BROWSER_IMAGE` 与 `AETHER_TOR_IMAGE` 里的镜像，并且只有 runner 自己打开 `AETHER_INGEST_ALLOW_FIXTURES` 时才接受夹具。构建文件放在 /var/lib/aether-tor-build，安装结束后删除。`config/threat_browser.yaml` 里浏览器、Tor、`.onion` 和 `owner_confirmed` 都是 false。请求里的开关不能超过这份配置。Tor 或 `.onion` 还要所有者和配置双方的 `owner_confirmed`。开关以 config/threat_browser.yaml 为准。所有者审批来自账本里已有的 approvals 行，verifier 只核对，worker 写入 operations 后把 consumed_by_operation_id 填上，同一条审批不能再用。外部自己构造的许可对象会被拒绝。Docker 只由 runner 进程执行。明文访问只创建出口网络，不启动 Tor。Tor 打开时浏览器只接入 `--internal` 网络，Tor 容器再接入出口网络。浏览器带 `--rm`，不发布端口，不挂 Docker socket。浏览器容器镜像带上 aether_worker 与 aether_domain。进程内 Playwright 只在夹具开关打开时可用。代理只允许 `socks5://aether-tor:9050`，测试夹具只允许 `socks5://127.0.0.1`。`HTTP_PROXY` 不传给浏览器。`.onion` 不调用明文 DNS。正文标成 `clearnet`、`tor` 或 `onion`，`admissible_evidence` 恒为 false；Tor 和 `.onion` 不能交给 verifier 当证据。用途只接受 `threat_intelligence`。`trade`、`illegal_download`、`contact_seller` 以及购买、非法下载、联系卖家这些目标直接拒绝。容器运行时如果报告的浏览器版本不是上述修订，就拒绝，并且不会改走本机 Playwright。本工作区没有 Docker daemon。需要 Docker 的集成测试在 CI 的 integration 作业里构建并启动这两个容器；`AETHER_REQUIRE_SERVICES=1` 且没有 Docker 时该测试失败，不跳过。进程内攻击回归仍直接调用同一个 `read_page`。容器里的 Chromium 因 `--cap-drop ALL` 无法使用自身沙箱，所以只在容器环境变量 `AETHER_BROWSER_NO_SANDBOX=1` 时加上 `--no-sandbox`。不改变 `live_ready`，不改 `contracts/` 和 `reference/`。 |
实施时确需改架构：追加日期、具体失败证据、最小变更、风险、迁移与新增/修订测试。不因个人偏好换栈；不得删除失败记录。


---

# 上游依据与证据范围

本轮2026-10-15检查以下官方仓库/文档。只用于确认架构选用能力；没有在这里替用户安装完整上游服务、锁定发布版本或运行性能对照。所有AETHER限额/目录/接口/选择均是本项目设计，不是上游性能保证。

## S01 · Temporal Python SDK
https://github.com/temporalio/sdk-python

确认范围：Workflow/Activity/client/testing/重放；当前branch文档不等于PyPI版本已验证

## S02 · Temporal activity definition
https://docs.temporal.io/activity-definition

确认范围：Activity副作用和幂等性；不保证任意远端操作恰好一次

## S03 · CodeGraph README
https://github.com/colbymchenry/codegraph

确认范围：默认explore工具、CLI/程序接口、telemetry off；运行时仍须真实探针

## S04 · OpenHands SDK
https://github.com/OpenHands/software-agent-sdk

确认范围：四个组件目录、Python/REST接口、隔离workspace选项

## S05 · LiteLLM
https://github.com/BerriAI/litellm

确认范围：模型适配接口；不选用其其他harness能力

## S06 · pgvector
https://github.com/pgvector/pgvector

确认范围：Postgres向量相似度与过滤注意事项

## S07 · BM25S
https://github.com/xhluca/bm25s

确认范围：BM25检索及保存/加载

## S08 · FlagEmbedding
https://github.com/FlagOpen/FlagEmbedding

确认范围：检索/重排模型工具

## S09 · MCP Python SDK
https://github.com/modelcontextprotocol/python-sdk

确认范围：官方工具协议客户端/服务端SDK

## S10 · Agent Skills
https://github.com/agentskills/agentskills

确认范围：SKILL.md规范

## S11 · Pydantic Evals
https://pydantic.dev/docs/ai/evals/evals/

确认范围：独立评测能力，不要求使用Agent运行时

## S12 · Syft
https://github.com/anchore/syft

确认范围：镜像和文件系统SBOM

## S13 · OSV Scanner
https://github.com/google/osv-scanner

确认范围：漏洞扫描，具体版本离线支持需探针

## S14 · Codex AGENTS.md
https://developers.openai.com/codex/agent-configuration/agents-md

确认范围：项目级指令发现/优先级/长度限制

## S15 · Codex best practices
https://developers.openai.com/codex/learn/best-practices

确认范围：任务计划、上下文控制和实际验证

## 提供的本地资料
已读取并比对V1.2设计/合同/任务、V1.3增量与DeepSeek总纲。它们完整保存在archive。DeepSeek是研究材料而非授权指令，不继承其中未经本轮验证的市场数字或许可证结论。
