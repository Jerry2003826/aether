# F24、F25 待当前 head 变绿；F26 保持 blocked

F26 没有 live 密钥，状态是 blocked，`live_ready` 是 false。blocked 不是通过。

F24：CI 安装 Hermes v0.22.1 并核对 `hermes --version`。对照实验仍是 not_run。

F25：恢复在同一事务里写回表行，并把未完成命令改成 STOPPED。产物用临时目录改名换上。

`docs/06_security.md` 写明 `NullPool` 是行级安全与性能的取舍。

只有这次 head 的 PR CI 和 Sonar 都绿，才把 F24 和 F25 标成 done。
