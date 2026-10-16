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
| ADR-F21 | 一次逻辑研究调用只派发一次；预算按将发出的文本估算；短引用不是目标完成 | 2026-10-15：审计 R4-01 至 R4-05。`active_seconds` 是从 `tasks.created_at` 起的墙钟，排队和暂停都算在内。调用前用本地 cl100k 估算输入并加上输出上限，不再用固定 512 token 或 0.05 美元代表任意长请求。已知 token 在价格未知时入账，价格未知只继续冻结金额。回报超过预留时记下 reported 值，任务不成功。已派发但没有回执则停止对账，不自动再打供应商。显式来源有一份不可读就整次 `NEEDS_REVIEW`。十二字重叠只记引用，不单独判 `SUCCEEDED`。验收版本改为 `goal-artifact-5`。阻塞调用放到线程里，不占用 Temporal 的事件循环。不改变 `live_ready` |
| ADR-F22 | 数字必须带着单位和主语；引用失败不能成功；派发前要有费用上限和聊天包装；控制命令在调用期间生效 | 2026-10-15：PR #9 合入 `aedbdd5` 后，Codex 仍指出八项 P1。覆盖按「数字 + 紧随单位 + 邻近主语」核对，六十次和九十天对调、摄氏度写成华氏度都失败。空正文仍先记下已知 token 和费用，再进入 `NEEDS_REVIEW`。派发前用本地上限估算最坏费用（输入每 token 100 micro-USD，输出每 token 400），装不下就不发；这不是供应商价目。token 预留按 chat 包装计算，而不是裸 prompt。`observed` 超额进入预算快照和费用接口，不并进已知花费。`execute-interruptible` 让暂停和取消在活动进行时写入数据库，旧历史仍走原路径。控制投递使用单独线程池。剩余时间不大于 0 时不派发。显式来源文件缺失与损坏一样停止。降级迁移先把 `observed` 行映射为 `unknown` 并保留回报金额。不改变 `live_ready`。`comparative_evidence` 仍是 `not_run` |
| ADR-F23 | 目标完成要绑住对象、单位、时段和比较关系；来源身份不因字节去重消失 | 2026-10-15：第五轮审计 `aedbdd5`。同一组数字对调对象、每分钟改成每秒、最多改成超过，都不能成功。目标明确要两件事而资料只支持一件时，结论是 `unverifiable`，不是强 PASS。正确转述仍然可以成功；十二字引用继续写在报告的 `citation` 里，但不单独否决已经对上的转述。验收版本改为 `goal-artifact-7`。空正文和 token 超额都把已知 token 计入 `tokens_used`。令牌发出之后、传输之前若任务已是 PAUSED 或 CANCELLED，就不发送，并释放未发出的预留。已有答案的重试和最终 SUCCEEDED 提交都再核对 `active_seconds`。`project_private` 资料在被任务引用后仍可被来源选择读到。不改变 `live_ready` |
| ADR-F24 | 多对象答案必须带主语；停发许可在真正派发前可以被取消 | 2026-10-16：PR #11 合入 `967b55f` 后 Codex 仍指出的 P1。两个对象的事实不能被没有主语的数字对上。目标拆开的每一部分要碰到自己的对象，共享的「限额」不算。控制命令排在非控制命令后面时仍要抽出来处理。`apply_task_control` 使用单独的控制线程池。派发先取得 `permitted`，暂停或取消提交后把它撤销，`consume` 失败就不再调用供应商。尚未派发就到期的预留改为释放，而不是标成 unknown。输出上限按剩余金额缩小。验收版本改为 `goal-artifact-8`。不改变 `live_ready`，`comparative_evidence` 仍是 `not_run` |
| ADR-F25 | 记忆 claim 不是资料；逻辑产物可以共享字节；一条坏命令不能停掉 worker；终态不能改写 | 2026-10-16：系统审计第一轮里本批负责的项。记忆 claim 使用 `memory_claim`，删除和更正会给旧字节打 tombstone，来源发现和显式名单只接受 `project_private`。显式把记忆 id 当资料会得到 `allowed_sources_invalid`，不把记忆文本送去当证据。同一 `storage_key` 的活行按分类和媒体类型区分，tombstone 不挡住以后的新行；第一次绑定仍会写 `artifacts.task_id`，以保持既有的任务引用测试。命令应用按条使用 savepoint，非暂时错误标 FAILED，`route_pending` 和 `serve` 吞掉单次失败后继续。同一任务的信号失败时，后面的命令回到 PENDING。`resume` 不清除 `cancel_requested`。公开合同哈希取自 `current_contract_id`，终态任务不能改草案。状态变化和 `task.status` 事件在同一事务里写入。事件序号在锁住任务行后分配，冲突则重试。新工作流用 `bounded-activity-retry` 把活动重试限制为 3 次并设置心跳超时，`ArtifactError` 记成 `NEEDS_REVIEW` 而不再抛出。数据库拒绝离开 SUCCEEDED、FAILED、CANCELLED。不改变 `live_ready`，`comparative_evidence` 仍是 `not_run`。活动超时后两次尝试互相改写预算的部分留给派发状态机那一批 |
| ADR-F26 | 未发出的预留可以接管或释放；验收只看模型当时看到的资料；评分不取自候选进程；令牌绑定账本中的单个请求；作业取消要确认容器已停 | 2026-10-16：系统审计第一轮 P1-04、P1-05、P1-06、P2-01、P2-05、P2-07，基线 `d6ed382`。每次执行带 `attempt_id`，只有 `sending` 的预留才记为 unknown；`dispatched`/`permitted` 由重试重新定价接管，网关在联系供应商前拒绝时释放并转人工复核。生成记录写入 `source_manifest`，验收器按清单重读资料，清单缺失、字节变化或提示哈希不符都是 `UNVERIFIABLE`；同一主语、单位、时段、比较关系的资料数值不同（中文数字归一化后）为 `conflicting_sources`，不判 PASS。验收版本改为 `goal-artifact-9`。评分器父进程不导入候选代码，pytest 在降权到 nobody 的子进程里跑，以退出码和随机路径 JUnit 报告为准，每个受信测试都要通过。报告的模式取自生成记录；未报用量是 unknown 并继续冻结，不判超额；LiteLLM 金额标为 `local_price_estimate` 并带 `pricing_version`。模型令牌要有 `task_id`、`request_key`、`attempt_id`，网关在同一事务核对账本后单次作废，任务停止即撤销。作业取消删除令牌和容器，核对标签下无残留才是 `confirmed`，已取消作业不再启动。不改变 `live_ready`，`comparative_evidence` 仍是 `not_run` |

实施时确需改架构：追加日期、具体失败证据、最小变更、风险、迁移与新增/修订测试。不因个人偏好换栈；不得删除失败记录。
