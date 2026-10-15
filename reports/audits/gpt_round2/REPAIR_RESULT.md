# 审计二轮修复结果

基线 `55fd636fa1615314c129f5728663f3450595bc47`。没有改 `STATUS.json`，没有把失败标成跳过，也没有把脚本提供者写成 live 成功。

## R2-01 验证自引用和证据复用

**修复前：** `goal_effect._ensure_verification` 把同一个 `artifact_sha` 同时传给 `artifact_sha256` 和 `observed_sha256`，查询只按 project、task、`goal_artifact` 取第一条。损坏或 tombstone 后 `ArtifactStore.get` 已经失败，这条验证仍会写成 PASS；换候选仍返回旧 id。

**根因：** 观察值由调用方填入，没有读盘。去重键也不包含候选、合同和摘要。

**修改：** `services/worker/aether_worker/goal_effect.py`、`services/worker/aether_worker/research.py`。验证角色自己 `get` 当前候选，用读到的字节计算 observed。删掉、哈希不一致，或回答没有包含来源里一段不在目标原文中的事实，都写 FAIL。已有证据必须同时匹配 artifact、contract、criterion 和两边摘要。

**修复后：** `tests/integration/test_audit_round2.py::test_goal_verification_reads_bytes_and_does_not_reuse_another_candidate` 在真实 PostgreSQL 上通过。完整候选 PASS；损坏字节和 tombstone 不是 PASS；另一条候选和合同得到新的验证 id。

## C01 任务停在保存目标

**修复前：** `DurableTaskWorkflow` 只调用 `materialize_goal`，把目标原文存成 JSON，并断言进程测试里的状态是 `QUEUED`。

**根因：** 常驻进程会消费 outbox，但没有把读取、脚本执行和独立验收接成会改变任务状态的活动。

**修改：** 仍是 Temporal 加 `command_outbox`。`prepare_task` 把任务写成 `RUNNING`。`execute_task` 用 `ArtifactStore.get` 读取已上传文本，调用 `scripted_answer`（明确不是 live 模型），再由 `aether_verifier` 读盘验收。PASS 才是 `SUCCEEDED`，检查失败是 `FAILED`，没有可读取资料是 `NEEDS_REVIEW`。编码作业继续走已有的 `coding.create`，由同一个进程调用 `apply_console_commands`。`AETHER_INTAKE_PAUSE_SECONDS` 默认 0，正式路径在 `RUNNING` 之后立刻执行。进程测试把它设成 4，在看到 `RUNNING` 时停掉 worker，再启动，然后检查终态；第二次启动不增加 `research.call`。

**修复后：** `tests/integration/test_worker_process.py` 通过。任务离开 `QUEUED`，回答里有资料中的事实，不是目标原文，验证者是 `aether_verifier`，`research.call` 只有一条。

## R2-02 日志短写和截断尾

**修复前命令与输出：**

```text
uv run --locked --no-build pytest -q tests/integration/test_audit_round2.py -k 'short_write or truncated_journal or corrupt_middle' --tb=line
E   json.decoder.JSONDecodeError: Unterminated string starting at: line 1 column 16 (char 15)
E   json.decoder.JSONDecodeError: Expecting value: line 1 column 34 (char 33)
E   json.decoder.JSONDecodeError: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)
```

**根因：** `_journal` 只调用一次 `os.write`，不看返回长度。`_journal_rows` 对半行 `json.loads`，整次恢复因此中断。

**修改：** `packages/storage/aether_storage/artifacts.py`。写满才 fsync；零写入报 `journal_short_write`。恢复在独占锁里把没有换行的尾部挪到 `journal-quarantine/`，前面的完整记录照常恢复。中间一整行损坏报 `journal_corrupt`，不把它当成不存在。

**修复后：** 上述三项以及全量套件里的恢复用例通过。

## R2-03 `./` 成员路径

**修复前：**

```text
E   aether_storage.artifacts.ArtifactError: 解压结果与归档成员不一致
E   FileExistsError: [Errno 17] File exists: '.../src/main.py'
```

ZIP 和 TAR 的 `./src/main.py` 被允许写入 `src/main.py`，比较时键仍是 `./src/main.py`。两个同名规范路径会在同一临时文件上冲突。

**根因：** 快照字典用归档里的原始名字，树扫描用的是规范化相对路径。

**修改：** 用 `PurePosixPath` 去掉 `.` 和重复分隔符。规范化后重名报 `archive_collision`。`..`、绝对路径和符号链接仍拒绝。

**修复后：** `test_dot_slash_archive_members_publish` 和 `test_canonical_archive_collision_is_rejected` 通过。原有的穿越和链接用例仍在 `test_artifacts.py` 里通过。

## R2-04 Database.fetch_one / fetch_all

**修复前：**

```text
fetch_one False fetch_all False
E   AttributeError: 'Database' object has no attribute 'fetch_one'
```

**根因：** 上一轮把这两个方法缩进到了 `_release_dbapi` 的 `return` 后面，不再是 `Database` 的方法。事务提交回调没有改。

**修改：** `packages/storage/aether_storage/db.py` 把方法放回类里。

**修复后：** `test_database_fetch_methods_read_postgres_rows` 对真实 PostgreSQL 读到 `{"value": 42}` 和两行 `generate_series`。

## 全量

```text
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres \
  AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 \
  uv run --locked --no-build pytest -q tests/unit tests/integration --tb=line
254 passed, 1 warning in 198.69s
EXIT:0
```

修复前同一批新增检查是 `7 failed`（验证用例当时还未按独立读盘的签名跑）。原有套件约 246 项，加上这 8 项是 254。`scripts/verify_freeze.py` 在没有把 `.hermes` 算进规格扫描时 `passed`。没有重跑 F26 live。脚本提供者不是供应商模型。
