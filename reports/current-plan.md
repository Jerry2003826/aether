# R8-A 采集、文档与切分

任务：在 `a2b9313` 上把采集链路的手写解析换成固定版本的开源库。ADR-F41。不改 `contracts/`、`reference/` 和 `readiness.live_ready`。

已改：

- RSS/Atom：`feedparser`，隔离子进程保留；调用前拒绝 `<!DOCTYPE` 与 `<!ENTITY`
- 网页正文：`trafilatura`；script src 用 lxml
- PDF/DOCX/PPTX/XLSX：`docling-slim` 的无模型 PDF 管线
- 分词：jieba `lcut`，wheel 在 `third_party/wheels`
- frontmatter：`python-frontmatter`
- 切分：`CharacterTextSplitter`，版本号仍是 `codepoint-1`
- 重试：`tenacity`，只包采集 HTTP
- 产物：只增加三种 OOXML 媒体类型

结果：冻结检查通过，参考套件 76 OK，单元测试与不依赖数据库的采集测试通过。全套集成测试没有在本机跑，因为没有 PostgreSQL、Temporal 和 Docker。PR #30 的 `pack-and-unit`、`integration` 和 SonarCloud 已通过。`live_ready` 保持 pending。
