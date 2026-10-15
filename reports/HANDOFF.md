# 交接

审查意见已提交。PR #1 在 `767b212` 之后的 GitHub Actions 与 SonarCloud 为成功，并已标为可审查。随后完成 F03。

## F03 测试

```text
AETHER_REQUIRE_SERVICES=1 uv run --locked --no-build pytest -q tests/unit tests/integration
32 passed
```

产物路径由服务端按哈希生成。zip/tar 拒绝路径穿越、符号链接、硬链接和过高压缩比。落盘后未写数据库的记录可以恢复。产物根目录不能放在宿主 home 下。

## 状态

- F00 done
- F01 blocked：BGE 权重未加载
- F02 done
- F03 done
- F04 未开始

CI integration 使用 pgvector `0.8.7-pg17` 与 Temporal dev server。
