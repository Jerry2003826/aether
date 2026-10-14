# AETHER V1.4 · 开工冻结版

**这是主方案的统一施工包，不是另一份补丁，也不是已经跑通的完整应用。**
主干延续 AETHER V1–V1.2；V1.3 与 DeepSeek 只补强边界、测试和供应链。

## 交给 Codex
新项目：把本包解压到空项目根目录，在此目录打开 Codex，发送 `CODEX_START.md`。
已有代码：把本包放在独立临时目录，按 `docs/16_MERGE_EXISTING.md` 合并；不要覆盖已有代码、迁移、锁文件或进度。

## 从哪里读
- `docs/00_FREEZE.md`：最终裁决、开工范围、非目标。
- `MASTER_SPEC.md`：完整阅读版；各 docs 章节是维护源，更新后重新生成。
- `docs/03_repository_map.md`：仓库、取用部分、边界和适配器位置。
- `tasks/backlog.yaml`：唯一施工队列。
- `tasks/legacy_mapping.yaml`：V1.2 的73项和V1.3的13项旧任务逐条归并/延期记录。
- `contracts/`、`reference/`、`fixtures/`：接口/数据合同和非生产参考代码。
- `reports/PACK_VALIDATION.md`：本次实际执行的包检查，明确尚未完成的集成。

## 范围
先做可恢复、可授权、可验收的 Agent 内核；随后在同一内核上完成研究、编码、记忆、MCP、跟踪推荐和技能实验。
默认原生记忆、静态抓取、Playwright、OpenHands；不安装几个同类平台让 Codex 再选。
CodeGraph 是计划实施的增强能力：独立启用门、失败可降级。没有它仍必须能编程。

## 现在可运行的仅是包检查
```bash
python scripts/verify_freeze.py
python -m unittest discover -s tests -p 'test_*.py' -v
```
需要 Python 3.11+、Pydantic 2 和 PyYAML；这些是检查器依赖，不是应用的生产锁文件。
应用初始化/Compose/模型探针由 Codex 按 F00 起实现。`archive/` 中旧资料不参与任务自动发现。
