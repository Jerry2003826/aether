# AETHER 首轮代码调试报告

**日期：2026-10-15。仓库：Jerry2003826/aether。固定基线：`b570f9197c650212a693f34a18d3729a04772f0d`。**

## 结论

本轮确认四类产物链路缺陷，交付一个最小修复补丁，以及能继续暴露其余缺陷的回归用例。没有修改远端仓库，也没有声称整套 Agent 已验收。

原始代码的扩展隔离测试：**13 项，8 失败、5 通过**。仅应用二进制上传补丁后：**3 失败、10 通过**。补丁相关的单独选择集：**8 通过、5 未选择**。失败数不等于缺陷数；多个输入触发同一个哈希缺陷。

另发现产品端到端测试覆盖缺口。它单独列出，**不算作已动态复现的第五个运行时 Bug**。

## 调查方法与限制

容器直接克隆 GitHub 因 DNS 解析失败，改为通过已连接的 GitHub 工具读取固定提交的源代码、目录树、测试和 CI 记录。没有完整仓库检出。

用于运行的五个源文件均与 GitHub 返回的 Git blob SHA-1 完全一致，校验见 `evidence/source_checksums.json`。`original/` 是这五个文件的精确子集；`patched/` 只修改 API 上传哈希。**两者都不是完整 AETHER 源码包，不要覆盖到项目根目录。**

实际运行分两层：

- 真实 ArtifactStore 与文件系统，使用 SQLAlchemy + SQLite 的真实提交/回滚；仅将 `id::text` 转成 SQLite CAST。测试数据库不是完整生产 schema，没有验证 PostgreSQL、RLS、并发唯一约束或真实恢复服务。
- 从固定 `app.py` AST 提取未改动的 `upload_artifact` 函数及路由装饰器，交给 FastAPI TestClient，运行实际 multipart 解析、上传函数、ArtifactStore 与文件写入。身份认证和项目授权是明确的测试替身；SQL JSON 方言做了适配。这不是完整 `create_app()` 集成测试。

未在本环境运行：完整仓库单元/集成测试、PostgreSQL/Alembic、Temporal 服务与重放、OpenHands、BGE、CodeGraph、Linux 容器隔离、真实供应商模型、完整浏览器工作台。没有使用用户真实凭证或发出有成本的模型请求。

本地依赖版本见 `evidence/environment.json`，并非仓库 `uv.lock` 的依赖环境。

## D01｜P1：二进制上传先做 UTF-8 文本哈希，触发 HTTP 500

**证据等级：源代码 + 哈希函数复现 + 隔离 HTTP 路由复现。补丁：已提供；生产集成未验收。**

位置：`services/api/aether_api/app.py:193`；`packages/domain/aether_domain/security.py:9-10`。

上传函数先执行：

```python
payload_hash = sha256_text(f"{media_type}:{sha256_text(data.decode('utf-8', 'surrogateescape'))}")
```

`surrogateescape` 将不能解码的字节变成代理字符，而 `sha256_text` 又严格执行 `value.encode('utf-8')`，因此抛出 `UnicodeEncodeError`。异常发生在后续存储错误处理之前。

本轮观测：包含非 UTF-8 二进制头的 PDF 输入与实际生成的 ZIP 在隔离 HTTP 路由返回 500；非法 UTF-8 的 `text/plain` 也返回 500，而应是可解释的 422。正常 UTF-8 文本对照通过。PDF 隔离用例仅为二进制头 fixture，不代表完整 PDF 解析已验收；仓库集成用例另构造了完整最小 PDF。

最小补丁：

```python
import hashlib
payload_hash = sha256_text(f"{media_type}:{hashlib.sha256(data).hexdigest()}")
```

直接对原始字节求摘要，外层仍保留 media type。对正常 UTF-8 输入，旧实现的解码再编码与原始字节相同，因此摘要保持兼容。没有修改共用的 `sha256_text`，没有放宽 MIME、大小、Origin、CSRF 或权限检查。

补丁文件：`patches/0001-binary-safe-upload.patch`。已对 byte-exact 原始文件执行 `git apply --check`。

修复后，隔离端点二进制输入返回 201、存储字节哈希一致、可通过 store 读回；非法 UTF-8 文本返回 422。见 `evidence/binary_fix_targeted.txt`。

来源：[上传端点](https://github.com/Jerry2003826/aether/blob/b570f9197c650212a693f34a18d3729a04772f0d/services/api/aether_api/app.py#L180-L231)、[文本摘要](https://github.com/Jerry2003826/aether/blob/b570f9197c650212a693f34a18d3729a04772f0d/packages/domain/aether_domain/security.py#L9-L10)。

## D02｜P1：外层数据库事务尚未提交，持久日志已经写 committed

**证据等级：实际源代码 + SQLite 真实回滚复现。状态：未修复。**

位置：`ArtifactStore.put()`、`recover()`、`_insert()`；API 上传函数确实传入外部 `conn`。

调用顺序是写持久文件、写 prepared 日志、向调用方事务插入数据库记录、立即写 committed 日志。调用方此时仍在事务中，还要更新幂等记录，最后才提交。

复现：在外层事务里调用 `put(conn=conn)` 后主动抛异常。数据库行已经回滚，但磁盘日志仍包含 committed；`recover()` 又直接跳过该 ID，因此返回空列表。

准确影响：**恢复状态与数据库不一致、待对账字节可能被漏处理。** 没有证据证明一个已经成功提交并向用户确认的上传在本轮丢失；不要把该问题描述成已证实的已提交数据丢失。

修复要求：数据库提交事实是权威；区分显式暂存、提交未知和明确回滚。可使用同事务发布意图与提交后的可重试日志发布，但不能在事务提交前宣称 committed。对旧日志要提供对账路径。显式回滚和已删除数据不得通过“见到 prepared 就插入”的粗糙补丁重新变成可见产物。完整改动须在 PostgreSQL 下测试提交失败、进程崩溃、连接中断、重试及删除语义。

来源：[ArtifactStore](https://github.com/Jerry2003826/aether/blob/b570f9197c650212a693f34a18d3729a04772f0d/packages/storage/aether_storage/artifacts.py)、[事务管理](https://github.com/Jerry2003826/aether/blob/b570f9197c650212a693f34a18d3729a04772f0d/packages/storage/aether_storage/db.py)。

## D03｜P1：相同内容去重后，恢复函数报告数据库里不存在的 ID

**证据等级：实际源代码 + SQLite/真实文件复现。状态：未修复。**

每次 `put()` 都生成新 UUID 写 prepared。`_insert_on()` 遇到相同 storage key 返回原 ID。于是第二次调用留下“新 ID 的 prepared”，但 committed 指向旧 ID。

恢复函数再次调用 `_insert()`，却不接收其返回的规范 ID，直接把 prepared 的新 ID 记成 committed 并加入 `restored`。

复现：对同一 project 连续两次 `put()` 完全相同的字节；两个正常返回值都是同一个真实 ID。随后 `recover()` 返回另一个 UUID；直接查询数据库确认该 UUID 不存在。

修复要求：区分写入尝试 ID 与规范产物 ID，记录映射并处理旧日志；恢复结果只能引用实际存在的规范产物。第二次恢复不得继续处理同一已解决记录。不能只把 append 改成旧 ID 而留下永久 pending 的 prepared 日志。还要保留 tombstone 语义与元数据一致性。

来源：[ArtifactStore](https://github.com/Jerry2003826/aether/blob/b570f9197c650212a693f34a18d3729a04772f0d/packages/storage/aether_storage/artifacts.py)。

## D04｜P1：同一压缩包重试导入抛出 FileExistsError

**证据等级：实际源代码 + 真实 ZIP 解压与文件系统复现。状态：未修复。**

位置：`ArtifactStore.import_archive()`，`destination.mkdir(parents=True, exist_ok=False)`。

相同压缩包经过内容去重返回相同 artifact ID，所以第二次导入使用相同 snapshots 目录；目录已存在便抛异常。首次导入与实际文件内容对照通过，第二次失败。

修复要求：解压到独立临时目录，校验完成后原子发布；完成状态必须由服务端可信元数据管理，归档内容不得伪造完成标记。重试时只有经过验证的完整快照可以复用。残留半成品应隔离/重建。**仅改成 `exist_ok=True` 不足以修复，会掩盖半成品与不一致。** 并发导入、解压中断、篡改、链接和路径穿越都须有测试。

来源：[导入逻辑](https://github.com/Jerry2003826/aether/blob/b570f9197c650212a693f34a18d3729a04772f0d/packages/storage/aether_storage/artifacts.py#L128-L146)。

## C01｜高优先级验收缺口：测试代替产品进程执行了命令

**证据等级：静态证据；没有在本环境动态复现完整产品任务卡住。**

`tests/integration/test_console_browser.py` 在 POST 创建 source 返回 202 后，由测试代码直接调用 `apply_console_commands(...)`，再刷新页面。因此此浏览器测试确实验证了真实页面/记录展示等能力，却没有验证普通启动方式下命令是否会被常驻 worker 自动消费。

本轮读到的 README 启动方式包括 PostgreSQL/Temporal、API、Next.js，没有常驻 worker/dispatcher 的启动命令；`scripts/` 列表也主要是探针与验收脚本。搜索发现相关调用主要在测试中，但搜索并不能证明全库/外部部署不存在入口。

此外，读到的 `ResearchWorkflow` 调用 `record_research_effect`；该 activity 记录数据库 `research.call` 事件，不等于实际研究模型执行。`research.py` 明确声明是 scripted research。它们可以是合法的基础设施测试，但不能作为 live 产品任务链路的替代证据。

应补一个**不直接调用后台内部函数**的进程级 smoke：按文档启动正式进程 → 使用 HTTP 登录/创建项目/提交目标 → 观察命令自动消费 → 观察真实产物和独立验证 → 停止/恢复 worker → 检查无重复副作用。只有该测试成立，才能对“按 README 启动即可自动执行任务”作出可靠结论。

本条不意味着所有已有测试无效，也不意味着本轮已经证明整个 Agent 完全不能运行。

来源：[浏览器测试](https://github.com/Jerry2003826/aether/blob/b570f9197c650212a693f34a18d3729a04772f0d/tests/integration/test_console_browser.py)、[README](https://github.com/Jerry2003826/aether/blob/b570f9197c650212a693f34a18d3729a04772f0d/README.md)、[ResearchWorkflow](https://github.com/Jerry2003826/aether/blob/b570f9197c650212a693f34a18d3729a04772f0d/services/worker/aether_worker/research_workflow.py)、[research.py](https://github.com/Jerry2003826/aether/blob/b570f9197c650212a693f34a18d3729a04772f0d/services/worker/aether_worker/research.py)。

## CI 与本轮结果的区别

固定 main 的 CI run 175（38718720987）已成功。另实际读取了前一 PR 的 integration job 116366115749 完整日志：138 passed，1 warning；该 job 检出的合并 SHA 是 `9d1f3e1debc6cc88f8ae88ffb57ea2b87675d9c0`，不是本轮 pinned main SHA。

该 CI 日志中的 doctor 报告 OpenHands 与 BGE 未安装；Temporal 服务为 in-memory dev。**这只说明该 CI job 的环境范围，不能据此否定用户本机或另一个 live harness 已运行的结果。** 本轮没有重跑或独立确认仓库所描述的 F26 本机 live 验收。

CI 记录：[main run](https://github.com/Jerry2003826/aether/actions/runs/38718720987)、[已读取的 PR run](https://github.com/Jerry2003826/aether/actions/runs/38718243984)。摘录摘要见 `evidence/remote_ci_summary.json`；它是人工提取的摘要，不冒充完整原始 CI 日志。

## 交付及建议顺序

1. 应用 D01 最小补丁；在真实仓库 PostgreSQL 环境运行二进制上传/回放/冲突测试。
2. 在隔离开发分支联合修复 D02/D03 的提交事实、规范 ID 和恢复日志语义，再处理 D04 的快照原子发布。
3. 跑 C01 的真正进程级产品闭环，然后继续审查 Temporal 重放、权限预算、沙箱和记忆删除。

`repository_tests/tests/integration/test_audit_artifact_regressions.py` 是按现有测试夹具编写的六个仓库集成用例，**本轮仅做语法检查，未执行**。其中三项覆盖尚未修复的问题，应用 D01 后不能期待整个文件变绿。

本轮不调整技术选型，不加入新框架，不改冻结验收标准，不以删除失败测试换取通过。
