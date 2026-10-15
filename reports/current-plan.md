# 研究任务改用 live 模型

基线 `e1cc9e5`。默认提供者是 live。`AETHER_RESEARCH_PROVIDER=scripted` 才走离线摘录。预算在调用前预留。`allowed_source_ids` 按字面限制资料。`scripts/run_live_task_check.py` 读取上传响应的 `artifact_id`。异常结果包含步骤、类型和去掉密钥后的消息。

# 审计三轮

基线 `4ba3b2f04df4bcd28a8d6f6cba0b0fd5d9d353cc`。

- R3-01：当前观察决定验证结果。缓存键包含候选、权威合同、验收版本和来源快照。
- R3-02：离线摘录必须对上目标，否则 `NEEDS_REVIEW`。合同摘要按内容重算。
- R3-03：`task_artifacts` 把内容去重和任务引用分开。
- R3-04：追加前先修截断尾，修复时截到前缀长度。
- R3-05：暂停、恢复、取消会改变任务状态，0 行更新不再报成功。

没有改 `STATUS.json`。脚本路径不是 live 模型。全量 `274 passed`。
