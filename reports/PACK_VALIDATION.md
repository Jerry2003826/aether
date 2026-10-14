# V1.4 交付包验证记录

日期：2026-10-15（Australia/Melbourne）。此记录仅适用于本次生成的设计与参考包。

## 已实际执行

```bash
python scripts/rebuild_spec.py
python scripts/verify_freeze.py --write-report
python -m unittest discover -s tests -p 'test_*.py' -v
```

三条命令均以退出码0完成。单测运行 **76项，全部通过**；其中64项继承领域参考测试、12项本版长链/边界/规格测试。
完整原始输出见 `reference-and-freeze-tests.log` 与 `pack-consistency.log`。
这些单测针对参考纯函数、数据模型和规格一致性，不是已实现应用的76项集成测试。

## 检查范围

- 28个当前任务包（26核心、1选定增强、1live），依赖无环、无缺失目标。
- 原V1.2的73项与V1.3的13项，共86项，有逐条当前目标映射；未并行执行两套任务。
- 32个API路径、49个操作具有对应实施任务；当前28个开发验收视图与backlog一致。
- JSON/YAML/JSONL可解析，内部JSON引用可解析；配置与32步依赖链限制一致。
- 只存在一份活动AGENTS.md；合并阅读版与源章节一致；3份原始资料ZIP通过CRC及路径检查。
- 新包文件清单与SHA256记录在 `reports/PACK_MANIFEST.json`，ZIP在打包后再次测试。

## 未执行、不可由上述结果推断

真实PostgreSQL迁移/RLS、Temporal与升级回放、OpenHands/CodeGraph/浏览器集成、Linux隔离、真实模型调用、BGE权重运行、实际预算扣费、供应链扫描、备份恢复、隐藏评测及Hermes能力比较，**本次均未执行**。
应用代码仍需按F00起实现；STATUS中的product_implementation保持not_started，各真实验收保持not_run。
API、SQL和配置是设计合同及示例，不是已经安装成功的依赖锁或生产安全保证。

## 本次参考检查环境

Python `3.13.5`；Pydantic `2.13.5`；PyYAML `6.0.3`；平台 `Linux x86_64`。
这是本容器的检查环境，非用户macOS/Linux目标环境兼容性证明。控制面目标Python3.13、Node24、PG17仍由F00/F01实际锁定。
