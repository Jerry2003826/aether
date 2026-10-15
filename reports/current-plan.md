# Hermes 四组探测已记录，还不能判断领先

监督者在 head `fcceba5` 上用 live key 跑了 `scripts/run_hermes_compare.py`。原样结果在 `reports/hermes_compare_result.json`。四组各 1 条，都答对，6 次计量调用。每组 n=1，Wilson 95% 区间约 0.21–1.0。`comparative_evidence` 保持 not_run，`superior` 保持 null，费用 unknown。请求别名 `deepseek-chat`，上游返回的 model 是 `deepseek-flash`。

上限 8 次调用、8000 token、单次输出 128、四组 45 秒墙钟。子进程环境是允许名单，learned 读取 `memories/MEMORY.md`，工具集是空的 `bot_room`，网关保留 SSE。

审计一轮的 D01–D04 和 C01 已在 main（`9d22bb8`）上。记录见 `reports/audits/gpt_round1/REPAIR_RESULT.md`。
