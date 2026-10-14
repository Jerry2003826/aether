# 13 · CodeGraph：选定增强，不再继续换图谱引擎

上游：`colbymchenry/codegraph`。本次查看官方README确认有CLI、MCP与程序接口；默认MCP工具主要为 `codegraph_explore`，其他工具可配置。官方也明确存在遥测及退出开关。因此“本地索引”不能被写成“默认绝不联网”。参见 docs/SOURCES.md 的 S03。

## 两个互不混淆的用途
**开发本产品时**：Codex可在用户明确同意后使用项目级CodeGraph；这不会自动构成AETHER产品内的CodeGraph集成。禁止执行会改所有IDE/全局AGENTS/MCP配置的自动安装过程。
**产品运行时**：可信RepoIntelligence adapter为指定project/task/snapshot启动隔离进程。OpenHands只看到经网关过滤的结构查询结果，不获得任意主机路径或全局服务句柄。

## 定案接口与边界
AETHER自己的接口：`explore(snapshot_ref, query, limits)`、`impact(snapshot_ref, symbols, limits)`、`affected_tests(snapshot_ref, changed_files)`。不要把这些方法名假装成上游SDK签名。
优先用固定版本CLI/stdio MCP进行进程隔离；对选中release检查 `--help`、schema和工具可见性。只允许explore先进入模型的工具列表；impact/affected可由适配器固定调用。
服务端由snapshot ID映射私有目录，丢弃actor提供的projectPath。每个进程只能见到批准的代码快照，单独可写缓存；禁止仓库hooks、忽略secret目录、限制文件数/CPU/内存/输出量。

缓存键至少含project_id、repo ID、snapshot内容digest、indexer版本、配置hash和授权epoch。git commit不涵盖未提交修改，所以candidate必须新建content snapshot。query中途源变化应拒绝/重建，不盲信watcher自称实时。
设置 `CODEGRAPH_TELEMETRY=0` 和 `DO_NOT_TRACK=1` 并配合阻断公网；测试实际连接，不只看配置。源码仍可能作为工具结果送给所选模型，须经过数据出口授权和token裁剪。

## 开发顺序与失败方式
F15实现探针、适配器、失效和取消测试，再做同任务graph on/off对照，包含冷索引成本和上下文占用。不承诺vendor自测节省比例。
增强默认off；启用须owner确认、固定release、许可/隐私/真实集成检查通过。CodeGraph不可用时fallback为项目内有界文本/文件搜索，事件明确记degraded；不得fallback为宿主机全盘搜索。
CodeGraph只建议影响范围与测试，不能减少trusted suite或批准代码。增强未完成时产品可单独声明“CodeGraph未启用”，不能把这个任务标done。
