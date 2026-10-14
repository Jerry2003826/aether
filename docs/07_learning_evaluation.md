# 07 · Skill Evolution Lab 与可信评测

## 1. 学什么，不学什么

V1不更新基础模型权重，不自修改control plane。学习对象是有适用范围的操作技能、检索策略配置和可解释的工具workflow；不接受任意代码自动进入高权限路径。用户反馈帮助发现候选，但最终是否更好由独立任务结果决定。

可学习的例子：某类依赖升级先检查lockfile；针对某类检索任务保留原query再改写；某类失败需要运行一个已批准的诊断工具。不可学习的例子：绕过审批、放宽验收、删除失败日志、把测试答案抄进skill。

## 2. Skill bundle

```text
skill-name/
  SKILL.md              # standard YAML frontmatter + human-readable procedure
  aether.skill.json     # scope, version, required capabilities, hashes, eval metadata
  references/           # reviewed supporting material
  scripts/              # optional; disabled until explicitly reviewed and hash-approved
```

按标准name/description规范校验。manifest记录parent version、training trace IDs、compatibility、tool schema hashes、test family、allowed scopes、max_step/tool budget、risk class和bundle hash。manifest里的权限是**请求**，不是自动授权。外部skill导入一律quarantine。

## 3. 候选生成和去污

只读取已授权、已脱敏、验收有结论的trace。成功例和失败例均保留原因；agent自述成功不算成功轨迹。训练数据和测试数据按任务来源/模板family/时间切分，避免同一仓库补丁变体同时在训练和验证里。

学习器输出CandidateSkill+具体假设+预计适用域+反例。进行secret检查、许可证来源检查、重复/冲突检查、过宽权限检查、拒绝包含答案/用户私人原文片段的skill。每批最多3个候选、学习预算独立受限；没有足够证据则什么都不更新。

## 4. Promotion 状态机

`DRAFT -> QUARANTINED -> STATIC_PASSED -> EVALUATING -> ELIGIBLE -> CANARY -> ACTIVE`

失败状态 `REJECTED`；已部署可 `ROLLED_BACK`。STATIC_PASSED不意味着效果更好。ELIGIBLE需要独立evaluator产物；CANARY默认需要owner确认。在线任务运行固定skill版本，部署切换只影响新run；旧版始终可回滚。

## 5. 评测门槛（初始研发策略，不是已取得结果）

候选必须先过全部安全/契约回归，然后在至少30个独立task family代表任务上与父版本配对比较。每个任务同model/config/toolset、重置环境、相同预算。对family做配对bootstrap置信区间；多个seed是同一任务的重复测量，不能冒充更多独立样本。

初始自动eligibility：成功率提升>=5个百分点、paired95%CI下界>0、成本比<=1.10、无新的安全失败、mandatory任务无回归。样本不足/CI跨0则INSUFFICIENT_EVIDENCE，不用“多数票”硬升。阈值版本化，改阈值不得重用同一holdout挑结果。人可选择试验性canary，但UI显示未证实提升，不能标“已学会”。

对于零成本scripted测试，成本比没有商业意义，只验证管线。离线重放是测试策略或解析的一种方法，不能替代真实执行的skill有效性比较。

## 6. Canary 与回滚

只在预批准的低风险新任务上启用5%（可设0）的canary。风险类不匹配不分流。保存task到version的分配、时间、selector policy和结果。发现任一安全失败立即停用；连续功能退化或成本越界按预先写入的窗口规则暂停并回滚。历史ACTIVE指针与bundle hash可原子切回，已运行任务保持原版本或显式取消。

不要把不断窥视线上小样本当严格统计显著。门槛用于工程保护，发布优越性需要另行冻结实验。

## 7. Hermes 对照实验设计

四组：Hermes cold、Hermes learned/frozen、AETHER cold、AETHER learned/frozen。每组使用独立home/workspace、同一基础模型及provider revision、相同数据/工具能力和输出token/成本限制；记录任何无法匹配的差异。基线也有机会使用自身学习功能，不能只让AETHER见训练样本。

模型路由先关闭，防止用更贵更强模型冒充架构优势。然后可以另做cost-aware router实验，明确是另一个比较。训练阶段预算同时报告；技能推理token也算成本。衡量：objective task success、false completion、恢复正确性、unsafe attempts blocked、实际unsafe effects、p50/p95时延、总token/费用、每成功任务成本。

学习消融：关memory、关skills、关verification辅助、无rerank、无经验选择器。消融不能关闭基础安全权限；安全不是可牺牲的实验变量。

## 8. 数据集隔离

本包 `evals/dev_cases.jsonl` 是**公开开发验收场景**，Codex可以看，不能叫heldout。产品提供独立evaluator加载release dataset的接口，但真实最终集应由owner或独立流程在冻结release后提供/封存，存在代码/actor容器之外。训练/学习器不给它文件系统访问权。

合成隐藏标签若生成器/seed/答案已让开发Agent看到，不等于真正未知任务。发布报告记录dataset hash、family split、版本、评测次数、是否窥视；同一holdout用作选型后就不再是最终holdout。

没有最终集时只生成实验工具和开发验收报告，不捏造“提升20%”。上一轮提到的成功率/成本目标是愿望，不是对本项目的先验事实。

## 9. 统计与结果格式

报告包含每任务结果、实际验证证据、失败类型、费用known/unknown、模型请求数、wall time、approval wait time、timeout和abstention。abstention不算成功但不等于false completion。false completion分母写清：所有声称完成的任务或全部任务，两个口径都可给但不能混用。

至少同时给raw counts与CI。0次失败不证明失败率为0；小样本不能保证1%以下。比较优越性只对测试分布成立。失败任务和unknown账单不可以从均值中偷偷删掉。


## 10. CodeGraph 消融实验（新增）

在同模型、同代码快照、同预算与验收集下做 CodeGraph On/Off 消融，比较客观任务完成率、token、成本、时延、文件读取数量与遗漏关键调用路径。冷启动索引工作单独计入成本，warm 查询重复测；失败/timeout照计。项目官网的节省比例仅作为假设，不能直接复制进 AETHER 的结果。额外记录 residual context tokens：CodeGraph 官方自测提醒长会话残留上下文可能更多。见 `F15`。

## 11. 冻结决定：管线完成不要求实验结果必须为正
V1采用普通模型生成最多3个受限候选，再由独立pydantic-evals/可信验证器调度测试用例，结果交SkillLab判断。pydantic-evals不参与产品主任务调度。DSPy/GEPA和其他进化框架延期，初版不训练权重。
所有候选因样本不足或退化被拒绝，是正确运行结果；不得为演示“学会了”降低阈值。工程验收要求拒绝、eligible校验、人工canary和回滚机制真正工作；合成分数只测试状态机，并显著标注，不作为真实收益。30个family及现有阈值是初始研究协议，不是统计充分性的普遍保证；真实实验仍需按方差/最小效应做功效分析并限制反复试验。
