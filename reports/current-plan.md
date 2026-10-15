# F00–F25 与 F27 已 done；F26 仍 blocked

监督者在 head `5ab4fef` 上对 DeepSeek `deepseek-chat` 跑了 live 探针，退出码 0。结果是 `reports/f26_live_result.json`。

head `fbb8260` 的结果已原样保存，退出码 0，但证据不够：恢复检查没有单独的第一次提交，hybrid 和隔离没有排名和容器退出码。F26 不标 done。

请重跑 `uv run --locked --no-build python scripts/run_f26_live.py --out reports/f26_live_result.json`。新文件应包含 `calls_detail`、提交 sha256、检索排名和容器 flags/exit_code。Hermes 四组对照和 CodeGraph 仍是 not_run。
