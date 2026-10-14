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
