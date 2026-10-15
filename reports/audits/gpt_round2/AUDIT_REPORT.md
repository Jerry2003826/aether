# AETHER 第二轮定点复审

审计提交：`55fd636fa1615314c129f5728663f3450595bc47`（本轮读取的 main）。上一轮基线：`b570f9197c650212a693f34a18d3729a04772f0d`。

## 结论

上一轮四个缺陷的原始触发场景已得到实质修复，并在本轮的本地隔离检查中通过。不能据此宣布整套 Agent 已通过验收：常驻 worker 已增加，但其任务路径仍停留在保存目标的接线演示；新增 goal_artifact 验证存在观察值自引用及证据复用错误；另外复现了归档规范化、数据库方法缩进和日志短写/损坏恢复问题。

本轮没有修改远端、创建 PR、触发付费模型或操作用户数据库。两份候选补丁只存在于本地副本。

## 证据和执行边界

- GitHub 连接器确认当前审计提交的 CI run `38760611305`（#191）为 `completed/success`。这是远端状态，不是本地运行数。来源：<https://github.com/Jerry2003826/aether/actions/runs/38760611305>。
- 直接 git 网络访问失败；没有成功完整克隆。通过连接器读取关键文件，在本地重建了七个选定源文件，全部核对 Git blob SHA。见 `evidence/source_checksums.json`。这不是全部仓库的审计。
- 本地 Python 3.13.5、SQLAlchemy 2.0.50、FastAPI 0.128.2、HTTPX 0.28.1、pytest 9.0.2、python-multipart 0.0.29。不是项目锁文件的完整依赖环境。
- 存储测试使用真实文件、文件锁、rename，以及文件型 SQLite 的真实提交/回滚。Database.transaction 与 ArtifactStore 的函数体未改写。SQL 方言 shim 处理 `::text`、JSON CAST、set_config 和 current_user；开启非 legacy 事务模式及外键。
- HTTP 上传测试运行实际源码提取的 upload_artifact 函数；认证和项目授权是测试替身，未初始化完整 API。
- 验证测试运行实际 `_ensure_verification` 和 `record_verification` 函数体，构造有效的本地任务/合同/产物/报告引用。Database 构造器指向本地 SQLite，数据库身份为模拟值。不验证 PostgreSQL 权限或 RLS。
- 未运行完整 PostgreSQL、Temporal、OpenHands、BGE、CodeGraph、浏览器端到端或真实模型验收。端到端缺口的结论来自入口、工作流、activity 和进程测试源码，而不是声称在这里运行过整个产品。

## 上轮问题复查

| 上轮条目 | 本轮结果 |
|---|---|
| D01 二进制上传 | 使用原始字节 SHA-256；PDF 二进制头、真实 ZIP 与 UTF-8 正常文本上传为 201；非法文本与错误 PDF 内容为 422。注意二进制头测试不是 PDF 解析质量测试。 |
| D02 日志早于数据库提交 | 实际外层事务提交前无 committed；明确回滚不复活；提交已发生但回执丢失时按库内行对账。 |
| D03 去重恢复幽灵 ID | 连续保存相同内容得到同一规范 ID；恢复不返回不存在的临时 ID，重复恢复不再加日志。 |
| D04 重复解压 | 普通 ZIP/TAR 重复导入成功；篡改现有文件后重新导入可隔离旧目录并重建。新增路径变体问题见 R2-03。 |

以上只是列出的边界场景，不是声称 PostgreSQL 并发与断电场景全部完成验收。

来源：
- [当前上传端点](https://github.com/Jerry2003826/aether/blob/55fd636fa1615314c129f5728663f3450595bc47/services/api/aether_api/app.py#L181-L223)
- [当前事务实现](https://github.com/Jerry2003826/aether/blob/55fd636fa1615314c129f5728663f3450595bc47/packages/storage/aether_storage/db.py)
- [当前产物存储](https://github.com/Jerry2003826/aether/blob/55fd636fa1615314c129f5728663f3450595bc47/packages/storage/aether_storage/artifacts.py)

## R2-01：新增 goal_artifact 验证没有独立观察产物，且复用了旧候选的证据

优先级：P1，验证可信度。状态：未修复。

位置：`services/worker/aether_worker/goal_effect.py:182–222`，以及 `research.py:84–132`。

`_ensure_verification` 向记录函数同时传入：

```python
artifact_sha256=artifact_sha,
observed_sha256=artifact_sha,
```

没有重新读取磁盘，没有核对删除状态。`record_verification` 对未过期且两个摘要相同的输入写 PASS；调用方保证两者相同，故不是独立观察。

本地结果：完整产物控制组正常 PASS；磁盘字节损坏后，ArtifactStore.get 已抛 `hash_mismatch`，该验证仍写 PASS；tombstone 后，get 已为 `not_found`，该验证仍写 PASS。这是 helper 级真实函数复现，不是 PostgreSQL/RLS 部署级利用证明。

复用查询也仅以 project/task/criterion 查第一条记录，没有绑定本次 artifact、contract 或摘要。给同一任务换一个真实产物及合同后，返回的仍是 v1 的验证 ID。对此新增的正向验收断言失败。

修复要求：把核验交给独立 verifier；从授权存储读取当前不可变候选，计算 observed 摘要，核对当前合同、候选/报告关联、删除可见性和判定标准。证据去重键要包括候选、合同、标准及其版本/摘要。没有实际检查则不能写 PASS。只把 `observed_sha256` 换成另一个由执行者填写的字段不是修复。

此发现仅针对新增 goal_artifact 路径；没有据此断言其他容器评分器都失效。

[代码来源](https://github.com/Jerry2003826/aether/blob/55fd636fa1615314c129f5728663f3450595bc47/services/worker/aether_worker/goal_effect.py#L182-L222)

## C01：常驻消费者已存在，但任务执行闭环仍未补完

优先级：P1，产品阻塞。证据等级：入口和测试代码审查；本地未运行完整服务。

正式入口 `python -m aether_worker` 调用 service.serve。注册的工作流是 DurableTaskWorkflow，活动是 materialize_goal。该活动把原始目标保存为 `{"goal": ..., "task_id": ...}`，附上一条 goal_artifact 验证记录。

新增 `test_worker_process.py` 的进程级测试是实质进步：HTTP 提交，独立启动 worker，不再靠测试函数直接调用消费者。但是 `_observe` 验收的是目标原文已存档、存在 PASS，且明确断言 `body["status"] == "QUEUED"`。停止 worker 也发生在这组结果已经可见之后，不等价于正在执行工具中途崩溃的恢复证明。

因此可以确认“自动消费及目标存档入口已补齐”，不能确认“研究/编码目标自动完成”。源码没有在该流程连接真实任务规划、检索、模型/工具循环和结果验收。修复报告对此承认不是 live 执行，故不把它说成作者伪造模型结果；问题是产品闭环仍未完成。

修复要求：继续使用既有架构，把相应任务执行器接到 Temporal activities；最小进程级测试也要用真实资料，经脚本 provider 或真正 provider、可信工具和 verifier 生成回答，而非复写需求。任务按真实结果进入 SUCCEEDED/FAILED/NEEDS_REVIEW；重启测试在有待执行/在途步骤时中断，再证明恢复及副作用去重。禁止只把 QUEUED 改成 SUCCEEDED 来让测试通过。

[入口](https://github.com/Jerry2003826/aether/blob/55fd636fa1615314c129f5728663f3450595bc47/services/worker/aether_worker/__main__.py)
[服务](https://github.com/Jerry2003826/aether/blob/55fd636fa1615314c129f5728663f3450595bc47/services/worker/aether_worker/service.py)
[工作流](https://github.com/Jerry2003826/aether/blob/55fd636fa1615314c129f5728663f3450595bc47/services/worker/aether_worker/durable_workflow.py)
[进程测试](https://github.com/Jerry2003826/aether/blob/55fd636fa1615314c129f5728663f3450595bc47/tests/integration/test_worker_process.py)

## R2-02：日志短写被当成成功，截断尾行会中断整个恢复扫描

优先级：P1，故障恢复；不是声称普通写入必然失败。状态：未修复。

位置：`artifacts.py:557–583`。

`_journal` 只调用一次 os.write，不检查返回字节数。用仅对 os.write 返回短写的故障注入，函数正常返回，但日志已是半截 JSON。下一次解析抛 JSONDecodeError。另在一条合法 staged 日志后添加不完整尾记录，调用公共 recover，同样在读取全部 JSON 时异常退出，前面完整记录没有机会处理。

这是可复现的故障处理缺口，不等于已经证明用户数据丢失。os.write 的契约返回实际写入字节数，不能假设每次都是请求长度。

修复要求：write-all 循环，处理零写入/错误；恢复需要识别并隔离不完整尾记录，保持前面完整记录可验证、可恢复；中间损坏必须单独报告，不能任意跳过。恢复处置和追加日志要协调锁，不能把末尾半行和新记录拼成一条。必要时使用长度/校验和分帧。保留原始损坏片段供审计，不以“所有异常 return []”冒充成功恢复。

[代码](https://github.com/Jerry2003826/aether/blob/55fd636fa1615314c129f5728663f3450595bc47/packages/storage/aether_storage/artifacts.py#L557-L583)
[Python os.write 参考](https://docs.python.org/3.13/library/os.html#os.write)

## R2-03：合法 ./ 路径的归档被新快照比较误拒绝

优先级：P2，输入兼容性回归。状态：已提供本地候选补丁，未推送、未做 PostgreSQL 全量验收。

位置：`artifacts.py:469–483` 和 `716–729`。

归档成员 `./src/main.py` 被允许，提取后实际路径是 `src/main.py`。payloads 的键仍为原字符串，扫描树的键却是规范相对路径，两个字典永远不同，最终抛 `snapshot_mismatch`。合法 ZIP 与 TAR 都已复现。不是路径越界，也不应靠关闭一致性检查修复。

候选补丁在读取成员时按 PurePosixPath 规范化；同一规范路径有多个文件成员时明确拒绝，避免规范化后的覆盖。遍历路径、符号链接和原有限额仍拒绝。临时目录、文件校验、原子发布流程保留。

补丁：`patches/0002-canonical-archive-member-paths.patch`。额外 6 个 guard 检查覆盖两种归档的重名与遍历、ZIP symlink 及大小限制。

## R2-04：Database.fetch_one/fetch_all 因缩进从类接口消失

优先级：P2，接口回归。状态：已提供本地候选补丁。

位置：`packages/storage/aether_storage/db.py:80–85`。

这两个方法现在是 `_release_dbapi` 的局部函数，不是 Database 的方法。调用直接抛 AttributeError。本轮 GitHub 搜索 fetch_one 仅找到声明，没有找到实际调用，因此不夸大成“所有数据库请求都会失败”；当前很多业务直接 conn.execute，CI 仍能绿并不矛盾。

补丁只把两个方法放回 Database，不改事务提交/回滚逻辑。

补丁：`patches/0001-restore-database-query-methods.patch`。

## 实际测试结果

| 检查 | 结果 |
|---|---|
| 原始选定源码，同一组 23 项定点检查 | 14 passed / 9 failed |
| 加两份候选补丁后，同一组 23 项 | 18 passed / 5 failed |
| 补丁直接针对的四个参数化检查 | 4 passed / 10 deselected |
| 额外归档安全 guard | 6 passed |
| git apply --check | 通过 |

9 个失败用例不是 9 个独立 bug。它们对应查询方法 2、归档路径 2、日志 2、验证 3。剩余 5 个失败没有隐藏、删除、标跳过或改为 xfail。C01 的产品阻塞来自源码审查，不计进本地执行数量。

命令、版本和输出见 evidence。测试中使用的有效行为期待是真正的正向断言，不是“故障出现即绿色”的反向测试。

## 合并与下一步验收

先保留当前 Git 状态，在隔离分支审查两份小补丁。只应用 patch，不用 source/ 或 patched/ 替换完整项目。随后在项目原有锁定环境、一次性 PostgreSQL/Temporal 测试服务上，把本轮最小复现迁移进正式 tests；既有单元/集成及安全测试都要保留。

优先补齐 R2-01、C01 和 R2-02。不要换调度器、记忆平台或框架，也不要降低原始目标。真正的交付验收必须展示从公开 API 接受工作目标到实际结果的自动链路，而不是仅存在文件、事件和一条 PASS。
