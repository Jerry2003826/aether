# F15 验收与测试

每条验收对应至少一个会真实执行的测试。没有 skip。`config/codegraph.example.yaml` 和 `config/feature_matrix.yaml` 仍是关闭的安全模板，不会自动启用。所有者确认后，已安装的 `config/codegraph.lock.json` 把 `product_enabled` 打开。遥测保持 `CODEGRAPH_TELEMETRY=0`，CLI 仍在 `--network none` 的 Debian 容器里。探针安装 npm 上核对过 sha512 的 `@colbymchenry/codegraph@1.6.2` 和 `@colbymchenry/codegraph-linux-x64@1.6.2`。没有执行 `codegraph install`。

对本仓库的真实索引和查询：

```sh
uv run --locked --no-build python scripts/run_codegraph_product.py --out reports/f15_product_result.json
```

需要本机 Docker，以及能 `npm pack` 到 registry.npmjs.org 的网络。镜像是锁文件里的 `debian@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587`。快照只含 `git ls-files`，跳过 `.env`、`secrets`、`.venv`、`node_modules`。`readiness.codegraph_enabled` 只有在这份结果的 `checks` 全部为 true 时才是 `passed`。

| 验收 | 测试 |
|---|---|
| 固定 release，核对包哈希，遥测关闭，公网被阻断时本地查询仍成功 | `tests/integration/test_codegraph.py::test_fixed_release_telemetry_and_blocked_network` |
| 索引的是快照副本而不是宿主路径；改码后哈希失效 | `tests/integration/test_codegraph.py::test_indexes_snapshot_copy_and_hash_expires` |
| 跨项目拒绝；actor 路径不传给 CLI | `tests/integration/test_codegraph.py::test_cross_project_rejected_and_actor_path_discarded` |
| 取消会清掉快照；不可用时只在快照内降级搜索 | `tests/integration/test_codegraph.py::test_cancel_clears_snapshot_and_fallback_stays_inside` |
| 冷索引耗时和上下文长度被记录；图谱开和关都能返回结果；不声称厂商节省比例 | `tests/integration/test_codegraph.py::test_cold_index_and_graph_on_off_do_not_claim_vendor_savings` |
| 不改动、不减少可信测试；affected 只是建议 | `tests/integration/test_codegraph.py::test_trusted_suite_unchanged_and_affected_is_advisory` |
| 所有者开关打开后仍断网、关遥测、不信任图谱建议；拒绝 `codegraph install`；文件数上限和越界符号链接会失败 | `tests/integration/test_codegraph.py::test_product_gate_rejects_install_and_limits` |
| 调用方和被调用方与夹具源码一致 | `tests/integration/test_codegraph.py::test_callers_and_callees_match_the_fixture_source` |
| 示例模板保持关闭；锁文件才是所有者开关 | `tests/unit/test_codegraph_status.py::test_example_stays_off_while_the_lock_is_the_owner_switch` |
| AST 不承认的调用方不能算通过；没有真实结果就不能把 CodeGraph 标成已启用 | `tests/unit/test_codegraph_evidence.py::test_edge_agreement_rejects_a_caller_the_ast_does_not_have` |
