# 06 · 权限、隔离与提示词注入防线

## 1. 威胁模型

把网页、RSS、PDF、代码仓库、MCP响应、第三方skill和LLM输出全部视为不可信。攻击可能诱导泄露API key、读取其他项目、修改验收/日志、扩大权限、反复花钱、运行恶意依赖或伪造完成报告。提示词过滤只能辅助，不是边界；隔离容器也不是无条件安全承诺。

评分器的父进程以 root 运行，从不导入候选代码。能归约成输入/输出协议的套件（每个断言都是候选探针与字面期望值的相等比较）由父进程持有断言和期望值。期望值不进入子进程、不写入 `protocol.json`。降权到 nobody 的子进程只计算探针并返回值，不跑 pytest。套件目录在子进程启动前收成 nobody 不可读；子进程若仍能打开套件、提前退出或返回值对不上，结果都是 FAIL。这种通过可以记为安全认证（`security_certified`），但评分结果的 `auto_promote` 恒为 false，不能据此自动发布。

任意 import 式白盒 pytest 仍与候选同一个解释器。它只验证合作代码：即便 JUnit 看起来通过，评分也不是 PASS，`trust` 为 `cooperative`，`security_certified` 为 false，不能自动发布。宿主只接受恰好一行 `ISOLATION` 和一行 `GRADE`。容器内的 `os.kill(-1, SIGKILL)` 只作为该容器 PID 1 清理本命名空间的残留进程，禁止在宿主机执行。

未覆盖、也不能称为已修复：容器逃逸、ptrace 父进程、候选猜中期望值、以及不能归约成字面协议的白盒断言。容器边界本身也不是对恶意代码的无条件安全承诺。

可信域：owner身份系统、policy/tool registry、gateway、runner-manager、verifier控制进程、release evaluator。应用开发者能修改这些代码，因此自我进化只允许改skill/受限workflow DSL，不允许Agent自动替换可信控制代码。

## 2. 网络和凭证分区

control网络：API/DB/Temporal/worker/gateway的受认证通信。runner-admin接口只允许trusted worker调用。每个编码job使用单独sandbox网络，默认无互联网，只能访问其job-scoped model gateway端点。沙箱不能访问control网段、其他job、云metadata、宿主机管理端口、Docker socket或DNS外传通道。HTTP工具由网关代表调用。

runner-manager是高权限基础设施。Docker socket仅它可持有，而且使用专用rootless daemon/专用runner VM；不能挂给API或Agent容器。不得声称只加`read_only`就防止越权。对恶意不受信任仓库的公开多租户部署，容器边界不够，V1明确不支持；应在后续使用更强隔离并重新威胁评审。

数据库key、provider key、GitHub token、审批签名key和verifier身份分别分发；任何一个actor不能获得全局credential。job-scoped token限制模型、project、task、预算、期限、调用次数，只能访问特定endpoint。不要把provider原始key透传给OpenHands。

## 3. 工具分类与能力授权

工具的分类、参数schema与影响由本地管理员维护的registry决定，**不信任MCP annotations**。类别：

- R0：授权项目内读取、搜索、无需外部变更的分析。仍要检查数据出口。
- R1：隔离工作空间文件改写/代码执行；无宿主机和外部写权限。
- R2：外部可撤销写操作，例如新建草稿PR；逐次绑定审批或明确限域预授权。
- R3：付款、正式发送、删除外部资源、部署、权限变更。V1默认disabled；只有管理员显式启用已实现且有对账策略的connector后逐次审批。没有实现的工具不能变成一个万能HTTP接口。

`CapabilityGrant`包含owner/project/task、tool、allowed_resources、args hash或限域规则、max_uses、expiry、data_class、egress destinations。Actor只能提出request，不能签发grant。流程为normalize -> resolve target -> check policy -> approval -> reserve -> commit intent -> dispatch。

## 4. 审批绑定与TOCTOU

Approval显示真实收件人/仓库/分支/域名、规范化参数、payload/patch hash、资源版本、费用上限、有效期和可逆性。批准后的参数、文件内容、base_commit、目标变化必须重新审批。不要使用“允许Agent干任何事”复选框。

审批token服务端签名、单用途、短TTL；数据库CAS消耗；重复点击不能重复动作。签名比较恒时；签名key不在sandbox。服务端确认owner身份，不接受请求体里自报的approver_id。审核事件单独记录。过期与撤回都阻止新的dispatch，但不假装撤销已经发生的外部操作。

## 5. SSRF与网页安全

下载只接受http/https、默认80/443，无URL userinfo、无file/gopher/unix等scheme。解析并规范化IDN/IPv4/IPv6；禁止loopback、private/link-local/metadata、reserved等非公开目标，判断以“是否全局可路由”为准，100.64.0.0/10共享地址段（含其中的云厂商metadata地址）、fec0::/10 site-local以及IPv4-mapped/NAT64内嵌的上述地址同样拒绝（本地fixture通过单独测试配置定点允许）。每次redirect重新检查，限制跳数、请求时长、解压后body大小和content type。

DNS检查必须与实际连接目标绑定并配合网络出口规则；“先查一次IP，再让库任意重解析”不能过安全验收。防DNS rebinding、IPv4-mapped IPv6、重定向到metadata、代理环境变量绕过、服务名称解析逃逸。HTTPX本身不提供这些授权语义；网络策略在真实容器内做探针验证。实现不了就禁用对应live网页功能，不降低测试。

JS浏览器与parser都不能在control网络。浏览器下载/弹窗/第三方请求受限，禁用复用用户cookie、自动扩展和未批准写请求。网页HTML在UI中作为清洗后的内容展示，禁止原样innerHTML；Markdown renderer禁止任意script/危险URL。提示词注入样本展示为引用数据而非执行指令。

## 6. 文件、代码和依赖

只挂载显式工作卷，non-root、drop capabilities、no-new-privileges、资源限额、默认只读rootfs、限制pids、独立tmp。真实可用字段以runner镜像smoke验证。tar/patch/file操作拒绝绝对路径、`..`、symlink/hardlink逃逸、设备文件和过大产物。

代码提交使用base snapshot与patch，不直接挂主机源码根、home或`.ssh`。安装依赖属于代码执行风险：默认镜像预装工具；临时新增包需批准的registry出口/锁定hash，不能让网页给出任意curl|bash后直接跑。Git hooks和仓库自带启动脚本不视为可信。

trusted test suite只读且独立；actor无法写测试定义、评测标签或verifier结果表。只读测试文件也不能解决被测代码运行时攻击，所以测试sandbox仍无密钥与网络。

## 7. MCP接入

只有管理员添加transport和server executable/URL，禁止模型动态安装MCP server。首次拉取schema保存hash并审核；server升级或工具schema变更进入quarantine，暂停旧授权。stdio使用固定argv而非shell拼接；argv检查会拒绝shell程序、env/sudo等启动包装器和解释器的内联代码参数（如`sh -ec`、`node -e`、`python -c`），但它只是配置错误的兜底，不是安全边界，边界仍是管理员审核的可执行文件与沙箱；远程服务固定TLS和允许域名。读超时、body限制、cancel和重连必须测试。

每个MCP tool映射到AETHER tool ID、风险/幂等分类与JSONschema。任何未分类工具默认deny。远程返回的instructions或“这是只读操作”不能修改分类。OAuth token仅gateway存储，按最小scope申请；未实现OAuth的provider只提供管理员配置token方式，UI如实标明。

## 8. 身份、审计与删除

V1单owner启动：仅loopback绑定，通过本机一次性enrollment建立登录；无默认公开密码。使用HttpOnly+SameSite cookie，state-changing请求检查CSRF/Origin；不在URL/localStorage存长期admin token。非loopback发布需要TLS和正式密钥配置，缺失则拒绝启动。数据库有 `one_owner_v1`，第二次创建所有者会失败。

`/v1/admin/*` 依赖这个单所有者不变量：任何已登录会话都属于那一个所有者，所以这些路由没有单独的角色检查。一旦支持多用户，必须先核对调用者是所有者，不能只凭登录态放行。

`packages/storage/aether_storage/db.py` 的 `Database` 使用 SQLAlchemy `NullPool`。每个事务单独建立连接，提交或回滚后关闭，不把连接放回池里复用。行级安全依赖 `set_config('aether.owner_id', ..., true)` 和 `set_config('aether.project_id', ..., true)`，第三个参数使设置只在当前事务有效。连接池在异常或会话状态没有清干净时，仍可能把上一个 owner 或 project 留在同一条物理连接上。不用池是为了避免这种串项目。代价是每次事务都要重新握手，吞吐低于常驻连接池。这是有意的行级安全与性能取舍，不是漏写 `pool_size`。

服务身份与用户会话分离。所有project请求服务端查访问权限，不能信header里的project_id。对所有list/detail/SSE/artifact/search/feedback/skill入口做越权测试。

审计默认存摘要、hash、工具参数的脱敏版本；避免原始秘密。hash chain可发现部分修改，但不对有DB管理员权限的人提供不可篡改保证。真正防篡改需外部只追加存储，V1不虚假宣传。审计保留期限可配置，敏感payload独立加密并支持删除。

## 9. 强制安全回归

至少包含prompt注入、跨项目检索、审批重放/参数漂移/过期、SSRF与DNSrebinding、路径逃逸、伪造工具分类、unknown effect、预算竞态、secret输出脱敏、恶意测试修改、公共skill自动加载、MCP schema漂移和sandbox网络连通探针。安全失败阻止发布和技能promotion；不能通过给提示词加一句“请安全”来代替修复。

## 10. 威胁情报浏览器与 Tor

浏览器抓取使用 Playwright `1.64.0` 和 Chromium 修订 `1248`（`156.0.8078.4`）。浏览器镜像按摘要固定为 `mcr.microsoft.com/playwright@sha256:06a9939e57531807f8d5fd76ce44b53165ffb7d7501d87ab10e285c20b1e971f`。派生镜像带上 `aether_worker`、`aether_domain` 和 `config/threat_browser.yaml`。这份配置里的浏览器、Tor 和 `.onion` 默认关闭。开关以部署的配置文件为准，请求里的开关不能超过它。打开 Tor 必须同时把 `owner_confirmed` 设为 true。

所有者审批来自账本里已有的 `approvals` 行。verifier 角色只核对，不写审批，也不持有 worker 能写的账本权限。worker 核对通过后写入 `operations`，并把 `consumed_by_operation_id` 填上，同一条审批不能第二次使用。调用方自己构造的许可对象会被拒绝。worker 进程不持有 verifier 的数据库地址，也不调用 Docker。Docker 只由 runner 进程执行。

明文访问只把浏览器容器接到出口网络，不创建 Tor 容器。Tor 打开时，浏览器容器只接入 `--internal` 网络，这条网络里只有 Tor 容器；Tor 容器再单独接入出口网络。SOCKS 只绑定这次访问在内部网络上分配的地址，策略只接受该 /28，并拒绝其他来源。出口网卡不监听 9050。夹具页也只绑在这个内部地址上。浏览器容器不挂 Docker socket，不发布端口，关闭 JavaScript 和下载。经 Tor 时只通过 `socks5://aether-tor:9050` 出去。进程环境里的 `HTTP_PROXY` 不传给浏览器，调用方也不能把代理改成别的地址。Tor 镜像由 `infra/tor.Dockerfile` 从 `debian@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f` 构建，安装 `deb.torproject.org` 上的 `0.4.9.14-1~d13.trixie+1`。构建时核对官方签名密钥指纹 `A3C4F0F979CAA22CDBA8F512EE8CBC9E886DDD89`，进程用户是 `debian-tor`。原先的 `osminogin/tor-simple` 不再使用。构建文件放在 `/var/lib/aether-tor-build`，安装结束后删除。

网页目标仍交给既有的 `assert_public_target`。每次文档请求连到这次校验返回的地址，有 IPv4 时用 IPv4。HTTP 的 Host 和 TLS 的 SNI 仍用原来的主机名，Chromium 不再自己解析或拨号。重定向每一跳重新授权。子资源和子 frame 的 document 请求直接中止，不发起抓取。附件和可执行内容在交给页面前拒绝，不写入下载目录。每次访问把脱敏记录写入 `audit_events`，按项目内的 sequence 接上一条哈希；调用方丢掉内存列表也不会丢掉这条记录。Tor 容器启动后，浏览器要等这个内部地址上的 9050 完成 SOCKS 握手。夹具模式因为 `DisableNetwork` 不接受 SOCKS，改为等同一地址的 8080 接受连接。`.onion` 不走明文 DNS，而且要另开 `.onion` 开关。经 Tor 或 `.onion` 得到的正文标成 `tor` 或 `onion`，不能作为 verifier 的可信证据。正文去掉双向控制符后放进不可信引用，页面文字不会变成工具调用。用途只有 `threat_intelligence`。交易、非法下载和联系卖家在代码里拒绝。进程内 Playwright 只在夹具开关 `AETHER_INGEST_ALLOW_FIXTURES=1` 打开时可用。

攻击回归使用本地恶意页面和本地假服务，不连接真实暗网。需要 Docker 的集成测试会构建并启动浏览器容器和 Tor 容器；Tor 在该测试里设置 `DisableNetwork 1`，因此不接受 SOCKS 连接，测试确认主进程是 `tor`。夹具页只在 Tor 容器内通过 TLS 提供。监听套接字保持明文，每个已接受的连接再握手，明文探测不会拆掉监听。证书在夹具容器启动时生成，私钥只出现在 `debian-tor` 的 0700 目录，不写入镜像层。浏览器把 `AETHER_FIXTURE_CA` 的 base64 解码成 PEM 文本再交给 `load_verify_locations`；该参数的字节形式是 DER，不能直接传入 PEM 字节。runner 只运行本进程环境 `AETHER_THREAT_BROWSER_IMAGE` 与 `AETHER_TOR_IMAGE` 里的镜像，并且只有 runner 自己打开 `AETHER_INGEST_ALLOW_FIXTURES` 时才接受夹具。`AETHER_REQUIRE_SERVICES=1` 且没有 Docker 时，这个测试失败，不跳过。进程内攻击回归仍直接调用同一个 Playwright 读页函数。


## 增补：CodeGraph MCP/CLI 不能获得主机级权限

CodeGraph 有机会读取整个仓库源码并把源码返回给模型，因此它是有真实数据泄露风险的**只读工具**。工作进程必须仅挂载获准 snapshot，不挂宿主 home、其他项目仓库、credentials 或 Docker socket；关闭遥测/默认网络；LLM 不能传递任意 projectPath。源码注释/README/工具响应全部视作不可信。图谱影响分析和相关测试列表不构成可信安全验收。完整规则见 `docs/13_codegraph.md`。
