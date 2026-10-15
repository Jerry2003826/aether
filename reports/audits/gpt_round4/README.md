# AETHER Debug Round 4

只读复审快照：Jerry2003826/aether @ `07cc2ae4b0f864618da44cee7df815aef223f7e9`。

- `AUDIT_REPORT.md`：五类问题、旧回归状态和证据边界。
- `CODEX_REPAIR.md`：真实仓库修复交接。
- `test_round4_controls.py`、`test_round4_regressions.py`：本轮21个控制/旧回归case和12个预期安全断言失败case。
- 另外三份测试文件：25个之前的存储/上传检查。
- `source/`：九个原始源文件，全部与 GitHub blob hash 一致；不是完整仓库，**不要覆盖你的项目**。
- `task_harness.py`：SQLite/日期/SQL方言适配及模型传输替身；不证明 PostgreSQL/RLS 或并发锁。
- `repo_tests/`：8个真实仓库 PostgreSQL 测试候选，本轮仅语法检查，未执行。
- `evidence/`：原始 pytest 输出、JUnit、版本、源码校验和远端CI元数据。

```sh
# Python 3.13.5 是本轮实际版本；依赖安装到隔离虚拟环境，不修改宿主系统。
python -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-audit.txt
python verify_sources.py
python -m pytest -q --tb=short
```

本地最终运行：58 collected，46 passed，12 failed，0 skipped / errors。当前原始源码的失败是本包要展示的缺陷，不能隐藏成绿色结果。测试不会发出真实模型网络请求。

这不是已经修好产品的补丁，也不是本轮全量 PostgreSQL/Temporal/真实模型验收。远端 GitHub CI #220 success 与这些定点检查是两种不同证据。
