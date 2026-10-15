# F00–F25 与 F27 已 done；F26 保持 blocked

最新绿的是 head `86eb0d7`：PR CI run `38699224527`（unit 51 passed，integration 137 passed），push run `38699216284`，SonarCloud 通过。

下一步要用户提供 `AETHER_LIVE_MODEL_KEY`、`AETHER_LIVE_MODEL_BASE`、`AETHER_LIVE_MODEL_ALIAS`，再跑 F26 live。`live_ready` 是 false。

仍是 not_run：Hermes 四组对照、CodeGraph 产品启用、F26 live 探针。
