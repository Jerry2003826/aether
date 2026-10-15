# CodeGraph 所有者开关

示例配置和功能矩阵保持关闭。已安装锁打开，安全默认不变：遥测关闭、容器断网、只索引 git 快照、失败时在快照内降级。

真实证据命令：

```sh
uv run --locked --no-build python scripts/run_codegraph_product.py --out reports/f15_product_result.json
```

`readiness.codegraph_enabled` 在结果检查全部通过之前保持 `not_run`。
