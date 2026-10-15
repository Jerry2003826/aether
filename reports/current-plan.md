# Hermes 四组对照还没跑

入口是 `scripts/run_hermes_compare.py`。监督者机器先执行 `scripts/install_hermes.py`，再设置 DeepSeek 的 `AETHER_LIVE_MODEL_BASE`、`AETHER_LIVE_MODEL_ALIAS` 和 `AETHER_LIVE_MODEL_KEY`。

上限 8 次调用、8000 token、单次输出 128。开发集和 holdout 分开。`comparative_evidence` 保持 not_run，`superior` 保持 null。

审计一轮的 D01–D04 和 C01 已在 main（`9d22bb8`）上。记录见 `reports/audits/gpt_round1/REPAIR_RESULT.md`。
