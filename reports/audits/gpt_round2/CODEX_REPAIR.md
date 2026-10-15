# AETHER 第二轮修复交接

检查当前 Git 与未提交改动。本报告基于 55fd636fa1615314c129f5728663f3450595bc47，不得直接覆盖用户后续变化。先阅读 AUDIT_REPORT.md 和 evidence。保持 AETHER 既有架构，不重新调研。

## 两项局部修复

审查 patches/0001-restore-database-query-methods.patch 与 patches/0002-canonical-archive-member-paths.patch。git apply --check 后在隔离分支应用。source/ 和 patched/ 只是选定源码子集，不是完整项目。

数据库补丁恢复 Database.fetch_one/fetch_all 的正确类归属，不得回退提交回调。归档补丁统一 ./、重复分隔符等合法名字的规范路径，规范化重名必须拒绝，不得删除完整性比较、放宽 ../ 或符号链接限制。

## 尚未修复，必须完成

1. 新增 goal_artifact 验证：不能 expected 和 observed 同源填同一个变量。独立读取当前产物，检查删除/版本、实际字节摘要、合同和标准。证据复用必须绑定当前 candidate + contract + criterion/version。损坏、tombstone 和换候选三种测试均不能写或复用 PASS。本地测试是 helper 级，不是 PostgreSQL 安全证明；把它们迁移到现有完整角色/外键 fixture 验证。
2. 正式 worker 仍只 materialize goal JSON：将真实执行器接回 Temporal 工作流。实现研究目标从真实输入到实际答案与可信验收的进程链路；编码能力按授权 job 路径接入。测试不得只验证目标原文回显，也不得直接把 QUEUED 改成 SUCCEEDED。允许明确标记的 scripted provider，但 DB、Temporal、读取工具和结果验证必须真实。
3. 日志：处理 os.write 短写、零写和错误；尾截断恢复必须保留完整前缀并隔离坏尾，锁保护恢复与追加。中间损坏单独报告，不能忽略任意坏记录或吞异常返回空列表。补进程中断、短写、损坏尾以及恢复幂等测试。

## 验收

先建立失败复现，再最小修复，最后真实 PostgreSQL/Temporal 集成回归；有条件再在工具执行中途停 worker，检查无重复副作用、无丢任务、不误报成功。使用项目的真实锁文件；本包 requirements-audit.txt 只是隔离复现环境，不替换生产锁。

不要把本报告的 SQLite/FastAPI 边界测试数算成项目全量测试；不要把 CI 绿色、保存原始目标或存在 PASS 记录当作整个 Agent 已完成。

更新一份修复报告，逐项给出命令、退出码、基线和修复后的结果。未跑、阻塞、失败保留原状态。未经用户授权不 push、发布或调用付费服务。
