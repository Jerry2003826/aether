# 审计一轮修复结果

基线 `b570f9197c650212a693f34a18d3729a04772f0d`。没有改 `STATUS.json`，没有放宽已有断言，也没有把失败用例标成跳过。

本机复现用的是 PostgreSQL 17（`pgvector/pgvector:0.8.7-pg17`）和 Temporal `1.9.1` 的 `server start-dev`。下面的命令都在这个环境里实际跑过。

## D01 二进制上传

**复现（修复前，6 个审计回归里的 3 个）：**

```text
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres \
  AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 \
  uv run --locked --no-build pytest -q tests/integration/test_audit_artifact_regressions.py --tb=line
```

PDF、ZIP 和非法 UTF-8 文本都在 `upload_artifact` 里失败：

```text
services/api/aether_api/app.py:193: in upload_artifact
    payload_hash = sha256_text(f"{media_type}:{sha256_text(data.decode('utf-8', 'surrogateescape'))}")
packages/domain/aether_domain/security.py:10: in sha256_text
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
E   UnicodeEncodeError: 'utf-8' codec can't encode character '\udcff' in position 0: surrogates not allowed
```

Starlette 的测试客户端会把未捕获异常重新抛出。这条 `UnicodeEncodeError` 发生在存储错误处理之前，生产路径上就是 HTTP 500。`sha256_text` 没有改。

**根因：** 先用 `surrogateescape` 把任意字节解码成文本，再按 UTF-8 编码进摘要。非 UTF-8 字节变成代理字符，严格的 `encode('utf-8')` 抛错。

**修改：** `services/api/aether_api/app.py`。摘要改为 `sha256_text(f"{media_type}:{hashlib.sha256(data).hexdigest()}")`。合法 UTF-8 的旧摘要不变。非法 UTF-8 的 `text/plain` 仍由 `ArtifactStore._validate_bytes` 给出 `media_mismatch`，接口映射为 422。

## D02 日志先于外层提交写成 committed

**复现：** 同一条命令里的 `test_external_transaction_must_not_write_committed_journal_early`：

```text
E   assert not True
```

`put(conn=conn)` 在调用方事务还没提交时就把 journal 写成 `committed`。外层回滚后数据库行消失，恢复却认为已经完成。

**根因：** `_insert` 使用调用方连接，插入尚未提交；下一条 journal 已经写 `committed`。

**修改：**

- `packages/storage/aether_storage/db.py`：事务成功提交之后才跑 after-commit 回调。事务体内的异常记回滚。`commit()` 自己抛错时记 unknown，并释放还开着的数据库事务。已经到达服务器的提交不会被后来的 rollback 撤销。
- `packages/storage/aether_storage/artifacts.py`：`put` 在提交前只写 `prepared` / `awaiting_commit`。提交后重新读取行，再写 `resolved` 和 `committed`。显式回滚写 `rolled_back`。看不到行也看不到回滚记录时，`recover` 写成 `reconciled`，不插入可见产物。

## D03 去重后的恢复 id 不在库里

**复现：** `test_dedup_recovery_only_returns_existing_ids`：

```text
E   aether_storage.artifacts.ArtifactError: 产物不存在
```

第二次 `put` 用新的 UUID 写 `prepared`，插入时碰到同一 `storage_key` 就返回旧 id。`recover` 不看 `_insert` 的返回值，把那个新 UUID 记成 committed 并放进结果。

**根因：** 尝试 id 和规范产物 id 混用。

**修改：** journal 带 `attempt_id`。去重命中时 `resolved.artifact_id` 是库里的规范 id。`recover` 只把这次新插入、并且 `get` 能读到的 id 放进返回值。第二次恢复看到 `resolved` / `rolled_back` / `reconciled`，或一条已经能在库里对上的 `committed`，就不再写日志。旧日志里“committed 了但行不存在”对账为 `stale_commit`，不重新公开。tombstone 占用唯一的 `storage_key`，再 `put` 得到 `tombstone_retained`，恢复也不会清掉 `deleted_at`。

## D04 同一压缩包再次导入

**复现：** `test_same_archive_retry_keeps_a_complete_snapshot`：

```text
E   FileExistsError: [Errno 17] File exists: '.../snapshots/<project>/<artifact>/'
```

内容去重返回同一个 artifact id，目标目录用 `exist_ok=False` 创建，第二次必然冲突。

**根因：** 解压直接写最终目录，没有独立的完成状态。

**修改：** 先检查路径、链接、文件数、大小和压缩比，再把成员写到 `tmp/extract/<uuid>`。校验字节后 `os.rename` 到 `snapshots/<project>/<artifact>`。完成标记写在 `snapshot-meta/<project>/<artifact>.json`，不在解压树里，归档成员伪造不了它。已有目录只有在成员字节和标记都对得上时才复用；否则整目录挪到 `quarantine/` 再重建。并发导入用 `flock`。没有把最终目录改成 `exist_ok=True`。

## C01 进程级消费

**原状：** `tests/integration/test_console_browser.py` 在 HTTP 202 之后直接调用 `apply_console_commands`。README 原先没有常驻 worker 的启动命令。`deliver_pending` 会领取除 `memory.%` 以外的全部 outbox，因此一个通用轮询会把 `source.create` 标成失败或投进任务工作流。

**修改：**

- `services/worker/aether_worker/__main__.py`、`service.py`：`python -m aether_worker`。同一进程里跑 Temporal worker，并轮询 `deliver_pending`。
- `dispatcher.py` 只领取 `start`、`approve_goal`、`pause`、`resume`、`cancel`。
- 控制台种类走 `apply_console_commands`，验证种类走 `apply_verifier_commands`，`memory.*` 走 `apply_memory_commands`。没有新的调度框架。
- `DurableTaskWorkflow` 在 `start` 被接受后，把目标写成任务产物，再用 `aether_verifier` 调用已有的 `record_verification`。哈希一致才是 PASS。任务状态保持 `QUEUED`，不把投递写成任务成功。
- 启动方式、环境变量和停启语义写在 `README.md` 与 `docs/10_operations.md`。`MASTER_SPEC.md` 已按 `scripts/rebuild_spec.py` 重生成。

这不是 live 模型执行，也没有把 scripted research 说成供应商调用。

## 修复后的证据

审计回归和耐久性用例、worker 进程用例都包含在全量结果里。全量：

```text
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres \
  AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 \
  uv run --locked --no-build pytest -q tests/unit tests/integration --tb=line
231 passed, 1 warning in 162.14s
EXIT:0
```

警告是既有的 Pydantic `ReadOnly` 提示。没有失败，没有跳过。

规格检查在没有本地 `.hermes` 检出时：

```text
uv run --locked --no-build python scripts/verify_freeze.py
result: passed
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py' -q
Ran 76 tests
OK
```

`.hermes/web/tsconfig.app.json` 是 JSONC。`install_hermes.py` 把检出放在仓库里之后，`verify_freeze` 会去解析它。CI 的 pack job 在安装 Hermes 之前运行，检出目录也被 gitignore。这次没有把 `.hermes` 放进提交。

## 数量

| 套件 | 修复前 | 修复后 |
|---|---|---|
| `test_audit_artifact_regressions.py` | 6 failed | 含在下面的 231 passed 里 |
| `tests/unit` + `tests/integration` | 原有 212 项；本机第一次全量时 23 项因 Docker socket 权限、Playwright 浏览器和 Hermes 不在 `PATH` 失败，不是这次补丁的断言变化 | 231 passed。新增 19 项：审计回归 6、耐久性 11、进程级 worker 2 |

212 这个数是 231 减去新增 19。原有 23 项在补齐 CI 同等工具后通过，包括 CodeGraph、容器隔离、评分沙箱、浏览器控制台和 Hermes 版本探针。

## 没做的事

- 没有重跑 F26 live，也没有供应商密钥。
- 没有改交付状态。
- 浏览器集成测试仍会调用 `apply_console_commands`。进程级证据在 `tests/integration/test_worker_process.py`，它只通过 HTTP 提交，并单独起停 `python -m aether_worker`。
- Temporal 仍是开发用的 `start-dev`。journal 仍是产物目录里的 jsonl；提交后的那次落盘如果再崩溃，下一次 `recover` 按数据库行对账。
