# 冻结决策记录

| ID | 裁决 | 原因/验证 |
|---|---|---|
| ADR-F01 | 主方案延续AETHER；DeepSeek只补充 | 用户指定；不同调度栈不拼装 |
| ADR-F02 | Temporal唯一执行权威；PG权威领域账本 | 防止重复调度；F06/F07故障测试 |
| ADR-F03 | 安全准入先于实际工具，UI提前至纵向闭环 | 纠正旧阶段顺序；F04–F09 |
| ADR-F04 | 一个typed planner，最多2读并行/1编码，无递归委派 | 控制复杂度和成本 |
| ADR-F05 | DAG依赖路径默认32，而非旧默认3 | 正常长链不是Agent嵌套；显式depth3仍受测 |
| ADR-F06 | 原生PG记忆默认，高级Hindsight延期 | 先完成来源/删除/纠正，不把记忆选型当开工前置 |
| ADR-F07 | HTTPX/Trafilatura+Playwright；其他浏览器框架延期 | 每类只有一个执行路径，减少复合重试/隐私面 |
| ADR-F08 | CodeGraph选定opt-in，schema/telemetry真实探针 | 不是全局安装；不可降低测试；F15 |
| ADR-F09 | SkillLab可全部拒绝候选 | 不制造正收益，不改变验收去演示进化 |
| ADR-F10 | core/live/optional/research分别完成 | 开工不等待最终holdout或付费key；结论不混用 |
| ADR-F11 | 不同时执行旧86项与新28项 | 全量映射，旧ZIP归档不自动加载 |
| ADR-F12 | 不把public SaaS/多租户/付费/语音列P0 | 单owner多项目产品边界明确 |
| ADR-F13 | 包自检跳过 `.venv`、`node_modules`、`.git`、`.next`、`data`、`secrets`、`__pycache__`、`.cache` | 2026-10-15：安装控制面后 LiteLLM/Temporal 自带 `AGENTS.md`，`jsonschema` 的 `issue.json` 含外部 `$ref`。依赖树不是本包合同；仓库内规格文件仍全量检查。Hugging Face 权重缓存里有空的 `added_tokens.json`，同样不属于规格 |
| ADR-F14 | `aether_api` 不再获得全表 DML，新表默认不继承写权限 | 2026-10-15：Codex P1。权威账本写操作留在 worker/verifier/dispatcher；非 loopback 绑定还要求 TLS 证书与 Secure cookie。测试见 `tests/integration/test_privileges.py` 与 `tests/unit/test_bind.py` |
| ADR-F15 | 词法分词使用冻结的 jieba 0.42.1 词典做正向最大匹配，不把词典提交进仓库 | 2026-10-15：jieba 0.42.1 只有源码包。CI 的 `uv sync --locked --no-build` 装不上它。运行时使用包内 `dict.txt`，缺失时按 `config/tokenizer/jieba-0.42.1.json` 的 URL 下载并核对 sha256。英文和 `!=` 这类代码符号另切，停用词表为空，避免删掉 `不` 和 `not` |
| ADR-CG1 | CodeGraph 固定 `@colbymchenry/codegraph@1.6.2` 与 linux-x64 包的 npm sha512；CLI 只在 `--network none` 的容器里跑；产品默认关闭 | 2026-10-15：GitHub runner 上的非特权 `unshare` 不能当隔离证据。容器镜像摘要是 `debian@sha256:7c7b2c966bc9ee8cedfeef67e0e279108992c77681fa595db4a9d65c06ccc587`。不执行 `codegraph install`。快照副本丢弃 actor 路径、secret 目录和越界符号链接。图谱建议不能改可信套件。不可用时只在快照内搜索，不声称厂商节省比例 |
| ADR-CG2 | 所有者确认后打开已安装锁，示例和功能矩阵仍默认关闭 | 2026-10-15：启用不等于新检出自动打开。`codegraph.example.yaml` 与 `feature_matrix.yaml` 的 `enabled` 保持 false。锁文件 `product_enabled` 为 true。遥测仍是 0，容器仍是 `--network none`。`readiness.codegraph_enabled` 只在 `reports/f15_product_result.json` 的检查全部通过后改为 passed |
| ADR-F16 | 删除先做 tombstone 并增加 `visibility_epoch`，派生索引跟着失效 | 2026-10-15：worker 只能更新 `projects.visibility_epoch` 这一列。晚到的向量和记忆文本写入必须看见同一个 epoch，而且行还没有 `deleted_at`。备份和 Temporal 历史不在这次删除里消失 |

实施时确需改架构：追加日期、具体失败证据、最小变更、风险、迁移与新增/修订测试。不因个人偏好换栈；不得删除失败记录。
