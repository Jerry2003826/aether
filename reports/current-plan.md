# 审计二轮

基线 `55fd636fa1615314c129f5728663f3450595bc47`。

- R2-01：验证者自己读候选字节，证据键包含候选、合同和摘要。
- C01：Temporal 活动读取已上传文本，脚本提供者写回答，验证后进入 SUCCEEDED、FAILED 或 NEEDS_REVIEW。
- R2-02：日志写满才算成功；截断尾隔离，中间坏行单独报错。
- R2-03：规范化 `./` 成员路径，重名拒绝。
- R2-04：`Database.fetch_one` / `fetch_all` 回到类上。

没有改 `STATUS.json`。脚本路径不是 live 模型。
