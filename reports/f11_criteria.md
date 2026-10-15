# F11 验收与测试

每条验收对应至少一个会真实执行的测试。没有 skip。向量来自 CPU 上的 `BAAI/bge-m3`，revision `5617a9f61b028005a4858fdac845db406aefb181`，维度 1024。重排分数来自 `BAAI/bge-reranker-v2-m3`，revision `953dc6f6f85a1b2dbfca4c34a2796e7dde08d41e`。测试用的紧凑结果在 `config/retrieval/fixture.json`，并带有每个向量的 sha256。`scripts/measure_retrieval_models.py` 把完整测量写到被 gitignore 的 `config/retrieval/measured.json`。缓存里没有的文本会走 ML 进程，失败时不会编造向量。

| 验收 | 测试 |
|---|---|
| 真实 embedding revision 与 1024 维 | `tests/integration/test_hybrid_retrieval.py::test_measured_revision_and_grouped_channels` |
| BM25、dense、RRF、rerank 分组对照，并计算 Recall/MRR/nDCG | `tests/integration/test_hybrid_retrieval.py::test_measured_revision_and_grouped_channels` |
| 错误 revision 或缺失文本不能被当成向量 | `tests/unit/test_retrieval.py::test_wrong_embedding_revision_is_rejected` |
| 缺失文本的进程失败时不发明向量 | `tests/unit/test_retrieval.py::test_cache_miss_does_not_invent_a_vector` |
| 授权过滤在检索、重排和上下文之前；删除与撤权立即不可见 | `tests/integration/test_hybrid_retrieval.py::test_deleted_and_revoked_chunks_are_invisible_before_context` |
| 跨项目不能读取另一项目的 generation | `tests/integration/test_hybrid_retrieval.py::test_other_project_cannot_read_the_index` |
| 同一连接提交项目 A 后，以项目 B 查询看不到 A | `tests/integration/test_hybrid_retrieval.py::test_project_binding_is_transaction_local` |
| 索引先写临时目录再切换；未激活的 generation 标记为 stale | `tests/integration/test_hybrid_retrieval.py::test_generation_swap_marks_retired_search_stale` |
| 中文、英文、emoji、代码符号可检索；未知词不抛异常 | `tests/integration/test_hybrid_retrieval.py::test_cjk_emoji_and_code_symbols_are_retrievable` |
| 否定词和代码符号不被停用词删掉 | `tests/unit/test_retrieval.py::test_tokenizer_keeps_negation_and_code_symbols` |
| 构建有 advisory lock，30 秒内重复构建不另起 generation | `tests/integration/test_hybrid_retrieval.py::test_build_debounce_and_lock` |
| 检索持有共享锁时不删除旧目录 | `tests/integration/test_hybrid_retrieval.py::test_retired_directory_stays_until_the_reader_lock_clears` |
