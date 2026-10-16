# P2-09 公开采集、工具执行、检索可见性与只读推荐

任务：从 `cc471e3` 做本路四项。GitHub 与 arXiv 采集走真实抓取并进入检索；已批准工具经注册表、MCP 和操作账本执行（S-060）；来源删除递增 visibility epoch，检索缓存不再返回已删来源（S-048）；推荐 GET 不写画像和曝光（S-006）。

相关合同：不改 `contracts/`、`reference/`、验收标准或 `STATUS.json`。不改变 `live_ready`。不新增 OpenAPI 操作。决定记为 ADR-F34。

拟改路径：

- `services/worker/aether_worker/remote_sources.py`
- `services/worker/aether_worker/pending_jobs.py`、`ingest.py`、`rss_isolate.py`
- `services/worker/aether_worker/tool_gateway.py`、`service.py`
- `packages/storage/aether_storage/operations.py`
- `services/worker/aether_worker/console_commands.py`、`retention.py`
- `services/worker/aether_worker/recommendations.py`、`services/api/aether_api/console_routes.py`
- `config/tools.example.yaml`、`tests/fixtures/mcp_fixture.py`
- `docs/08_api_ui.md`、`docs/DECISIONS.md` 与由此重建的 `MASTER_SPEC.md`
- `tests/unit/test_remote_sources.py`
- `tests/integration/test_r7_public_sources.py`、`tests/integration/test_r7_tool_dispatch.py`
- `tests/integration/test_console_api.py`（GET 不再写曝光，见 ADR-F34）

测试命令：

```text
uv run --locked --no-build python scripts/verify_freeze.py
uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'
uv run --locked --no-build pytest -q tests/unit
AETHER_REQUIRE_SERVICES=1 AETHER_ADMIN_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/postgres AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233 \
  PATH="$HOME/.local/bin:.hermes/.venv/bin:$PATH" uv run --locked --no-build pytest -q tests/integration
uv run --locked --no-build python scripts/doctor.py --require-services
```

进度：四项产品路径已接到 worker。先失败的采集单测在实现前无法收集（缺少 `aether_worker.remote_sources`）。

结果：冻结检查通过。参考套件 76。单元 231。集成在 Hermes 不在 PATH 时 347 passed、1 failed（`test_pinned_hermes_cli_prints_its_real_version`，`hermes_cli_missing`）。把 `.hermes/.venv/bin` 放入 PATH 后，该探针与本路 5 条集成用例一起 6 passed。doctor exit 0。`reports/doctor.json` 的本机探测差异没有提交。

阻塞：无。不改 `live_ready`。BM25 文件里可能仍留已删除文本的字节，返回前按 tombstone 过滤。本路径不发放 capability。probe 仍不打开传输。编码作业和技能评测仍是 unsupported。
