# F25 验收与测试

每条验收对应一个会真实执行的测试。没有 skip。空漏洞列表不是已审阅。

| 验收 | 测试 |
|---|---|
| SBOM 来自锁文件；漏洞库离线且 not_run；脚本里没有 `down -v` | `tests/unit/test_supply_chain.py::test_sbom_uses_the_lockfile_and_does_not_call_an_empty_list_reviewed` |
| 恢复必须确认 | `tests/unit/test_supply_chain.py::test_restore_requires_confirmation` |
| 恢复后同一事务写回 tables.json，并把 PENDING/DELIVERING 改成 STOPPED；测试从数据库读回状态，不再派发；产物先复制到临时目录再改名换上 | `tests/integration/test_backup_restore.py::test_restore_stops_commands_and_writes_snapshot_rows_back` |
| 复制失败时不删掉现有产物目录 | `tests/unit/test_supply_chain.py::test_artifact_replace_keeps_the_live_tree_when_the_copy_fails` |
