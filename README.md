# AETHER V1.4

AETHER 是本地/私有部署、面向一个所有者的可持续工作 Agent 工作台。用户可以让它研究资料、修改授权代码仓库、跟踪明确主题；能看见实际过程、审批、成本、证据、失败与恢复。经验以可删除记忆和经过评测的技能保留下来。

V1 的初始发布是单账户、多项目。研究资料、搜索索引、偏好和执行记录由本机控制。它以技术资料研究为主要知识场景，并可在隔离环境执行代码任务。是否优于 Hermes 留给对照实验。完整定义见 [`docs/00_FREEZE.md`](docs/00_FREEZE.md)、[`docs/01_product.md`](docs/01_product.md) 和阅读版 [`MASTER_SPEC.md`](MASTER_SPEC.md)。

当前交付矩阵以 [`STATUS.json`](STATUS.json) 为准（`as_of` 2026-10-15，`main` 合并提交 `8eade30`）。

## 架构

控制面是一个 Python 单体仓库。API、领域模块和 worker 共用源码，按进程、数据库角色和网络分权。权威划分：

| 状态 | 权威 |
|---|---|
| 执行进度、DAG、等待、取消、重试 | Temporal Workflow history |
| 目标、合同、权限、审批、成本账本、知识版本 | PostgreSQL |
| 某次外部操作是否发生 | 外部系统与 operation ledger 的对账 |
| 字节产物 | 按项目隔离的内容寻址 ArtifactStore |
| 编码会话内的临时执行 | 有界 OpenHands job |

逻辑分层：

```text
Next.js 控制台 ── HTTPS / REST / SSE
FastAPI API ── PostgreSQL（领域、命令、操作、投影）
        │ durable command outbox
Temporal ── Task / Research / Watch / SkillEvaluation workflow
        │ activities（全部非确定性 I/O）
Worker
  ├─ 有界 typed turn loop 与 DAG
  ├─ BM25S 快照 + pgvector + BGE-M3 / BGE reranker
  ├─ 可信网关（模型、已批准工具、成本与能力）
  ├─ Runner：隔离的 OpenHands 编码作业
  │    └─ CodeGraph：已启用的只读仓库图谱，限定在获准快照内
  └─ 独立验证器
```

Workflow 代码不做网络、数据库或文件系统 I/O。副作用走 activities 和可信服务。模型适配只使用网关里的 LiteLLM。UI 经 REST 与可重放的 SSE 访问 API，直接调用模型、OpenHands Agent Server 或 Temporal 管理接口不在这条路径上。目录和接口见 [`docs/02_architecture.md`](docs/02_architecture.md)、[`docs/03_repository_map.md`](docs/03_repository_map.md)。

## 安全模型

细则在 [`docs/06_security.md`](docs/06_security.md)、[`docs/09_data.md`](docs/09_data.md)、[`docs/04_execution.md`](docs/04_execution.md) 和 [`docs/DECISIONS.md`](docs/DECISIONS.md)。

### 单所有者

V1 只建立一个所有者。数据库约束 `one_owner_v1` 使第二次创建所有者失败。启动绑定 loopback，经本机一次性 enrollment 登录，没有默认公开密码。会话使用 HttpOnly、SameSite cookie；改变状态的请求核对 CSRF 与 Origin。`/v1/admin/*` 依赖这个单所有者不变量：已登录会话就是该所有者。非 loopback 绑定同时要求 `AETHER_ALLOW_NON_LOOPBACK=1`、`AETHER_COOKIE_SECURE=1`，以及 TLS 证书和私钥文件，否则进程拒绝启动。

### 数据库行级隔离

业务表带 `project_id`，引用使用 `(project_id, id)` 复合外键。应用层鉴权与 PostgreSQL 行级安全同时生效。每个事务用 `set_config('aether.owner_id', ..., true)` 和 `set_config('aether.project_id', ..., true)` 设置范围，第三个参数使设置只在当前事务有效。`aether_api`、`aether_worker`、`aether_verifier`、`aether_dispatcher` 都是 `NOSUPERUSER NOBYPASSRLS`。`packages/storage/aether_storage/db.py` 的 `Database` 使用 SQLAlchemy `NullPool`：每个事务单独连接，提交或回滚后关闭，避免连接池把上一个 owner 或 project 留在同一条物理连接上。API 角色不能写验证结果、技能评测和任务的最终成功状态。

### 离线容器评分器

独立验证器核对不可变合同、候选清单哈希和可信套件哈希，然后在新的评分容器里只跑绑定套件。容器是 `aether-grader:local`，运行参数包含 `--network none`、`--read-only`、`no-new-privileges` 和 pid 限制；工作目录以只读方式挂入。评分进程使用 `python -I`。子进程先导入 pytest，再把 `/work/src` 加到 `sys.path` 末尾。pytest 返回后把通过数和退出码写进父进程事先打开的管道，然后 `os._exit`。父进程只认这根管道。容器标准输出里的隔离结果以评分器写出的第一行为准。候选顶层的 `pytest.py` 和 `sitecustomize.py` 直接失败。Actor 不能写 `verification_results`，也不能用自己的日志充当通过。

### 删除是 tombstone

删除先写 tombstone，并增加项目的 `visibility_epoch`。BM25 generation 退休，已删 chunk 的向量被清空，推荐特征快照记为失效。搜索缓存同时绑定 visibility epoch 和索引代。晚到的向量和记忆文本写入必须看见同一个 epoch，而且该行还没有 `deleted_at`。这次删除不清除备份和 Temporal 历史。见 ADR-F16。

## 安装与运行

控制面是 Python 3.13，前端是 Node 24，依赖服务是 PostgreSQL 17（pgvector）和 Temporal。版本与镜像摘要在 `uv.lock`、`apps/web/pnpm-lock.yaml` 和 [`infra/compose.yaml`](infra/compose.yaml)。

```sh
uv sync --locked --no-build
```

Compose 只发布到 `127.0.0.1`。其中的 Temporal 命令是 `server start-dev`，用于开发；发布要用持久服务配置，见 [`docs/10_operations.md`](docs/10_operations.md)。

```sh
export AETHER_POSTGRES_USER=aether
export AETHER_POSTGRES_PASSWORD='生成本机口令，不要写入仓库'
docker compose -f infra/compose.yaml up
```

库和角色由 `aether_storage.bootstrap.ensure_database` 创建。第一次运行时它为四个角色生成口令并返回连接串，口令不进 Git。下面这条会把带口令的 URL 打到终端，用完即从终端记录中去掉：

```sh
export AETHER_ADMIN_DATABASE_URL='postgresql://postgres:口令@127.0.0.1:5432/postgres'
uv run --locked --no-build python -c 'import json, os; from aether_storage.bootstrap import ensure_database; print(json.dumps(ensure_database(os.environ["AETHER_ADMIN_DATABASE_URL"]), indent=2))'
```

升级时可以重跑同一条命令补齐表、授权和迁移标记。已存在的角色保持原口令，返回的字典里不会有它们，正在运行的 API 和 worker 不受影响。确实要换口令时才显式轮换；轮换会让所有用旧口令连接的进程立即断库，要先停服务，换上新 URL 再启动：

```sh
uv run --locked --no-build python -c 'import json, os; from aether_storage.bootstrap import ensure_database; print(json.dumps(ensure_database(os.environ["AETHER_ADMIN_DATABASE_URL"], rotate_passwords=True), indent=2))'
```

把返回的 `aether_api` URL 设为 `AETHER_DATABASE_URL`。产品配置名见 [`.env.example`](.env.example)，示例里的值是空的：

| 变量 | 作用 |
|---|---|
| `AETHER_BIND_HOST` | API 绑定地址，默认 `127.0.0.1` |
| `AETHER_DATABASE_URL` | API 使用的 `aether_api` 连接串 |
| `AETHER_ADMIN_DATABASE_URL` | 建库、`doctor` 和集成测试用的管理员连接串 |
| `AETHER_TEMPORAL_ADDRESS` | 例如 `127.0.0.1:7233` |
| `AETHER_ARTIFACT_ROOT` | 产物目录，默认 `data/artifacts` |
| `AETHER_BOOTSTRAP_PATH` | 引导文件，默认 `secrets/bootstrap.json`，必须留在 `secrets/` |
| `AETHER_ALLOW_LIVE` | 示例里是 `false` |
| `AETHER_MODEL_API_KEY` | [`config/models.example.yaml`](config/models.example.yaml) 指向的供应商密钥环境变量名 |

Live 探针和 F26 验收另认三个变量，缺任何一个就记 `blocked`：

```sh
export AETHER_LIVE_MODEL_BASE=https://api.deepseek.com/v1
export AETHER_LIVE_MODEL_ALIAS=deepseek-chat
export AETHER_LIVE_MODEL_KEY='只放在本机环境里'
```

上面是 DeepSeek 的 OpenAI 兼容根地址和模型名。密钥、数据库口令和引导明文只留在本机。`.env`、`secrets/` 已被忽略；不要把它们提交进 Git，也不要写入日志或结果 JSON。

API 默认监听 `127.0.0.1:8000`：

```sh
uv run --locked --no-build python -m aether_api.main
```

Worker 是同一个 Python 源码里的另一个进程，不是第二套调度器。它连接已有的 Temporal，只把任务命令投进 `DurableTaskWorkflow`，其余命令按种类交给已经存在的应用函数：

| `command_outbox.kind` | 处理 |
|---|---|
| `start`、`approve_goal`、`pause`、`resume`、`cancel` | `aether_dispatcher` 投递到 Temporal。`start` 由 worker 活动写入任务产物，再用 `aether_verifier` 记一条独立验证 |
| `source.*`、`ingestion.create`、`feedback.record`、`skill.import`、`skill.deploy`、`watch.*`、`connector.register`、`coding.create` | `aether_worker` 调用 `apply_console_commands` |
| `evaluation.create`、`skill.evaluate` | `aether_verifier` 调用 `apply_verifier_commands` |
| `memory.*` | `aether_worker` 调用 `apply_memory_commands` |

`deliver_pending` 不会领取控制台、记忆或验证命令。`start` 先把任务标成 `RUNNING`，再读取本项目已上传的文本，用明确标记的离线脚本提供者摘录资料，然后由 `aether_verifier` 重新读取磁盘上的候选和权威合同。报告分开记录流程是否跑完、字节是否完整、目标是否被摘录覆盖。三项都通过才是 `PASS`，任务才进入 `SUCCEEDED`。字节或合同对不上是 `FAILED`。没有可读取的资料，或者这个离线摘录回答不了目标（例如计算题），是 `NEEDS_REVIEW`，不会标成 `SUCCEEDED`。这不是 live 模型。

`pause`、`resume`、`cancel` 会改变任务状态。接口先写下 `resume_state`，worker 再按允许的迁移写成 `PAUSED`、`RUNNING` 或 `CANCELLED`。暂停之后不会读资料、不会写回答；恢复后才执行。取消不是回滚。相同内容可以共用一条产物记录，每个任务通过 `task_artifacts` 单独引用，不会把第一条任务的 `task_id` 改掉。

停掉进程再启动时，已经完成的活动不会再执行一次；Temporal 按 `command_id` 去重。验证缓存只命中同一次观察：候选、权威合同、验收版本和来源快照都要一致，旧的 PASS 不会盖住后来的损坏或删除。编码作业仍走 `coding.create`，由同一个进程调用已有的授权作业写入，不另起调度器。

编码作业要先有带标签 `aether.openhands_sandbox=1.54.0` 的本地镜像。Docker 可用时，runner 发现这个标签还不存在就会构建。启动前也可以自己执行：

```sh
uv run --locked --no-build python scripts/build_openhands_sandbox.py
```

基座是 `python@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f`。`JOB_IMAGE` 常量仍是 Alpine digest。容器里的 OpenHands Agent 通过挂载的 Unix socket 把模型请求交给网关，供应商 key 留在网关进程。该套接字只对属主和 uid 65534 开放。每次模型调用单独过预算准入并消耗一张子票；重放或超预算在到达供应商之前拒绝。聊天补全忽略请求体里的 timeout，只转发白名单字段。协议判定前用作业创建时的测试文件覆盖副本并核对哈希。内存与 swap 上限是 805306368 字节。

把 `ensure_database` 返回的三个角色 URL 设进环境。产物目录必须和 API 的 `AETHER_ARTIFACT_ROOT` 是同一个路径。

```sh
export AETHER_WORKER_DATABASE_URL='postgresql://aether_worker:…@127.0.0.1:5432/aether'
export AETHER_VERIFIER_DATABASE_URL='postgresql://aether_verifier:…@127.0.0.1:5432/aether'
export AETHER_DISPATCHER_DATABASE_URL='postgresql://aether_dispatcher:…@127.0.0.1:5432/aether'
export AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233
export AETHER_ARTIFACT_ROOT=data/artifacts
export AETHER_TASK_QUEUE=aether-tasks
uv run --locked --no-build python -m aether_worker
```

入口读到验收库地址和 `AETHER_LIVE_MODEL_KEY` 之后，会把它们交给独立进程，再从 worker 环境里删除并重新执行。重新执行后的 worker 不持有验收库口令、供应商 key，也不打开 Docker 套接字。直接构造 `WorkerEndpoints` 的进程内调用仍使用传入的地址和 key。已是 `AETHER_PROCESS_ROLE=worker` 却还带着这两项环境变量时，进程退出，错误信息只列出变量名。

`AETHER_INTAKE_PAUSE_SECONDS` 默认是 `0`，正式启动不要设置。它是 `DurableTaskWorkflow` 的启动参数：大于 0 时，任务记成 `RUNNING` 之后、读取资料之前，按这个秒数做一次 durable sleep。数值写进 Workflow history，重启后仍用同一次启动时的秒数。进程测试把它设成 `4`，才能在 `RUNNING` 时停掉 worker。控制信号可以提前结束这次等待。`4ba3b2f` 之前的历史没有这条补丁标记，回放时仍走原来的 sleep。

研究路径默认调用 live 模型。三个变量和短探针相同：`AETHER_LIVE_MODEL_BASE`、`AETHER_LIVE_MODEL_ALIAS`、`AETHER_LIVE_MODEL_KEY`。缺任何一个，任务进入 `NEEDS_REVIEW`，原因是没有配置供应商，不会改用脚本回答。离线测试要显式设置 `AETHER_RESEARCH_PROVIDER=scripted`。调用前按任务预算预留 token、次数、费用和墙钟；超限则任务失败，`resume_state` 为 `budget_exhausted`，并且不会发出这次请求。`allowed_source_ids` 省略时读取项目里未绑定的文本；空列表表示没有来源；列表里有不存在或不可读的 id 时整次任务进入 `NEEDS_REVIEW`，不会改读其他资料。

进程打出 `aether-worker-ready` 后开始轮询。`SIGTERM` 或 `SIGINT` 让当前这一轮结束后退出。队列默认 `aether-tasks`，测试可以改 `AETHER_TASK_QUEUE`。

引导密钥由 `issue_bootstrap_secret` 写入 `secrets/bootstrap.json`（文件里是哈希）。终端只显示一次明文。随后向 loopback 的 `POST /v1/auth/bootstrap` 提交 `bootstrap_secret`、`display_name` 和至少 12 个字符的 `passphrase`。

```sh
uv run --locked --no-build python -c 'from pathlib import Path; from aether_api.auth_service import issue_bootstrap_secret; print(issue_bootstrap_secret(Path("secrets/bootstrap.json")))'
```

控制台：

```sh
corepack enable
corepack prepare pnpm@10.18.2 --activate
cd apps/web
pnpm install --frozen-lockfile
pnpm dev
```

`pnpm dev` 绑定 `127.0.0.1:3000`。`AETHER_API_BASE` 与 `NEXT_PUBLIC_AETHER_API_BASE` 默认 `http://127.0.0.1:8000`。

依赖探针：

```sh
uv run --locked --no-build python scripts/doctor.py --require-services
```

## 测试

单元测试不要求数据库。集成测试要求 `AETHER_REQUIRE_SERVICES=1`，并设置 `AETHER_ADMIN_DATABASE_URL` 与 `AETHER_TEMPORAL_ADDRESS`。CI 使用与下面相同的锁定解释器：

```sh
uv run --locked --no-build pytest -q tests/unit
AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/integration
```

Makefile 里已有的目标：`check-pack`、`test-reference`、`doctor`、`test-unit`、`test-integration`、`test-security`、`acceptance-report`、`demo-live`。`make backup` 和 `make restore` 只打印所需参数，不执行备份或恢复。`make demo-live` 跑的是短探针 `scripts/run_f26_live.py`，短探针的 `probed` 不是完整验收。

### `scripts/run_f26_acceptance.py`

完整 live 验收需要本机 Docker、uv，以及两个独立环境。OpenHands 四个包钉在 1.54.0。ML 环境提供 `FlagEmbedding`，第一次会下载 `BAAI/bge-m3`（revision `5617a9f61b028005a4858fdac845db406aefb181`）。独立测试和网络隔离使用摘要固定的 `python@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4`（索引摘要，容器内 CPython 3.12.15）。评分依赖用 `pip install --require-hashes --only-binary=:all:` 安装 `pytest==8.3.5`，不使用可变标签。

```sh
cd environments/openhands && uv sync --locked --no-build
cd ../ml && uv sync --frozen
docker pull python@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4
cd ../..
export AETHER_LIVE_MODEL_BASE=https://api.deepseek.com/v1
export AETHER_LIVE_MODEL_ALIAS=deepseek-chat
export AETHER_LIVE_MODEL_KEY='只放在本机环境里'
uv run --locked --no-build python scripts/run_f26_acceptance.py --out reports/f26_acceptance_result.json
```

`--out` 只接受相对路径 `reports/f26_acceptance_result.json`。退出码：`probed` 为 0，`failed` 为 1，`blocked` 为 2，`incomplete` 为 3。硬上限是 8 次补全、累计 20000 token、单次输出最多 512 token。供应商没有返回费用时 `cost_status` 为 `unknown`。验收步骤、引用规则和已记录结果见 [`reports/f26_criteria.md`](reports/f26_criteria.md)。

### `scripts/run_live_task_check.py`

同一组模型变量还可以跑一条真实研究任务。它走公开 HTTP 和 `python -m aether_worker`，提供者是 live。缺变量时退出码是 2，结果是 `blocked`，不会假装成功。key 不进命令行，不进结果文件。

```sh
export AETHER_DATABASE_URL='postgresql://aether_api:…@127.0.0.1:5432/aether'
export AETHER_WORKER_DATABASE_URL='postgresql://aether_worker:…@127.0.0.1:5432/aether'
export AETHER_VERIFIER_DATABASE_URL='postgresql://aether_verifier:…@127.0.0.1:5432/aether'
export AETHER_DISPATCHER_DATABASE_URL='postgresql://aether_dispatcher:…@127.0.0.1:5432/aether'
export AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233
export AETHER_LIVE_MODEL_BASE=https://api.deepseek.com/v1
export AETHER_LIVE_MODEL_ALIAS=deepseek-chat
export AETHER_LIVE_MODEL_KEY='只放在本机环境里'
uv run --locked --no-build python scripts/run_live_task_check.py --out reports/live_task_check.json
```

`--out` 只接受 `reports/live_task_check.json`。退出码和 F26 一样：`probed` 是 0，`failed` 是 1，`blocked` 是 2，`incomplete` 是 3。`probed` 只表示这次调用跑完，不表示 `live_ready`。上传接口返回 `artifact_id`。失败时结果里有 `step`、`error_type` 和去掉密钥后的 `error_message`。监督者在 head `7eb911b` 上用真实 key 跑过一次：退出码 0，`status=probed`，任务 `SUCCEEDED`，1 次 `research.call`，1 次验证，`cost_status=unknown`。原样结果在 [`reports/live_task_check.json`](reports/live_task_check.json)，文件 sha256 是 `085b8b3224c305fd5ad0fece2069e939237d7ed04a8fc262862d61943fb8fc91`。结果里的 `live_ready` 是 `false`。这次检查不改交付矩阵里的 `live_ready`。第四轮之后，只重叠十二个字不再判目标完成；显式来源有一份不可读会停在 `NEEDS_REVIEW`。上面两条命令仍然可以重跑。

## 当前状态

F00–F27 的 `status` 都是 `done`。`readiness`：

| 项 | 值 |
|---|---|
| `kernel_engineering` | `passed` |
| `product_engineering` | `passed` |
| `live_ready` | `passed` |
| `codegraph_enabled` | `passed` |
| `comparative_evidence` | `not_run` |

`f27.live_ready` 仍是 `false`。`f27.codegraph_enabled` 改为 `true`：`config/codegraph.lock.json` 的 `product_enabled` 已是 true，`readiness.codegraph_enabled` 已是 `passed`，证据是 `reports/f15_product_result.json`。`readiness.live_ready` 仍是 pending。

F26 最近一次完整验收在 head `d694f89`，退出码 0，`status=probed`。协议、现场 BGE 混合检索、OpenHands 1.54.0 补丁和 Linux 隔离都通过。3 次调用，合计 1303 token。比较报告 sha256 是 `710c3053d6be9d52c18986aa3e13adfcd9d005b8ebe664ef69b3a183221b44fe`，completion 484 token。`semantic_support` 仍是 `not_judged`：引用写齐不等于已经判断比较内容成立。`cost_status` 仍是 `unknown`。原样结果在 [`reports/f26_acceptance_result.json`](reports/f26_acceptance_result.json)，文件 sha256 是 `52ffd4b2da3a463ddea421db53098b4aaa441d0e7d87d3fc9f900af1d325db38`。文件里的 `live_ready` 仍是 `false`，脚本不自证；交付矩阵按产品工程加 F26 记为 `passed`。更早的 `cb22fbc` 那次也是退出码 0，比较报告当时是 488 token。同一头上的 `scripts/run_live_task_check.py` 在创建任务之前失败：它读取上传响应的 `id`，接口返回的是 `artifact_id`，模型没有被调用。脚本现在读取 `artifact_id`。修好之后，监督者在 head `7eb911b` 上用真实 key 跑通一次研究任务，见上面的现场检查记录。费用仍是 unknown。这次成功不改 `live_ready`。

CodeGraph 已启用。所有者打开的是已安装锁 [`config/codegraph.lock.json`](config/codegraph.lock.json)（`product_enabled: true`，CLI `@colbymchenry/codegraph@1.6.2`）。[`config/codegraph.example.yaml`](config/codegraph.example.yaml) 和 `config/feature_matrix.yaml` 仍然关闭，新检出不会自动打开。证据在 [`reports/f15_product_result.json`](reports/f15_product_result.json)：`status` 为 `passed`，索引 162 个文件、2674 个节点、7081 条边，库 8892416 字节，冷索引约 2.00 秒，状态 `complete`。调用关系与 AST 一致，改快照后查询返回 `stale_snapshot`，容器带 `--network none`，遥测关闭。复现命令见 [`reports/f15_criteria.md`](reports/f15_criteria.md)：

```sh
uv run --locked --no-build python scripts/run_codegraph_product.py --out reports/f15_product_result.json
```

该命令需要本机 Docker，以及能 `npm pack` 到 registry.npmjs.org 的网络。图谱建议不能改可信套件。

Hermes 四组在 head `fcceba5` 上用 live key 探测过一次，原样结果在 [`reports/hermes_compare_result.json`](reports/hermes_compare_result.json)。每组 n=1，都答对，Wilson 95% 区间约 0.21–1.0，不足以判断领先。`comparative_evidence` 仍是 `not_run`，`superior` 仍是 null，费用 unknown。请求别名是 `deepseek-chat`，上游返回的 model 是 `deepseek-flash`。F25 的漏洞库仍是 `offline_not_run`。交接记录在 [`reports/HANDOFF.md`](reports/HANDOFF.md)。

## 文档

维护源是 `docs/`。`MASTER_SPEC.md` 由这些章节生成。

| 文档 | 内容 |
|---|---|
| [`docs/00_FREEZE.md`](docs/00_FREEZE.md) | 最终裁决、范围、选型 |
| [`docs/01_product.md`](docs/01_product.md) | 产品范围与完成定义 |
| [`docs/02_architecture.md`](docs/02_architecture.md) | 架构与权威 |
| [`docs/03_repository_map.md`](docs/03_repository_map.md) | 上游取用与边界 |
| [`docs/04_execution.md`](docs/04_execution.md) | 执行、编码作业、独立验证 |
| [`docs/05_knowledge.md`](docs/05_knowledge.md) | 采集、检索、记忆 |
| [`docs/06_security.md`](docs/06_security.md) | 权限、隔离、注入防线 |
| [`docs/07_learning_evaluation.md`](docs/07_learning_evaluation.md) | 技能评测与对照 |
| [`docs/08_api_ui.md`](docs/08_api_ui.md) | API 与控制台 |
| [`docs/09_data.md`](docs/09_data.md) | 数据模型、RLS、保留 |
| [`docs/10_operations.md`](docs/10_operations.md) | 运行模式、模型、预算 |
| [`docs/11_delivery.md`](docs/11_delivery.md) | 施工顺序与就绪定义 |
| [`docs/12_integration_details.md`](docs/12_integration_details.md) | 集成接缝 |
| [`docs/13_codegraph.md`](docs/13_codegraph.md) | CodeGraph 边界 |
| [`docs/14_BOUNDARIES_AND_FAILURES.md`](docs/14_BOUNDARIES_AND_FAILURES.md) | 跨组件故障 |
| [`docs/16_MERGE_EXISTING.md`](docs/16_MERGE_EXISTING.md) | 合并已有代码 |
| [`docs/DECISIONS.md`](docs/DECISIONS.md) | 冻结决策 |
| [`docs/SOURCES.md`](docs/SOURCES.md) | 上游依据 |
| [`tasks/backlog.yaml`](tasks/backlog.yaml) | 任务队列 |
