# AETHER Debug Round 2 — 55fd636

定点复审包，不是完整应用，也不是一个全修复版本。

- AUDIT_REPORT.md：四类新发现的问题、上一轮修复复查、产品闭环缺口与证据边界。
- CODEX_REPAIR.md：实施交接。
- source/：七个选定源文件，与审计提交 Git blob SHA 对齐。
- patched/：只加入两份局部候选修复的副本。
- patches/：独立 patch 及合并 patch。不覆盖整个项目。
- test_*.py / audit_harness.py / source_loader.py：隔离复现与候选修复检查。
- evidence/：实际测试日志、版本、校验值和远端 CI 元数据。

本地没有 PostgreSQL/Temporal/真实模型。SQLite 采用非 legacy 事务模式并启用外键；上传认证为测试替身，验证函数的数据库构造器被导向本地库。不要称为生产端到端验收。

## 重放

在独立目录、Python 3.13 环境安装 requirements-audit.txt，然后：

```sh
python -m pytest -q test_artifact_audit.py test_upload_endpoint_isolated.py test_verifier_audit.py --tb=short
# 审计快照已知：14 passed、9 failed，退出码 1；失败不得隐藏。

AETHER_AUDIT_SOURCE="$PWD/patched" python -m pytest -q test_artifact_audit.py test_upload_endpoint_isolated.py test_verifier_audit.py --tb=short
# 两个候选补丁已知：18 passed、5 failed，仍未全修复。

AETHER_AUDIT_SOURCE="$PWD/patched" python -m pytest -q test_patch_guards.py
# 6 passed。
```

测试不会调用真实模型或外部 API。artifact 的 PDF 输入只是二进制上传边界，不是内容解析或 PDF 渲染。

## 应用候选补丁

先审查 CODEX_REPAIR.md，在当前项目隔离分支操作，保留用户改动：

```sh
git apply --check /path/to/patches/round2-candidates.patch
git apply /path/to/patches/round2-candidates.patch
```

需要再运行项目自身的锁定环境测试。不要将 source/ 或 patched/ 作为整个仓库覆盖。未修改任何 GitHub 远端。
