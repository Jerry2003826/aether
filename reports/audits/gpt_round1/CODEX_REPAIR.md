# AETHER 已确认缺陷：定点修复交接

基线 Jerry2003826/aether@b570f9197c650212a693f34a18d3729a04772f0d。先读当前仓库 AGENTS.md 及本包 AUDIT_REPORT.md。直接调试现有实现，不重新设计 Agent 或重启仓库调研。

## 先保全现场

检查 `git status` 与当前提交，保留用户未提交修改。在隔离工作分支处理，不强制覆盖，不自动 push/发布。当前源码如已变化，重新定位对应逻辑，不盲目套补丁。本包 original/patched 只是五文件子集，绝不能替换整个项目。

## D01：先应用最小补丁

检查 `patches/0001-binary-safe-upload.patch`。修正 app.py 二进制指纹，保持 UTF-8 文本旧指纹兼容，不改公用 sha256_text 的安全语义。

把 `repository_tests/tests/integration/test_audit_artifact_regressions.py` 合并到项目测试目录；如同名文件存在，先比较，不覆盖。用现有 PostgreSQL 夹具先跑 -k 'binary_upload or invalid_utf8'。这三项本轮没有在真实仓库执行，需要你运行并保存实际结果。

## D02/D03：联合修复产物事务与恢复日志

准确复现：外部 conn 尚未 commit 时已写 committed；相同内容重复 put 后 recover 返回从未入库的新 UUID。

必须区分：数据库提交事实、调用尝试 ID、去重后的规范 artifact ID。日志只能在可证明的正确状态下推进。不要把已有明确回滚/删除的记录重新公开；提交结果未知应保留对账状态。兼容旧日志。恢复返回值只能引用可读取的真实产物，第二次恢复不应产生新工作。

测试真实 PostgreSQL：外层事务回滚、commit 异常、写文件后进程终止、去重重复调用、不同幂等键相同字节、并发 put、旧 journal、tombstone 后恢复。不要为了让单个隔离测试通过而更改数据库约束或删除语义。

## D04：快照原子发布与重试

同一 ZIP 重复 import_archive 当前抛 FileExistsError。不要只改 exist_ok=True。使用受控临时目录、文件/清单验证与原子发布；已有完整快照应可验证地复用；半成品不能冒充成功。保留路径、链接、文件数、大小及压缩比检查。完成标记不能由归档成员伪造。

测试首次/重复/并发导入、解压中断与重试、现存快照篡改和恶意归档。

## C01：验证真正产品启动链路

当前浏览器测试调用内部 apply_console_commands 代替后台消费。先找到生产入口（如已有，记录路径和启动命令），不要预设不存在。编写进程级测试，禁止在测试函数中直接执行消费者帮助推进业务。

只通过公开 HTTP/API 提交目标，观察由正式 worker 自动推进到产物和独立验证。停 worker 再恢复，验证无重复副作用。缺少常驻 worker/dispatcher 时补齐现有架构接线，不再引入第二套调度框架。避免通用 dispatcher 抢占其他 kind 的 outbox，需要验证实际命令路由。

## 报告要求

逐项输出：是否复现、根因、修改文件、失败前证据、修复后命令与实际结果、未运行环境、残留风险。运行现有单元与集成测试。不降低 STATUS/验收门槛，不伪造 live 成功。没有 API 密钥时不访问付费模型；没有 Docker/Temporal 时报告对应环境缺项，不假造替代环境通过。

本包已执行的本地测试不等同于你的 PostgreSQL、Temporal 或整仓测试；必须重新验证。
