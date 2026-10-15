# AETHER Debug Round 1

本包针对 `Jerry2003826/aether@b570f9197c650212a693f34a18d3729a04772f0d`。先读 `AUDIT_REPORT.md`。

**交付：四类已复现缺陷，一个已在隔离环境验证的最小补丁，其余缺陷仍保留失败用例。无远端修改。**

## 内容

- `original/`：五个经 Git blob 哈希验证的原始源文件。
- `patched/`：同一子集，只修二进制上传指纹。
- `patches/0001-binary-safe-upload.patch`：可以审查的最小代码改动。
- `test_current_defects.py`、`test_upload_endpoint_isolated.py`、`source_loader.py`：实际运行的隔离复现代码。
- `repository_tests/`：待合并到真实项目的 PostgreSQL/API 回归测试，**未执行**。
- `evidence/`：原始本地测试日志、环境、源文件哈希、人工提取的远端 CI 摘要。
- `CODEX_REPAIR.md`：定点修复交接，不要求重新设计架构。

`original/` / `patched/` 不是完整仓库。不要用本包替换整个项目，也不要运行任何远端写入。

## 重现本轮结果

在本包所在目录、单独虚拟环境中运行，避免导入另一个已启动测试进程里的同名 AETHER 模块。需要 Python 3.13 与 `requirements-audit.txt` 中依赖（记录的是本轮环境，不是生产锁文件）。

```bash
python -m pytest -q test_current_defects.py test_upload_endpoint_isolated.py
# 预期：8 failed, 5 passed；这是故障复现，不是安装失败。

AETHER_AUDIT_SOURCE_ROOT="$PWD/patched" python -m pytest -q test_current_defects.py test_upload_endpoint_isolated.py
# 预期：3 failed, 10 passed；其他三类缺陷没有被本补丁修改。

AETHER_AUDIT_SOURCE_ROOT="$PWD/patched" python -m pytest -q test_current_defects.py test_upload_endpoint_isolated.py -k 'fingerprint or text_upload or upload_endpoint or invalid_media'
# 预期：8 passed, 5 deselected。
```

使用 SQLite 事务与真实文件，不模拟 PostgreSQL/RLS/并发保证。HTTP 测试运行实际上传函数，但认证和项目授权为测试替身。完整细节见报告。

## 在真实项目应用 D01

确认工作区基线与未提交变更，先审查 patch。下面是本地操作，不会 push。

```bash
cd /你的/aether
# 将下列路径改成本包的绝对路径。
git apply --check /解压路径/AETHER_Debug_Round1/patches/0001-binary-safe-upload.patch
git apply /解压路径/AETHER_Debug_Round1/patches/0001-binary-safe-upload.patch
```

再按 `CODEX_REPAIR.md` 合并仓库集成用例，并在真实 PostgreSQL 配置存在时运行：

```bash
AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q \
  tests/integration/test_audit_artifact_regressions.py \
  -k 'binary_upload or invalid_utf8'
```

这是要求在真实仓库执行的命令，**不是本轮已经执行的命令**。本包不携带 API Key、数据库口令或引导凭证。
