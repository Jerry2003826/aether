# 11 · 唯一施工顺序和完成定义

当前只有F00–F27这套队列；其中F15为选定增强、F26为live验收，其他是核心工程任务。不是28个独立大平台，也不是新的工期承诺。

| 阶段 | 任务 | 可见交付 |
|---|---|---|
| G0 | F00–F01 | 实际工程/核心依赖可启动；SDK与ML独立探针 |
| G1 | F02–F06 | 存储/身份/产物/网关/预算/审批；真实副作用前先完成 |
| G2 | F07–F09 | 真实Temporal任务→工具→独立基础验收→最小UI，故障恢复 |
| G3 | F10–F12 | 授权采集、真实混合检索、上下文与原生记忆 |
| G4 | F13–F15 | 受限OpenHands编码、独立测试；CodeGraph单独增强 |
| G5 | F16–F21 | 遗忘/MCP/跟踪/推荐/技能候选与可拒绝的学习闭环 |
| G6 | F22–F27 | 完整UI/安全/对照工具/备份发布/live矩阵 |

依赖是DAG，不是一条把所有工作锁死的链。F01阻塞时继续F02–F09；没有CodeGraph不阻塞其他核心；没有付费key仍可完成不依赖live的工程和测试。当前阶段过关后继续下一个未阻塞任务，不自动停止于demo。F15在F14完成后安排实施；selected_optional表示不阻塞核心发布，并非默许永远跳过。真实受阻时明确记录原因、未启用状态和恢复条件。

## 目标命令（由Codex实现）
`make doctor bootstrap dev demo-offline test-unit test-integration test-security test-e2e demo-live acceptance-report backup restore`。
本包目前只有check-pack/test-reference，不能把未实现目标命令当作已存在脚本。所有命令失败非零退出，不能吞异常。restore为显式高影响本地操作，默认不覆盖现有数据。

## 交付边界
内核工程：F00、F02–F09真实通过；产品工程：所有scope=core验收通过；live_ready：产品工程+F26通过；CodeGraph_enabled：F15通过及owner启用；comparative_evidence：独立真实实验有记录，不由任务done自动推断。
产品工程可包含明确scripted模型的E2E，但数据库/Temporal/本地工具/SDK/ML等相应集成不能全mock后标通过。没有真实权重/runner的平台用例必须blocked；小模型/CPU可用性按实测而非硬件印象。
工程版交付包允许live_not_run，但必须明确不是已验收的live V1。未完成F15可以交付核心产品，同时标CodeGraph未启用，不能勾已接入。

## 三段产品演示
研究：导入真实中英资料，执行混合检索并生成带来源版本和引用位置的比较报告，缺证据处明确标注。
编码：授权仓库快照→OpenHands产生patch→独立测试workspace验收；中断后不能重复创建job或使用旧验收。
升级注意（2026-10-17，ADR-F42 修订）：编码作业的 objective 从 operation 参数读出并原样交给沙箱 Agent。升级前已经排队、但 operation 参数里没有 `objective` 的旧作业，升级后会以 `objective_missing` 失败，不会退回旧的 demo 提示；需要用同一作业键带上 objective 重新提交。没有 Agent 改动（补丁为空）的作业判 `agent_no_change` 失败，候选文件本来就通过也不算成功。
学习：可追踪训练trace→候选→独立评测→拒绝/证据不足/eligible→人工canary/回滚。合成分数只证明机制；没有正收益候选是允许的真实结论。
跟踪/推荐作为产品功能验收：无变化不调用总结模型；所有推荐有实际特征理由且能清除画像。

## 测试证据与发布
每条结果包含check_id、task_id、类别、命令、cwd、git commit、工具/镜像/模型/dataset版本、退出码、时间、日志、产物hash、status和原因。`not_run/blocked/skipped/failed`均不等于passed。
所有最终成功状态必须来自当前不可变候选的可信验证。发布report应从实际JUnit/JSON记录生成，并列出未测平台、unknown账单、缺失live检查和可选功能状态。
安全/角色/SSRF/网络/预算/幂等/恢复/回滚/删除竞态必须有实际集成证据，不能以纯函数参考测试代替。
测试场景公开给Codex是开发集，不叫holdout。Hermes与AETHER使用相同模型/权限/预算，双方可训练并冻结；无法相同处记录。提供driver是工程成果，跑出领先是研究成果，两者严格分开。
