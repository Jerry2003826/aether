# SYS-01 后续：变基到含 PR #26 的 main

任务：把 PR #25 变基到 `cfbc65d`。ADR-F34 是 GitHub/arXiv 采集、工具账本、来源删除缓存和只读推荐。ADR-F35 是任务流程复核（PR #28）。ADR-F36 至 ADR-F38 是编码作业和技能评测（PR #26）。本决定改记为 ADR-F39。

相关合同：不改 `contracts/`、`reference/`、验收标准或 `STATUS.json`。`evaluateSkill` 与 `createCodingJob` 仍按 ADR-F36 至 ADR-F38 受理并跑到可查询终态。不改变 `live_ready`。

两边都留下的行为：

- URL、RSS、`github_api`、`arxiv_api` 都按 `approved_config.allowed_domains` 做精确主机名比对。空名单拒绝这些远程抓取。URL 与 RSS 的重定向继续比对。
- `github_api` 与 `arxiv_api` 仍只连接官方主机，或在夹具开关和 `AETHER_INGEST_FIXTURE_HOSTS` 同时满足时连接夹具主机。不跟随重定向，不跟随 `download_url`。非官方主机仍是 `source_host_rejected`。
- `local_document` 仍读本项目产物。抓取失败仍进入 `fetch_failed`。已激活的跟踪仍可暂停，并在激活时写入 `approved_by`。
- 工具仍经已审核 manifest、MCP 和操作账本执行。来源删除仍递增 `visibility_epoch` 并丢掉检索缓存。推荐 GET 仍不写画像和曝光。
- 词法检索仍不调用编码进程。非空 `source_ids` 仍在截断之前过滤。
- 编码作业和技能评测仍跑到可查询终态。取消仍要确认容器消失。TTL 仍不回收持有租约的作业。围栏、预算、基线和终态事件仍按 ADR-F38。
- NEEDS_REVIEW 仍有所有者出口。worker 不以所有者名义自动批准。执行前必须有所有者的 `approve_goal`。
- active elapsed 只累计 RUNNING 与 VERIFYING。排队、审批、暂停和复核不计入。
- 终态结束本次 workflow run，并在历史达到 5000 或服务端建议时 continue-as-new。工作流 ID 复用是 `REJECT_DUPLICATE`。投递有租约。审批失败写入真实状态，不同载荷仍是 409。超额可对账。已成功任务的答案和验收报告删除返回 409 `evidence_retained`。

拟改路径：`docs/DECISIONS.md`（ADR-F39）、由此重建的 `MASTER_SPEC.md`、`reports/HANDOFF.md`、`reports/current-plan.md`。代码路径已在本提交：`pending_jobs.py`、`ingest.py`、`remote_sources.py`，以及覆盖白名单的采集测试。

测试：不 skip、不 xfail、不删断言、不放宽断言。变基后重跑 `verify_freeze`、参考套件、单元测试，以及 `AETHER_REQUIRE_SERVICES=1` 的集成测试。

进度：冲突已按两边行为合并，决定是 ADR-F39。三条 P2 已接上：PDF 按媒体类型抽正文，停止的 schedule 确认删除后不再轮询，过滤检索只给允许的位置打分。变基到 `cfbc65d` 之后的本机结果还没重跑，不把上一头的数字记成这次的结果。不改 `live_ready`。
