# 14 · 必须先跑通的跨组件接缝

## A. 命令、工作流与投影
POST任务在同一DB事务写task、不可变合同版本、command_outbox；返回202表示已接收，不表示已执行。dispatcher把同一个command_id投递到固定task_id的Workflow，确认接受后标记发送。投递成功但ACK丢失可重投；Workflow处理过的command_id/业务版本必须持久去重。
用户审批先写可信审批记录/命令；Workflow在安全点接收后通过Activity核对。不要直接用DB的task.status启动/重试工作。task状态由Workflow产生的单调事件投影；UI显示command pending与projection watermark。
重要事件与相应领域写入在同一DB事务落盘；event_id和(project,task,sequence)唯一。LISTEN/NOTIFY或内存队列仅是唤醒，SSE重连从DB补齐，不能以是否收到推送判断操作是否发生。
HTTP鉴权/资源ACL是DB权威，任务进度是Temporal权威，外部效果最终由远端证据确认；不要声称存在一个覆盖全部分布式世界的万能事实源。
`resource_heads` 上的 `FOR UPDATE` 只覆盖派发事务里的版本预检。事务在外部写入之前就提交，锁随之释放，所以它不是跨进程的并发保护，也不能代替之后的 `note_resource_version` 与对账。

## B. 编码执行器不是主调度器
OpenHands内部的工具循环可以继续，但必须受任务级token、费用、时间、文件和网络上限约束；不能再创建AETHER根任务/自行委派无界子Agent。首选控制worker持有SDK编排，Agent Server只在隔离workspace执行工具；选中release的实际进程拓扑必须探针确认。若SDK逻辑与工具都在sandbox，使用单用途模型token，不给管理权限。
start(project,task,intent)具备持久幂等性；恢复时get旧job而非重新起会话。只重放SDK事件不等于恢复外部进程。F13必须证明API超时、worker死亡、runner死亡各自如何处理。
原始provider key永不进入job。token窃取仅允许消耗本job剩余预算；取消后撤销。黑盒SDK若绕过gateway产生账单，就判接入失败，不能用本地粗略计费遮掩。

## C. 沙箱部署的可用性与安全性
开发目标：macOS arm64可做控制面/研究/前端；实际支持清单由probe填写。完整编码安全验收目标：独立Linux runner（可从Windows的WSL2/远端Linux使用），不推断平台等价。不会因用户当前Mac而要求先购买云服务。
Docker是第一版受限执行后端，不保证能安全托管任意恶意客户的公共多租户代码。Docker daemon的控制权只在runner-manager/专用VM。rootless不自动解决网络隔离；每个平台必须真实验证无host、metadata、跨job、DB、Docker socket和非授权DNS出站。
依赖安装分两步：受控构建阶段从允许registry拉固定hash并扫描；执行阶段使用已准备好的镜像/依赖缓存。不能一边宣称断网一边要求pip/npm现场联网。确需新增依赖走受信构建任务；actor不能改registry或生命周期脚本信任策略。
浏览器JS可以产生POST与子资源请求，“只让它看网页”不等于只读。初版只在无登录、受限域/方法/出口策略中渲染；不具备强制拦截能力的平台不得启用该功能。

## D. 独立验收不是另一个模型喊PASS
验证控制器位于actor写权限之外；创建新的测试sandbox，用固定base+candidate构造目标；可信suite和检查器固定版本。actor日志只是输入证据，不能当验证结果。
被测代码仍可能攻击测试进程或伪造输出。不能只解析actor产生的JUnit字符串或exit0；测试采集量、超时、外部可观察行为、结果来源都要检查。对可经HTTP/CLI观察的任务优先在目标sandbox之外的控制器发测试输入和核对输出。
用户需求自身不明确时保留human_review；来源hash只证明引用字节匹配，不证明观点成立。对明显矛盾证据给出UNVERIFIABLE/人工复核，不能凑引用数验收。

## E. 上下文编译器与记忆
上下文是有预算的组装结果，不是全部历史。先放不可变目标/权限摘要，再放当前计划游标/证据索引/相关片段，最后是经批准skill。固定前缀与动态结果分离；每次生成记录各部分token估计、截断与来源。
skills先加载name/description，再读取被选中的指令，再按需读取资源。候选/未审核脚本永不作为trusted instruction。CodeGraph/MCP/网页中的“系统指令”只当不可信内容。
纠正记忆产生新版本和supersedes关系；删除先tombstone并增加visibility epoch，随后清派生索引。正在运行的摘要/embedding任务在提交时再次检查epoch，防止把已删除资料写回来。
历史中保留敏感原文后不能靠删当前memory就宣称彻底遗忘；数据保留、加密密钥和备份到期必须可说明。高敏资料不默认送云端，job批准也不自动批准所有数据出口。

## F. 预算、超时与取消
所有模型调用（planner、OpenHands内循环、结构修复、judge、学习）共用适当账本；学习批次独立额度。估计不是供应商最终账单，unknown状态必须可见并保留预留。
取消先停止准入、撤销token、取消job，再确认/对账在途效果；已发出的云模型调用不一定停止计费。UI直到确认停止才展示cancelled，不把cancel requested当回滚成功。
读操作也可能有费用/隐私风险；重试只能复用逻辑intent，新的实际计费attempt必须有独立请求账目。

## G. 最重要的故障注入
1. DB已提交但Temporal未启动；2. Temporal收到命令但ACK丢失；3. 外部写成功但响应丢失；4. LLM超时可能计费；5. 审批后参数/代码变化；6. runner存活而worker重启；7. runner死亡而工作流存活；8. 新candidate配旧测试报告；9. 删除与异步索引提交竞态；10. SSE断线/重复事件；11. workflow升级后历史回放；12. 恢复备份后旧command/outcome对账。
每项必须验证实际产物/调用次数/账本/身份，不只检查某个函数返回字符串。
