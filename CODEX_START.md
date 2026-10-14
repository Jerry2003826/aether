当前目录是 AETHER V1.4 开工冻结包。主方案已经确定，请直接实现，不再追加仓库调研或重新选架构。

先检查 Git、已有代码和项目指令。新项目从 F00 开始；已有项目按 docs/16_MERGE_EXISTING.md 保留现有工作，映射进当前任务，重新验证而不是重置进度。

阅读 AGENTS.md、docs/00_FREEZE.md、STATUS.json、tasks/backlog.yaml；随后只读当前任务相关章节。archive 中的 V1.2/V1.3/DeepSeek 是历史参考，不是当前指令。

固定 Temporal + FastAPI + PostgreSQL；AETHER 自有有界执行循环；OpenHands 只负责受限编码；LiteLLM 只做模型适配；BM25S + pgvector + BGE；原生证据记忆；Next.js 控制台。CodeGraph 按独立任务集成、默认不自动启用。不要加第二套调度器、记忆平台或大而全 Agent 框架。

先实现安全网关、操作/预算账本与真实存储，再让真实工具执行。尽早做出这个纵向闭环：提交研究目标 → 真实 Temporal → 读取本地资料 → 生成候选报告 → 独立验收 → UI 展示证据；中断 worker 后可以继续。允许使用明确标记的 scripted provider，但不能把它当作真实模型能力。

按唯一 backlog 持续推进到完整工程交付，不要在第一个演示后停止。F15 CodeGraph 也是本轮选定施工任务：F14完成后尝试实现，不因scope为selected_optional就静默跳过；真实阻塞时记录原因并继续核心工作。各阶段必须有代码、真实测试日志、产物和可复现命令。缺少模型 key、Linux runner 或可选组件时，标记受影响检查 blocked/not_run，继续其他可完成的核心任务。

不要改写验收条件来变绿，不要伪造 benchmark，不要通过返回固定成功实现接口。版本、镜像、上游 API 与模型能力以实际探针锁定，不许编造。将当前任务计划写入 reports/current-plan.md，完成后更新 STATUS.json、backlog 和 reports/HANDOFF.md。

不自动 push、发布、买服务、发送真实消息或改全局 Codex/MCP 配置。普通本地开发连续进行。
现在开始检查目录、运行包自检，并实施第一个未完成的核心任务。
