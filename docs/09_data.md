# 09 · 数据模型、事务与保留

`contracts/schema.sql`是数据库初始结构合同，不是声称已经在PostgreSQL运行过的迁移。Codex将它转为Alembic并执行clean upgrade / rollback / upgrade / data-preserving upgrade测试。

另有auth_sessions、node_dependencies、ingestion_jobs、coding_jobs和connector_configs辅助表；身份表为全局，业务实体均有project_id。evidence_links以不可变artifact为边的端点，verification结果可附在tested_by边上。

## 实体组

| 组 | 表 | 关键约束 |
|---|---|---|
| 身份/项目 | owners, projects | V1单owner；project域是所有业务的隔离边界 |
| 目标与运行 | tasks, goal_contracts, task_nodes, command_outbox, task_events | contract版本不可覆盖；event/command去重；status为投影 |
| 权限与操作 | capability_grants, approvals, operations | 同intent唯一；approval绑定hash；未知结果保留 |
| 证据 | artifacts, verification_results, evidence_links | immutable hash；验证关联contract/产物/验证器 |
| 成本 | budget_accounts, model_requests | 整数microUSD；reservation与actual分开；unknown不是0 |
| 知识 | sources, source_versions, chunks, index_generations, memories | 来源版本、offset、scope、向量revision一致 |
| 学习 | skills, skill_versions, skill_deployments, evaluation_runs | 同scope活动版本原子切换；候选无自升权限 |
| 个性化 | feedback_events, profile_snapshots, recommendation_impressions | 幂等反馈；policy/propensity版本；能删除与重算 |
| 自动化 | watches, notifications | schedule ID稳定、站内通知默认无外部副作用 |
| 审计 | audit_events | 只追加应用权限；不声称可抵抗数据库管理员 |

## Scope与数据库权限

所有业务表含project_id，引用采用(project_id,id)复合FK，避免把其他项目的对象挂进来。应用层鉴权+数据库RLS双层；DB会话每transaction设置授权后的project上下文，连接池复用不得遗留scope。只给服务所需权限；API账号不能写verification结果、skill评测成绩和最终task成功状态；actor无数据库账号。

`schema.sql`中的RLS是project predicate基准，实际迁移必须加入角色权限与项目列表访问策略。不能因为表owner通常绕过RLS而在集成测试中只用superuser；必须用真实runtime角色验证。项目创建等全局操作走专用已认证路径。

## 四个关键事务

1. **提交**：task+contract draft+command_outbox原子提交；Temporal dispatcher用固定workflow ID，失败可重投。
2. **审批与动作**：锁定approval和operation，核对intent hash/version/expiry，消耗一次性授权、写PREPARED/DISPATCHED；远程网络调用不在长数据库transaction内。
3. **预算**：锁budget account，验证spent+held+new<=limit与verification headroom，插入唯一request reservation；结算actual时保留超支事实，不能拒绝记账以伪装没超额。
4. **技能切换**：校验独立eval evidence与expected previous version，更新active deployment，append audit；写入版本不修改已运行任务。

## 大对象

数据库保存metadata/ref，内容在ArtifactStore。路径只能服务端生成`project_id/sha256-prefix/sha256`，不得接受用户相对路径作为最终存储路径。先临时写入+fsync+hash验证，再rename；DB记录引用，后台GC回收无引用对象。下载必须经过鉴权，public URL不默认开放。支持将来S3兼容adapter，但V1默认本地volume不依赖第三方对象存储。

## 数据一致性

index、profile、UI projection都是可重建派生数据；每次查询记录水位/版本。缓存key至少含project_id+权限版本+source/embedding/tokenizer版本+query。删除和撤权增加visibility epoch，旧缓存立即失效。RRF与reranker不能绕过tombstone检查。

## 保留与备份

默认原始模型prompt不落日志；可配置脱敏trace保留30天、task metadata90天、artifact30天、知识资料直到owner删除。所有默认值在配置可见，不是法律要求。backup单独加密，记录保留与删除到期；测试pg dump/restore及artifact hash核对。恢复后先停外部dispatch，对UNKNOWN和未完成操作对账，不能一恢复就重放全部写操作。
