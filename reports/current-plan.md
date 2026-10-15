# F00–F25 与 F27 已 done；F26 仍 blocked

监督者在 head `5ab4fef` 上对 DeepSeek `deepseek-chat` 跑了 live 探针，退出码 0。结果是 `reports/f26_live_result.json`。

通过：协议 tools/schema/usage、研究引用、编码形状检查。费用 unknown。

仍是 not_run：hybrid 检索、Linux 隔离、编码恢复、Hermes 四组对照、CodeGraph 产品启用。因此 `live_ready` 保持 false，F26 不标 done。

下一次在监督者机器上用 `uv run --locked --no-build python scripts/run_f26_live.py --out reports/f26_live_result.json`。Docker 只用于隔离检查；没有就记 not_run。新结果交回来之前不改 F26。
