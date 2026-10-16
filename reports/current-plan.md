# R8-C 威胁情报浏览器，变基到 13bc096

任务：把 Playwright / Tor 路径接到已合并的 R8-A（`13bc096`，ADR-F41）上。ADR-F42 已被未合并的 PR #31 占用，本决定记为 ADR-F43。不改 `contracts/`、`reference/` 和 `readiness.live_ready`。

已改：

- 采集与浏览器共用 `net_guard.assert_public_target`，有 IPv4 时用 IPv4
- 子 frame 的 document 请求中止
- 访问审计写入 `audit_events`
- Tor 代理就绪后才启动浏览器；夹具模式等 8080
- `playwright==1.64.0`

结果：冻结检查通过。单元测试 324 passed。本机没有 Docker，容器测试在 `AETHER_REQUIRE_SERVICES=1` 下失败，不跳过。`74cbfd8` 的 CI run 38959600766：integration `392 passed, 4 warnings in 793.36s`，Sonar 质量门通过。`live_ready` 保持 pending。
