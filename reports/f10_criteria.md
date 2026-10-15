# F10 验收与测试

每条验收对应至少一个会真实执行的测试。没有 skip，也没有空测试。向量列保持 null，没有用随机向量代替 BGE。

| 验收 | 测试 |
|---|---|
| 原文哈希与 Unicode 码点偏移正确 | `tests/integration/test_ingest.py::test_offsets_cover_cjk_emoji_and_newlines` |
| 空正文显式失败 | `tests/integration/test_ingest.py::test_offsets_cover_cjk_emoji_and_newlines` |
| SSRF 不连接 loopback，重定向重新检查 | `tests/integration/test_ingest.py::test_ssrf_does_not_connect_to_loopback_or_follow_redirect` |
| DNS 结果为保留地址时拒绝 | `tests/integration/test_ingest.py::test_ssrf_does_not_connect_to_loopback_or_follow_redirect` |
| 解析器有体积上限，且用户请求不加载 Playwright | `tests/integration/test_ingest.py::test_fixture_download_is_real_and_parser_stays_static` |
| 授权夹具上的真实 HTTP 下载写入 SourceVersion，embedding 未填 | `tests/integration/test_ingest.py::test_fixture_download_is_real_and_parser_stays_static` |
| RSS 空频道失败 | `tests/integration/test_ingest.py::test_fixture_download_is_real_and_parser_stays_static` |
