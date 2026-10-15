# 交接

F04–F06 在全量套件通过后保持 done。四条 Critical（请求哈希、token 预留、连接重置、资源当前版本）的复现测试在 `tests/integration/test_budget.py` 与 `tests/integration/test_operations.py`。对照表仍是 `reports/f04_f06_criteria.md`。

## 全量测试（标 done 之前）

```text
AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/unit tests/integration
...................................................................      [100%]
67 passed, 2 warnings in 12.57s
EXIT:0
```

警告是既有的 Starlette `httpx` 弃用提示，以及 Pydantic 对 `ChatCompletionReasoningItem` 的 `ReadOnly` 提示。没有跳过的集成用例。

## P1 之后的全量测试

```text
AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/unit tests/integration
........................................................................ [100%]
72 passed, 2 warnings in 15.23s
EXIT:0
```

dispatcher 接上之后又跑了同一条全量命令：`74 passed, 2 warnings in 14.91s`，`EXIT:0`。

资源行锁和 httpx 传输错误之后：`78 passed, 2 warnings in 15.67s`，`EXIT:0`。派发前对 `resource_heads` 使用 `FOR UPDATE`。确认后的资源版本只经 `note_resource_version` 写入。`httpx.ConnectError`、`ReadTimeout`、`RemoteProtocolError` 以及它们的包装异常记为 unknown。

P1 覆盖：

- `aether_api` 不能写 operations、budget_accounts、model_requests、audit_events、verification_results、evaluation_runs；不能 UPDATE/DELETE command_outbox。新表不继承 DML。
- SUCCEEDED、FAILED、CANCELLED 不能改回 QUEUED、DRAFT、RUNNING。API 仍不能把任务推进到 DRAFT/QUEUED 以外。
- `ensure_database` 写入 `alembic_version=0004`。删掉版本行后再 `alembic upgrade head` 会在已存在的 `owners` 上失败。
- `AETHER_ALLOW_NON_LOOPBACK=1` 单独不能绑定 `0.0.0.0`，还要 Secure cookie 和 TLS 证书文件。
- 登录失败名额在密码校验前原子占用。8 个并发错误口令只有 5 个进入校验，其余 429。

## 状态

- F00、F02、F03、F04、F05、F06、F07：done
- F01 done。CPU 上对 `aether probe` 做了一次 BAAI/bge-m3 编码，`dimension_probed` 为 1024，revision 为 `5617a9f61b028005a4858fdac845db406aefb181`。OpenHands 仍是 partial，live 模型 not_run。
- F07 done。`aether_dispatcher` 把 `command_outbox` 投到真实 Temporal。Temporal 拒绝时行留在 DELIVERING，任务保持 QUEUED，不会被标成成功。同一个 command_id 再投递不会在 Workflow 里出现两次。
- F08 done。任务创建返回 202，状态保持 QUEUED，并经 dispatcher 进入真实 Temporal。SSE 用 Last-Event-ID 按 sequence 补齐，响应里有 pending command 和 projection_updated_at。审批决定由服务端核对 owner、哈希和版本。事件和产物不跨项目。全量：`83 passed, 2 warnings in 16.57s`，`EXIT:0`。对照表 `reports/f08_criteria.md`。
- F09 done。本地产物经真实存储和文本工具进入脚本网关，HTTP 调用 1 次，哈希与 `aether_verifier` 的 PASS/FAIL 写入验收表。过期候选在 actor 自报 PASS 时仍为 FAIL。worker 重启后重投同一命令，`research.call` 仍是 1 条。全量：`87 passed, 2 warnings in 17.84s`，`EXIT:0`。对照表 `reports/f09_criteria.md`。
- F10 done。HTTPS 把连接钉在解析出的地址上，证书和 SNI 仍核对原主机名。响应按流计数，超限即断开。RSS 在带资源限制的子进程里用 defusedxml 解析。Playwright 受控 profile 实际执行了页面脚本。`allow_hosts` 没有测试开关时拒绝。测试开关打开时，不在 `allow_hosts` 里的 `127.0.0.1` 仍拒绝，不会连上。`resource_heads` 的 `FOR UPDATE` 只是派发前预检。全量：`100 passed, 2 warnings in 24.21s`，`EXIT:0`。对照表 `reports/f10_criteria.md`。
- F11 done。项目设置用 `set_config(..., true)`，只在当前事务有效。PR #1 CI run `38676628928`：unit `32 passed`，integration `91 passed, 2 warnings in 35.83s`。SonarCloud quality gate OK，可靠性评级 1。对照表 `reports/f11_criteria.md`。
- 本地 MCP stdio 夹具已能完成 argv 拒绝、schema 隔离、真实 echo、超时 unknown、重连和密钥脱敏。F17 仍是 not_started：交付物要求官方 SDK 客户端，当前客户端不是官方 SDK。
- F12 done。没有测试产物的 `observed_test` 降为 proposed。同一 CI run 通过。对照表 `reports/f12_criteria.md`。
- F13 done。GitHub runner 上的真实容器测试通过：同一 job 只有一个容器，重启后重新挂上；容器内公网、元数据和 Docker socket 不可达，密钥不在环境里，只能打到该 job 的脚本网关。对照表 `reports/f13_criteria.md`。
- F14 done。PR #1 CI run `38678232085`：unit `32 passed`，integration `100 passed, 2 warnings in 45.73s`。SonarCloud quality gate OK。评分在沙箱里只跑绑定套件。对照表 `reports/f14_criteria.md`。
- F15 done。PR #1 CI run `38681057899`：unit `32 passed`，integration `102 passed, 2 warnings in 52.97s`。SonarCloud quality gate OK，可靠性、安全、可维护性都是 1。CodeGraph 1.6.2 的 CLI 在 `--network none` 容器里跑，包哈希已核对。产品开关仍然关闭，所以 `codegraph_enabled` 仍是 not_run。对照表 `reports/f15_criteria.md`。
- F16 done。PR #1 CI run `38682012230`：unit `32 passed`，integration `103 passed, 2 warnings in 70.39s`。SonarCloud quality gate OK。删除是 tombstone：visibility epoch 增加，BM25 generation 退休，已删 chunk 的向量被清空，推荐特征快照记为失效。晚到的向量和记忆文本写不回去。结果写明备份和 Temporal 历史还在。对照表 `reports/f16_criteria.md`。
- F14 done。PR #1 CI run `38683576919`：unit `32 passed`，integration `112 passed, 2 warnings in 70.42s`。SonarCloud quality gate OK。评分器用 `python -I`，先导入 pytest，再把 `/work/src` 加到 `sys.path` 末尾。顶层 `pytest.py` 和 `sitecustomize.py` 直接失败。隔离结果只认评分器写出的第一行。
- F16 done。同一 CI run。搜索缓存同时绑定 visibility epoch 和索引代，删除后不再返回已删内容。
- CodeGraph 产品开关仍然关闭，`codegraph_enabled` 仍是 not_run。
- F17 done。PR #1 CI run `38684944139`：unit `34 passed in 8.20s`，integration `113 passed, 1 warning in 70.30s`。SonarCloud quality gate OK。进程崩溃后调用返回 unknown，后续调用不再挂起。服务器起不来是 `connect_failed`。
- F18 done。同一 CI run。未批准不建 Temporal schedule。水位不变不写通知。重复 trigger 只有一条站内通知。停止后不再派发。默认不发外部消息。
- F19 done。PR #1 CI run `38685299953`：unit `34 passed in 6.97s`，integration `114 passed, 1 warning in 67.18s`。SonarCloud quality gate OK。探索默认关闭。不称协同过滤。对照表 `reports/f19_criteria.md`。
- F14、F17、F20 done。PR #1 CI run `38686196972`：unit `40 passed in 10.06s`，integration `115 passed, 1 warning in 69.16s`。SonarCloud quality gate OK。评分结果只认父进程管道。MCP 注解不放宽本地风险和审批。技能包按需加载，未批准脚本不执行。`codegraph_enabled` 仍是 not_run。
- F21 done。PR #1 CI run `38686730319`：unit `40 passed in 7.73s`，integration `118 passed, 1 warning in 75.07s`。SonarCloud quality gate OK。对照表 `reports/f21_criteria.md`。
- F20、F21 done。PR #1 CI run `38687896639`：unit `40 passed in 7.43s`，integration `123 passed, 1 warning in 72.10s`。SonarCloud quality gate OK。密钥整段替换。脚本整目录哈希后在容器里用 `python -I` 执行。回滚中途失败时指针和 canary 状态都不提交。对照表 `reports/f20_criteria.md`、`reports/f21_criteria.md`。
- F22 done。PR #1 CI run `38690927397`：unit `42 passed in 10.84s`，integration `129 passed, 1 warning in 94.24s`。SonarCloud quality gate OK。49 个操作与合同一致。控制台读真实 API。对照表 `reports/f22_criteria.md`。
- 下一步：F23 安全与故障回归。live 模型、Hermes、CodeGraph 产品启用仍是 not_run。
