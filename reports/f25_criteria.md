# F25 验收与测试

每条验收对应一个会真实执行的测试。没有 skip。空漏洞列表不是已审阅。

| 验收 | 测试 |
|---|---|
| SBOM 来自锁文件；漏洞库离线且 not_run；脚本里没有 `down -v` | `tests/unit/test_supply_chain.py::test_sbom_uses_the_lockfile_and_does_not_call_an_empty_list_reviewed` |
| 恢复必须确认 | `tests/unit/test_supply_chain.py::test_restore_requires_confirmation` |
| 恢复后停掉未完成投递，产物字节回来，任务不被标成成功 | `tests/integration/test_backup_restore.py::test_restore_stops_dispatch_and_puts_artifact_bytes_back` |
