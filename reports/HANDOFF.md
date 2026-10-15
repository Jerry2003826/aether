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
- F11 done。BM25S generation、pgvector 和实测 BGE-M3 向量做 RRF，重排用实测的 bge-reranker-v2-m3 分数。删除、撤权和跨项目在进入上下文前过滤。旧 generation 标记 stale。中文、英文、emoji 和 `!=` 可检索。全量：`114 passed, 2 warnings in 25.54s`，`EXIT:0`。对照表 `reports/f11_criteria.md`。
- 本地 MCP stdio 夹具已能完成 argv 拒绝、schema 隔离、真实 echo、超时 unknown、重连和密钥脱敏。F17 仍是 not_started：交付物要求官方 SDK 客户端，当前客户端不是官方 SDK。
- F12 done。记忆按来源声称、提议、推断和用户确认分开保存。冲突只打上同一组标记，不改原来的说法。确认会新增 revision。上下文按预算组装，不把全量历史拼回去。高敏文本和跨项目 CodeGraph 在未批准出口时不会进入上下文。全量：`117 passed, 2 warnings in 26.69s`，`EXIT:0`。对照表 `reports/f12_criteria.md`。
- 下一步：F13 受限 Linux runner 和 OpenHands。F15 仍排在 F14 之后，不跳过。live 模型、CodeGraph、Hermes 仍是 not_run。

live 模型、CodeGraph、Hermes 仍是 not_run。
