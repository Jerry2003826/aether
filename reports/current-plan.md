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

结果：冻结检查通过，参考套件 76 OK，单元测试 297 passed。全套集成测试没有在本机跑，因为没有 PostgreSQL、Temporal 和 Docker。`b40ce32` 的 `pack-and-unit`、`integration` 和 SonarCloud 已通过。`live_ready` 保持 pending。

审查收紧（仍是 ADR-F41）：

- 非法 YAML frontmatter → `IngestError("parse_rejected")`
- Retry-After 拒绝 NaN 和非有限值
- ACTIVE 索引的分词版本不符 → `tokenizer_mismatch`，不在 search 里重建
- 重定向链共用 3 次 tenacity 预算
- RSS 先按 UTF-8/UTF-16/UTF-32 解码再拒绝实体声明
- 网页跳过 XML 声明和已闭合注释；技能 metadata 只读到第二个 `---`
