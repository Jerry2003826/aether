# 08 · API、控制台与用户交互

## 1. 通用协议

API前缀`/v1`，JSON字段snake_case，时间UTC RFC3339；UI按Australia/Melbourne展示并标注时区。ID使用UUID，示例/reference中允许可读测试ID，生产层统一转换。分页使用opaque cursor和limit<=100，不用无界列表。

对创建task、发布skill和外部操作请求使用`Idempotency-Key`，作用域是owner+project+route+key，保存规范化payload hash；同key不同body返回409。更新资源使用If-Match版本控制；审批重新绑定具体artifact/intent，不依赖UI缓存。

错误统一`{code,message,request_id,retryable,details}`。401未登录、403越权、404不可见或不存在、409版本/幂等冲突、422不合法合同、429限流/预算、503依赖不可用。不把异常包装成200+success:false。健康检查`/health/live`与`/health/ready`分开。

## 2. 资源与路由

| 路由 | 方法 | 契约/行为 |
|---|---|---|
| /v1/projects | GET/POST | owner项目列表/创建；不暴露其他项目 |
| /v1/projects/{project_id}/tasks | GET/POST | 分页任务；POST输入原始目标+预算/资料范围，202返回task_id |
| /v1/projects/{project_id}/tasks/{task_id} | GET | 状态投影、合同版本、budget known/held/unknown、产物 |
| /v1/projects/{project_id}/tasks/{task_id}/contract | GET/PUT | 草案合同；已批准合同变更新建version，旧版只读 |
| /v1/projects/{project_id}/tasks/{task_id}/commands | POST | approve_goal/pause/resume/cancel；command_id去重，202异步接收 |
| /v1/projects/{project_id}/tasks/{task_id}/events | GET SSE | event_id游标重放，不丢断线期间事件 |
| /v1/projects/{project_id}/approvals | GET | 待处理审批列表 |
| /v1/projects/{project_id}/approvals/{approval_id}/decision | POST | approve/reject；body含expected_intent_hash，server查owner |
| /v1/projects/{project_id}/artifacts | POST multipart | owner上传受限文件，返回ArtifactRef，默认25MiB |
| /v1/projects/{project_id}/artifacts/{artifact_id} | GET/DELETE | 鉴权后读取/删除；download不接受任意path |
| /v1/projects/{project_id}/sources | GET/POST | source config，域名/权限/抓取间隔明确 |
| /v1/projects/{project_id}/sources/{source_id} | GET/PATCH/DELETE | 版本控制；删除立即tombstone |
| /v1/projects/{project_id}/ingestions | POST/GET | ingest command与状态，内容未索引完明确显示 |
| /v1/projects/{project_id}/search | POST | query、filters、mode；返回hits/来源/索引版本/degraded |
| /v1/projects/{project_id}/memories | GET/POST | 搜索或用户明确添加记忆；proposed不能自动confirmed |
| /v1/projects/{project_id}/memories/{memory_id} | PATCH/DELETE | 用户纠正/删除；生成新revision或tombstone |
| /v1/projects/{project_id}/recommendations | GET | 只读推荐列表。不写画像、不写曝光；impression_id 为空，直到显式的 rank 写入 |
| /v1/projects/{project_id}/feedback | POST | idempotent event；user不在body中自报 |
| /v1/projects/{project_id}/skills | GET/POST | 列表/导入quarantine；脚本不自动运行 |
| /v1/projects/{project_id}/skills/{skill_id}/evaluate | POST | 独立evaluator job；actor不能指定最终分数 |
| /v1/projects/{project_id}/skills/{skill_id}/deployments | POST | promote/rollback，要求eligible eval hash+审批 |
| /v1/projects/{project_id}/watches | GET/POST | 授权来源跟踪、schedule、预算 |
| /v1/projects/{project_id}/watches/{watch_id} | PATCH/DELETE | 暂停/更新/删除schedule |
| /v1/projects/{project_id}/evaluations | GET/POST | 明确dev/validation/release类别与dataset hash |
| /v1/projects/{project_id}/costs | GET | 时间范围与任务账本；未知成本单独字段 |
| /v1/admin/connectors | GET/POST | owner-only registry和连接器配置；密钥仅写不可回显 |
| /v1/admin/connectors/{connector_id}/probe | POST | 非副作用能力测试，不能自动扩大scope |
| /v1/admin/models/probe | POST | model/tool/structured output能力与价格配置检查 |
| /v1/projects/{project_id}/coding-jobs | POST | 受理 202。worker 在断网、只读、限资源的沙箱里跑到 FINISHED 或 FAILED；失败后同一作业键可以重试。任务取消只有在容器确认消失后才写成 CANCELLED；仍持有租约的作业不会被 TTL 回收。受理不是通过 |

`/v1/admin/*` 依赖 V1 只有一个所有者。已登录会话就是该所有者，这些路由因此没有第二层角色检查。支持多用户时必须先核对所有者角色，不能只凭登录态放行。第二次创建所有者会被拒绝。

`contracts/openapi.yaml`提供上述资源与认证路由的设计合同；实现时由真实FastAPI生成OpenAPI并做契约差异测试。不得只实现少数示例就宣布API完成。`apps/web`通过生成的typed client调用真实API。

## 3. SSE

EventEnvelope包含event_id、task_id、project_id、sequence、type、time、payload、schema_version。持久event序号在每task单调增加，不用时间戳替代唯一ID。客户端`Last-Event-ID`重连，从数据库读遗漏事件后再进入live tail；心跳与数据事件区分。

events必须脱敏。不同task的event ID不能拿来绕过项目权限。历史已清理时返回明确cursor_expired和重新拉取snapshot方法，不静默从现在开始。UI不能因最后一个LLM token写“完成”就把task标成success。

## 4. 控制台页面

**Overview**：项目切换、活跃任务、待审批、最近失败、今日成本（unknown单列）。没有数据时显示空态与新建任务，不用假曲线填满页面。

**Task workspace**：左栏DAG/步骤列表，中间目标合同与运行事件，右栏预算/权限/技能版本；底部Artifacts与Verification。暂停/取消有请求中状态；显示恢复次数和为什么blocked。有限长度执行理由即可，不要求记录模型隐藏chain of thought。

**Approval inbox**：目标、规范化参数/patch、影响、费用、expiry、资源版本、来源与变更diff。高影响动作不能默认checked；不提供误导性的“永久允许所有”。

**Knowledge**：来源状态、索引延迟、版本、删除；Search结果可展开命中段落与source。Memory页面显示谁说的/何时/冲突/有效范围，支持纠正与forget。

**Discover**：推荐卡片、真实推荐理由、bookmark/helpful/hide；探索状态可见、可关闭，禁止用伪随机结果冒充学习模型。

**Skills lab**：候选和parent diff、训练来源数量、验证集类别、raw结果/CI/费用、部署与回滚。没有足够样本明确“证据不足”，不显示虚假的能力增长百分比。

**Evaluations & settings**：四组baseline实验配置与报告；模型alias、key状态、预算、connector权限、隐私/保留设置。

## 5. UI验收

支持中文长文本、键盘操作、明确label、焦点管理、可读对比度。至少测试窄屏和桌面；错误、loading、offline、empty、stale projection、approval expired都要可视化。禁止暴露原始HTML/密钥。Playwright测试使用真实API与fixture provider；mock-only组件测试不能代替端到端。
