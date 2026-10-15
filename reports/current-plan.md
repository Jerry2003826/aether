# F26 仍 blocked，等待完整验收结果

已记录的 `reports/f26_live_result.json` 是三条短文冒烟，第一次编码失败也是提示诱导的。不能据此标 done。

完整入口：

```sh
cd environments/openhands && uv sync --frozen
cd ../ml && uv sync --frozen
docker pull python:3.12-alpine
cd ../..
export AETHER_LIVE_MODEL_BASE=https://api.deepseek.com/v1
export AETHER_LIVE_MODEL_ALIAS=deepseek-chat
export AETHER_LIVE_MODEL_KEY=...
uv run --locked --no-build python scripts/run_f26_acceptance.py --out reports/f26_acceptance_result.json
```

上限 8 次调用、20000 token、单次输出 512。`dac5c49` 的结果已原样记下，研究失败来自引用分隔符。检查器改完后用同一条命令重跑。`live_ready` 保持 false。Hermes 四组对照和 CodeGraph 仍是 not_run。
