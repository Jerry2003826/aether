# 12 · 集成细节、认证与初始参数

## 服务间协议

所有内部调用仍做服务身份认证，不因为处于Compose网络就信任请求。V1使用安装时随机生成、按服务分开的凭据与明确scope；不写进仓库或传给actor。非本机跨主机连接要求TLS。生产轮换和撤销记录在audit。

**runner → model gateway**：为OpenHands提供经过能力探针确认的OpenAI-compatible chat-completions端点；若所选模型不支持该协议则由LiteLLM适配。job token仅能访问推理端点，服务器从token查project/task/job，不接受body改变scope。请求中model只能是授权alias，max_tokens受服务端限额；每次流式请求同样先reserve，流关闭后处理usage/unknown；不能用流提前终止绕过账单。

**worker → runner**：使用contracts/interfaces.py定义的start/get/cancel。start以(project,task,intent)唯一，返回job_id；get返回真实状态/最后心跳/产物引用；cancel返回accepted而非“外部动作已停止”。API/actor不直接得到容器地址。runner把upstream事件转换为脱敏AETHER event，不接受上游直接修改应用状态。

**worker → verifier**：发送immutable input manifest、contract hash、registry中trusted suite ID/hash，独立worker检查；只允许verifier凭据创建最终evidence，API/runner/actor均不能仿造。验收schema来自本包contracts/acceptance.schema.json，不是自由的shell字符串。

**worker → knowledge/ML**：文本先在受限parser转canonical text再进入embedding/rerank。首版内网HTTP或同进程协议均可，但返回embedding revision、维数、输入hash；一次batch内部不能混模型revision。状态不可用明确degraded/blocked，不能返回随机向量。

## 单owner认证流程

安装命令生成随机一次性bootstrap secret，终端只显示一次，禁止日志。`POST /v1/auth/bootstrap`只允许loopback、无既有owner、有效secret，创建owner并设置持久登录口令（服务端Argon2id哈希）；立刻作废bootstrap secret。公网不能开放bootstrap。

`POST /v1/auth/session`用口令登录，限流，返回HttpOnly/SameSite会话cookie；cookie为随机不透明值，数据库仅保存其哈希。`GET /v1/auth/session`返回身份/CSRF token；`DELETE`注销并撤销服务端session。状态变更要求CSRF token+Origin检查；跨站CORS默认关闭。release cookie Secure，localhost开发例外显式配置。

服务端从session取owner，不相信body中的owner字段。session表有expiry/last_seen/revoked，默认12小时绝对有效期、30分钟idle，可配置。恢复口令只能走本机显式CLI，撤销所有session。此方案不等于企业SSO。

## 具体时间/重试边界（初始值）

| 操作 | 默认值 | 处理原则 |
|---|---|---|
| HTTP静态抓取 | connect5s、read20s、total30s，最多3次重定向、HTTP硬上限20MiB（HTML/RSS默认10MiB） | redirect/每次真实连接重做目标策略；非幂等写不套此重试 |
| 读Activity | 单次30s、总90s、最多3次、backoff1/2/4s | 只有允许幂等重试的读操作 |
| LLM请求 | total120s，stream idle30s | 超时可能计费；unknown保留reservation，不能无条件重发 |
| 模型结构修复 | 最多2次 | 占同一task预算和总请求上限 |
| 代码job | 默认15分钟且不超过task剩余active budget | 以job状态/实际开始时间计，poll5s；取消grace10s后隔离进程树终止 |
| 文档parser | 每文件120s、默认内存1GiB | kill并报告parser_timeout；大文档可显式扩额，不默默跳页 |
| 审批 | 72小时 | expired后PAUSED，不继续执行旧授权；新的审批需新grant |
| SSE | 15秒heartbeat | DB为游标恢复来源，不能只依赖内存广播 |
| BM25 generation | 30秒debounce，单builder租约 | 查询可使用明确标记的旧generation；删除立即过滤 |

资源值是设计起点，F00在目标机器测量后写platform report，可按证据调整；不是性能承诺。OpenHands官方SDK是否有同名参数须实测，本表由外围服务执行，不据此虚构上游参数。

## 工作流拓扑细化

`TaskWorkflow`为用户task的唯一parent，保存contract版本、node cursor、pending command ID和剩余预算摘要。Research/Code/Extraction节点可用受限child workflow；Verification用独立task queue。`WatchWorkflow`只比较源变化并在预算内提交普通research task；`SkillEvaluationWorkflow`只运行受控评测，不是递归创建无界agent。

workflow history软上限5000 events时在安全边界continue-as-new；有未决update handler/child结果时先完成或明确转交，保持task的logical ID。外部UI event序列不随run ID重置。Activity读取DB合同后返回版本和hash，workflow固定使用该版本，不能每次重放重新读“最新”。

暂停/取消命令可随时接收，但应用在安全边界，预算不得因重启/continue-as-new重置。active elapsed应持久计算，排队/审批等待不扣active budget，已启动的远程job即使worker离线也继续计时。48次模型请求上限同时覆盖OpenHands内部调用和judge；验证预留按共享账本原子检查。

## 参考代码边界

Pydantic frozen仅阻止字段重新赋值，不深冻结list/dict。真正不可变合同需：规范化序列化、hash、只追加DB版本、写权限和每次使用校验，不能靠frozen=True实现安全边界。reference BudgetLedger是单进程算法参考；生产必须用事务。validate_relative_export_path仅做词法校验，不解决symlink、hardlink、mount或TOCTOU。SHA只能证明字节一致，不能证明来源可信。

参考Identifier允许可读fixture值，生产所有实体ID统一UUID；example中sha256用于合成内容身份，不是声称真实线上来源。schema文档的ID限制不能代替鉴权。

## 本地文件与仓库输入

UI使用`POST /v1/projects/{project_id}/artifacts`上传本地文件，默认25MiB，先写受限临时目录后校验hash和类型；Source.kind=local_document时locator只能是本项目artifact ID，不能是服务器路径。编码仓库由owner显式导出/上传快照或受限Git connector按批准revision取得；压缩包导入必须检查zip-slip/symlink/hardlink/文件数量/解压总大小，且在隔离worker中处理，不通用展开用户任意附件。

技能评测/部署等非聊天控制操作使用有类型的control task关联审批和审计；不能伪造任意既有task作为approval scope。若无需外部动作，control task仅使用其审批能力，不获取执行器权限。


## CodeGraph 运行时协议（新增）

受信 RepoIntelligence adapter 在 isolated runner 的专用工作进程中执行固定 CLI/MCP；可按 immutable snapshot manifest 缓存 index。只暴露 `repo.code.explore`, `repo.code.impact`, `repo.code.affected_tests` 经受信 gateway 过滤的结果；前后端/actor 不能调用全局 MCP server 或选择路径。版本与 status/freshness 写入 artifact metadata；取消任务时停止 watcher/daemon、关闭本次 graph 句柄。默认禁用，降级需显式记录。详见 `docs/13_codegraph.md`。
