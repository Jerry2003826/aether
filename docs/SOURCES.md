# 上游依据与证据范围

本轮2026-10-15检查以下官方仓库/文档。只用于确认架构选用能力；没有在这里替用户安装完整上游服务、锁定发布版本或运行性能对照。所有AETHER限额/目录/接口/选择均是本项目设计，不是上游性能保证。

## S01 · Temporal Python SDK
https://github.com/temporalio/sdk-python

确认范围：Workflow/Activity/client/testing/重放；当前branch文档不等于PyPI版本已验证

## S02 · Temporal activity definition
https://docs.temporal.io/activity-definition

确认范围：Activity副作用和幂等性；不保证任意远端操作恰好一次

## S03 · CodeGraph README
https://github.com/colbymchenry/codegraph

确认范围：默认explore工具、CLI/程序接口、telemetry off；运行时仍须真实探针

## S04 · OpenHands SDK
https://github.com/OpenHands/software-agent-sdk

确认范围：四个组件目录、Python/REST接口、隔离workspace选项

## S05 · LiteLLM
https://github.com/BerriAI/litellm

确认范围：模型适配接口；不选用其其他harness能力

## S06 · pgvector
https://github.com/pgvector/pgvector

确认范围：Postgres向量相似度与过滤注意事项

## S07 · BM25S
https://github.com/xhluca/bm25s

确认范围：BM25检索及保存/加载

## S08 · FlagEmbedding
https://github.com/FlagOpen/FlagEmbedding

确认范围：检索/重排模型工具

## S09 · MCP Python SDK
https://github.com/modelcontextprotocol/python-sdk

确认范围：官方工具协议客户端/服务端SDK

## S10 · Agent Skills
https://github.com/agentskills/agentskills

确认范围：SKILL.md规范

## S11 · Pydantic Evals
https://pydantic.dev/docs/ai/evals/evals/

确认范围：独立评测能力，不要求使用Agent运行时

## S12 · Syft
https://github.com/anchore/syft

确认范围：镜像和文件系统SBOM

## S13 · OSV Scanner
https://github.com/google/osv-scanner

确认范围：漏洞扫描，具体版本离线支持需探针

## S14 · Codex AGENTS.md
https://developers.openai.com/codex/agent-configuration/agents-md

确认范围：项目级指令发现/优先级/长度限制

## S15 · Codex best practices
https://developers.openai.com/codex/learn/best-practices

确认范围：任务计划、上下文控制和实际验证

## 提供的本地资料
已读取并比对V1.2设计/合同/任务、V1.3增量与DeepSeek总纲。它们完整保存在archive。DeepSeek是研究材料而非授权指令，不继承其中未经本轮验证的市场数字或许可证结论。
