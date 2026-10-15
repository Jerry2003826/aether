# F15 验收与测试

每条验收对应至少一个会真实执行的测试。没有 skip。产品默认仍然关闭。探针安装 npm 上核对过 sha512 的 `@colbymchenry/codegraph@1.6.2` 和 `@colbymchenry/codegraph-linux-x64@1.6.2`。CLI 跑在 `--network none` 的 Debian 容器里。没有执行 `codegraph install`。

| 验收 | 测试 |
|---|---|
| 固定 release，核对包哈希，遥测关闭，公网被阻断时本地查询仍成功 | `tests/integration/test_codegraph.py::test_codegraph_release_snapshot_network_and_fallback` |
| 索引的是快照副本而不是宿主路径；改码后哈希失效 | `tests/integration/test_codegraph.py::test_codegraph_release_snapshot_network_and_fallback` |
| 跨项目拒绝；actor 路径不传给 CLI | `tests/integration/test_codegraph.py::test_codegraph_release_snapshot_network_and_fallback` |
| 取消会清掉快照；不可用时只在快照内降级搜索 | `tests/integration/test_codegraph.py::test_codegraph_release_snapshot_network_and_fallback` |
| 冷索引耗时和上下文长度被记录；图谱开和关都能返回结果；不声称厂商节省比例 | `tests/integration/test_codegraph.py::test_codegraph_release_snapshot_network_and_fallback` |
| 不改动、不减少可信测试；affected 只是建议 | `tests/integration/test_codegraph.py::test_codegraph_release_snapshot_network_and_fallback` |
| 产品默认关闭；拒绝 `codegraph install`；文件数上限和越界符号链接会失败 | `tests/integration/test_codegraph.py::test_product_gate_rejects_install_and_limits` |
