# F03 不可变产物与安全导入

目标：项目内 content-addressed 产物，上传校验 hash、长度和媒体类型，拒绝 zip-slip、符号链接、硬链接和解压超额。

合同：`contracts/schema.sql` 的 artifacts；`POST/GET/DELETE /v1/projects/{project_id}/artifacts`。

拟改路径：`packages/storage/aether_storage/artifacts.py`、`services/api/aether_api/app.py`、`tests/integration/test_artifacts.py`。

测试：`AETHER_REQUIRE_SERVICES=1 uv run pytest -q tests/integration/test_artifacts.py`

进度：未开始。

结果：无。

阻塞：无。
