# 05 · Knowledge Fabric、搜索、记忆与推荐

## 1. 知识对象与来源

每份资料分成 `Source -> SourceVersion -> Chunk`。Source 是稳定入口，SourceVersion 保存抓取时间、内容时间（未知则 null）、原始/规范化内容 hash、语言、解析器版本、可见项目；Chunk 保存 canonical text 的 code-point offset、页号/段落标识和向量模型版本。摘要不能替代原文证据。

每条记忆是 `MemoryRecord`：kind（semantic/episodic/procedural）、category（fact/preference/procedure/episode/project_decision）、claim、epistemic_status（proposed/source_claim/user_confirmed/observed_test/inference/superseded）、来源证据、scope、valid_from/valid_to、recorded_at、置信等级及其产生方式。冲突由conflict_group标记，删除由deleted_at表示，不把来源可信度和生命周期混成一个字段。置信等级是启发式，不假装经统计校准的事实概率。

用户明确陈述、网站声称、Agent 推断、实际测试结果分开。冲突不覆写：产生 conflict group，保留双方证据与时间，让用户或任务消解。重要项目决策优先显式确认；敏感个人信息不自动跨项目共享。

## 2. 采集链路

```text
approved SourceConfig
  -> quota / robots / source permission / HTTP safety checks
  -> conditional GET (ETag / Last-Modified when available)
  -> immutable raw artifact + metadata
  -> isolated parser -> canonical text + locator map
  -> exact hash dedupe / near duplicate grouping
  -> versioned chunking
  -> embedding job
  -> PostgreSQL commit + index update outbox
  -> BM25S generation build + atomic pointer swap
```

默认输入：授权 URL、RSS/Atom、GitHub public API、arXiv API、上传 Markdown/纯文本/文本PDF、WebVTT字幕。私有 GitHub 连接是管理员配置的受限 connector，凭证不传 actor；不在 V1 假装支持所有私人平台。

网络每来源并发1、请求间隔初始1秒、HTML/RSS/VTT body默认10MiB、HTTP总硬上限20MiB、本地PDF上传默认25MiB；遇429服从 Retry-After，拒绝未经授权的登录/付费墙绕过。限值是设计初值。重复源 canonical URL 和内容hash去重，历史版本仍可引用。压缩文件默认不展开；未来展开必须有zip-slip、symlink和解压比/总大小限制。

解析失败保存 error、原始hash、失败阶段，可重试；不要悄悄存空正文。Docling/HTML parser 都按cpu/time/memory上限运行；OCR只给明确扫描件任务，记录费用/耗时，不代替文本解析。

## 3. Chunking

按标题/段落/代码块优先切分，初始目标 embedding-token 512、overlap64、单chunk最多1024tokens。不要将巨大代码块硬塞进一个向量；保留 parent section link。引用 offset 总是针对规范化文本，而不是后续分词文本。对中文、emoji、合成字符、CRLF、HTML实体做 offset 测试。

生产chunk_id使用UUIDv5，由project namespace与source_version_id/chunking_version/span确定性生成；原始摘要单独作为内容hash，不把64位hex塞进UUID字段。重切分产生新版本，不复用旧 chunk_id 指向新内容。embedding 维数、模型revision、归一化方式和 tokenizer version 一并存储；禁止不同embedding模型的数据混用。

## 4. 词法索引

BM25S `method=lucene,k1=1.2,b=0.75`。`Tokenizer` 采用冻结中文jieba词典+英文/代码分词；query词表禁止自增长破坏索引。停用词不要删除有意义的代码符号或否定词。未知词得到空或低召回，不导致异常。

每项目一个不可变generation。构建worker从固定数据库快照导出有序chunk IDs与文本，保存BM25矩阵/词表、映射、source watermark和manifest hash；写临时目录后校验，原子切换active generation。查询拿到generation句柄后整个请求使用同一版本，引用计数结束才GC旧目录。进程并发构建通过数据库advisory lock/lease避免覆盖。

明确限制：BM25S不是我们的事务型增量索引；批量重建默认debounce30秒。UI展示index_lag。新资料只有当chunk/vector/lexical符合最低合同才显示ready；向量失败可显式lexical_only，不假装完整hybrid。采集作业构建索引时，测量表里已有的向量照常写入。未进入测量表的正文，若本机没有已固定修订的嵌入权重，或 ML 环境不能离线导入，这条作业不下载模型，该段向量留空；词法索引仍用已抽取的文本发布。显式重建仍可启动编码进程。等待上限仍是 90 秒。

删除/撤权立即进入tombstone，所有搜索结果在返回、重排、组装LLM上下文之前再次查权限/有效性；随后重建index并清理磁盘。在V1项目内权限一致，不承诺细粒度每段ACL；跨项目索引绝不合并再先检索后过滤。

## 5. Hybrid retrieval

查询输入先鉴权。原query必保留；可最多产生2个受预算限制的改写，不让改写替换用户真实意图。默认每路top50：BM25与dense cosine。融合用 RRF：`sum(1/(60+rank))`，rank从1开始、同一路重复ID只计一次。初版固定权重相等；不把不同数值尺度的BM25和cosine直接相加。

RRF top30交 `BAAI/bge-reranker-v2-m3`，返回top8；每份文档最多2段，再按上下文预算裁剪。知识工具结果带所有 source_version/hash/span，不只返回自然语言摘要。重排失败允许标记degraded并退回RRF，不改变证据ID。

本地小语料默认精确向量查询。HNSW是后续同一接口下的优化，须验证project过滤后Recall@k；不能为了速度过滤掉正确答案却不报告。分别测BM25-only/dense-only/RRF/rerank的Recall@10、MRR@10、nDCG@10和p95，不先宣布hybrid必然更好。

## 6. Evidence graph

不引入Neo4j。PostgreSQL `evidence_links` 表存 `supports/contradicts/derived_from/supersedes/tested_by` 边。边的端点统一为immutable artifact：source version的canonical text、memory claim snapshot或验证报告。边有来源和创建方法，LLM声称的supports不等于已验证支持。材料来源变更时，旧版本引用仍可查；失效/删除则输出明确撤回状态，而不是偷偷改引用文本。

报告中的每个重要外部事实绑定一个或多个Citation，核实source hash/offset与claim。LLM评审仅检查语义相关性，独立于最终发布判定。无法证实的结论写“不确定”，不要引用不相关来源凑数量。

## 7. 长短期记忆策略

工作记忆：当前目标、已完成节点、重要失败、工具结果索引；大内容存artifact，不把整个历史拼回prompt。项目记忆：稳定决策/术语/约束，需有来源。个人偏好：默认只在项目内生效，owner可显式设为global。

写入流程 `observation -> proposed -> dedupe/conflict check -> policy -> source_claim / inference / observed_test / user_confirmed`（根据真实来源分类，只有明确用户确认才是user_confirmed）。对于高影响偏好、事实冲突和跨项目共享请求需要确认；网页和仓库不能把“用户允许发送密钥”写入记忆。读取先scope过滤，再检索；高重要度/新鲜度帮助排序，不提升未经验证的可信等级。

删除必须涵盖chunk/vector/BM25快照、推荐特征、派生摘要缓存和可选trace数据。Temporal history尽量只存不敏感ID；已写入历史的敏感payload必须按加密/保留策略处理。删除状态与备份到期时间告知用户，不能声称已从不可改写备份即时物理清除。

## 8. 推荐闭环（完整V1，但不冒充协同过滤）

候选：项目知识库内容相似top100、已批准来源的新资料top50、当前goal相关top50。排除hidden/已删除/跨项目内容；document级去重。

初始score各特征归一到[0,1]：`0.35长期兴趣+0.15短期兴趣+0.20当前目标+0.10新鲜度+0.10来源可信等级+0.10未读新颖性`。缺失特征对剩余权重重新归一。公式和版本写入每次推荐事件，不称最优算法。用MMR λ=0.8选10项，单来源最多2项，所有非随机推荐可解释为实际特征贡献。

反馈：bookmark+3、helpful+2、click+0.2、有效停留+0.3、hide-3，仅作初始规则。停留只接受窗口在前台且有效曝光的范围；click不直接等于学习收益。事件带event_id去重，记录impression_id、候选集合hash、rank、policy_version、exposure_time。

探索默认关闭。开启后仅在最后一个slot执行ε=0.05的均匀随机探索；记录候选N和实际选择概率，不能把ε直接当propensity。最优item被选择的概率为(1-ε)+ε/N，其余为ε/N；候选改变需新hash。安全/隐私过滤发生在探索前。用户可关闭画像与探索，或清空兴趣。

主动跟踪使用Temporal schedules；只跟踪预批准来源和明确目标，默认生成站内通知/研究草稿，不自动向他人发消息。每次扫描有独立预算和增量水位，无变化则不调用总结模型。


## 9. 代码图谱与 Knowledge Fabric 的区分

通用网页/文档/RSS 仍采用本章 BM25S + pgvector + BGE；CodeGraph 仅处理代码仓库的 symbols/imports/calls/impact，不替代个人项目记忆、事实证据图或验收。代码图谱索引是版本化的派生缓存，受 project ACL/仓库快照限制。详见 `docs/13_codegraph.md`。

## 10. 冻结决定：原生记忆先做完整
当前生产MemoryEngine固定native_postgres，不等待Hindsight/Graphiti对比。已有事实/事件/技能/来源关联足以实现可追踪更新和遗忘。只定义一个薄MemoryBackend接口，不为每个候选实现空适配器。上下文编译和并发删除要求见docs/14_BOUNDARIES_AND_FAILURES.md。未来Hindsight只能读取授权副本，其派生记忆不能覆盖原始证据/权限。
