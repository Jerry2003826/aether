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

开发进程里的 worker 是 `uv run --locked --no-build python -m aether_worker`。它使用已有 Temporal 和 `command_outbox`，不另起调度器。worker 没有自己的镜像，直接在主机上运行；生成候选补丁要用 PATH 上的 `git`（`git diff --no-index --binary`），所以 git 是 worker 主机的前置条件。缺少 git 时 worker 启动时打一条 `worker_git_missing` 警告并照常启动；补丁记 `patch_unavailable`，作业保留原来的终态与错误码（见 ADR-F44）。环境变量、命令种类和停启方式见 README「安装与运行」。

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

**编码评分：候选文件超过 8 MiB**：评分进程每步复制的候选非评分文件总量超过 8 MiB 时不跑任何测试，测试结果（`aether.coding_test_result`）里只有一条 `name=suite`、`verdict=FAIL`、`detail=candidate_files_too_large` 的用例，（评分进程 stderr 里的文件名清单不回传到作业日志）。排查：在任务基线与发布补丁里找出大文件（如 `find . -type f -size +512k -not -path "./tests/*" -not -path "./.git/*"`，再看补丁新增的数据、生成物或依赖目录），从任务基线移除或改放评分目录；这不是候选代码的测试失败，不要按普通 test_failed 处理。

**编码容器残留**：每个编码容器带 `aether.role=coding` 与 `aether.deployment=<AETHER_DEPLOYMENT_ID，未设为 local>` 标签。同一 Docker 守护进程上跑多套部署时，各自必须设置不同的 `AETHER_DEPLOYMENT_ID`。worker 启动时只清理本部署的残留容器：已停止超过 10 分钟的，或运行时间超过编码沙箱最长运行时长再加 10 分钟的；年轻容器与其他部署的容器不动。退出后 `rm -f` 失败会重试一次，仍失败时记 `coding_container_left`，等下次启动清理。委托模式（runner 管 Docker）下 worker 不清理，记 `coding_container_sweep skipped=delegated`。手动排查：`docker ps -a --filter label=aether.role=coding --filter label=aether.deployment=<id>`。入口意外异常的 stderr 只有 `entry_failed <类型名>`，不含异常消息；存储前日志尾部经 `secret_scan.redact_text` 整段脱敏。

## 7. 禁止默认危险的运维行为

不使用`docker compose down -v`作为普通重启/升级；不自动清空DB；不把任何admin端口映射0.0.0.0；不在前端bundle注入provider key。备份恢复、数据导出、真实PR发布需要显式命令和授权。安装脚本不改机器范围防火墙/安全设置；需要这些权限的部署步骤先生成配置供owner执行/批准，并把相关验收标清。

**备份恢复与 Temporal**：备份只含 AETHER 数据库与产物目录，不含 Temporal 的工作流历史。恢复点之后 Temporal 里发生的事不会随数据库回退：备份后已经完成的任务在恢复时保留当前状态（结果里列在 `kept_live_tasks`），不会被改回排队；恢复把所有 PENDING/DELIVERING 命令改成 STOPPED，不自动重新派发，由所有者核对后重新提交。在用库上恢复前先停 worker 与派发进程。重建到空库时，Temporal 若也是新的，旧工作流不存在，未完成的任务需要重新派发；Temporal 若仍是原来的，工作流仍按原历史运行。

可重复的崩溃演练是 `scripts/run_crash_recovery_drill.py`。它使用名称以 `aether-drill-` 开头的 Temporal 容器和该容器自己的 sqlite，不停止共享开发服务。顺序是备份 Postgres 与 sqlite、只删除演练标记指向的 sqlite、按 `restore-aether` 合并恢复、再读取任务状态和 Temporal 工作流。两边不一致时报告 `relation=diverged`、`action=human_review`，不改写终态。恢复仍是合并，不是把 Temporal 历史回退到备份点。演练写入的备份、sqlite 和报告必须位于当前工作目录内。工作流编号只接受字母、数字、点、下划线和连字符。Postgres 与 sqlite 在容器停止时一起复制。确认值不是 `restore-aether` 或备份哈希不符时，不停止容器，也不删除 sqlite。容器以宿主机 uid 运行，并设置 `USER=temporal` 与 `HOME=/state`，这样镜像 passwd 里没有这个 uid 时仍能创建 sqlite。
