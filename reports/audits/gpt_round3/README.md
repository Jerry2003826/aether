# AETHER Debug Round 3 — 4ba3b2f

这是第三轮审计和复现材料，不是完整项目，也不是已修复版本。当前固定源代码保持未修改：42 项检查，32 passed / 10 failed；五类待修问题。

先读 AUDIT_REPORT.md。交给 Codex 的指令在 CODEX_REPAIR.md。

## 运行本地隔离检查

需要 Python 3.13（SQLite 非 legacy 事务支持），按 requirements-audit.txt 在独立环境安装依赖。不要改变项目本身的锁文件。

```bash
python verify_sources.py
python -m pytest -q test_artifact_audit.py test_patch_guards.py \
  test_upload_endpoint_isolated.py test_round3_tasks.py test_round3_journal.py
```

当前代码预期会返回非零退出码，因为回归要求尚未满足；不能把它当安装失败。也不能把预期失败改成 xfail 来宣称修复。

源文件逐个验证 Git blob SHA；AST 只编译原函数，注入 SQLite 和依赖边界。文件 I/O 真实执行，但没有真实 PostgreSQL、角色/RLS、Temporal、OpenHands、CodeGraph、BGE 或模型调用。

repo_tests/test_audit_round3_postgres.py 可作为项目集成测试候选：只语法检查，未运行。复制到真实仓库 tests/integration 后按其真实环境运行，不要在本包内把这份候选当已执行证据。

evidence/all_checks.txt、round3_results.xml、test_summary.json 是最终 42 项结果；new_regressions.txt 是追加研究目标反例前的中间结果（16 项），storage_controls.txt 为存储控制子集（20 项），两者不额外累计。

source/ 只有七个固定文件，禁止覆盖整个项目。没有远端修改，没有生产修复补丁。
