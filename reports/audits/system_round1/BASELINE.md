# 全系统不变量审计 · 第 1 轮 · 基线

## 1. 冻结的版本

| 项 | 值 |
|---|---|
| 基线提交 | `d6ed382b539c8e03994b593e1cdc33a6dbdb207a`（main，PR #12 合并：「Merge pull request #12 from Jerry2003826/cursor/audit-round5-codex-p1-7618」） |
| 检出方式 | 本 VM 上 detached HEAD 检出该 SHA，`git status` 干净 |
| 审计日期 | 2026-10-16（UTC） |
| 上一轮外部审计基线 | 第五轮 `aedbdd5`；第五轮修复与 Codex 跟进合入后才是本基线 |

本轮的阅读、复现和输出全部针对这一个 SHA。复现用例只放在 `reports/audits/system_round1/repro/`，没有改任何产品代码、测试、迁移、`STATUS.json` 或 `reports/HANDOFF.md`。主分支之后如有新提交，修复与验收需要重新冻结基线，不能把两个版本的源码混在一起看。

## 2. 运行环境

| 组件 | 本次实际版本 | CI / 规格目标 | 差异说明 |
|---|---|---|---|
| 操作系统 | Ubuntu 24.04.5 LTS，内核 6.12.94+，4 vCPU，15 GiB 内存 | ubuntu-latest | — |
| Python | CPython 3.13.16（uv 管理） | 3.13 | 一致 |
| uv | 0.13.1 | setup-uv（未锁小版本） | — |
| Python 依赖 | `uv sync --locked --no-build`，`uv.lock` sha256 `ad9bb2d6b408fb98613acd8e6bb1c413cadebb56ff850da043e16fcf5867691e` | 同一锁文件 | 一致 |
| 关键包 | fastapi 0.143.1，pydantic 2.14.1，sqlalchemy 2.1.4，alembic 1.20.0，psycopg 3.3.6，temporalio 1.34.0，litellm 1.105.1，mcp 2.3.0，tiktoken 0.14.0，pytest 9.1.1，playwright 1.64.0 | 同上（锁文件） | — |
| PostgreSQL | 17.11（PGDG apt 包）+ pgvector 0.8.7 | 镜像 `pgvector/pgvector:0.8.7-pg17@sha256:ac0853…` | 同一大版本与 pgvector 版本，不是同一镜像摘要 |
| Temporal | CLI 1.9.1（内置 Server 1.32.0，UI 2.54.1），`temporal server start-dev --headless`，内存存储 | 镜像 `temporalio/temporal:1.9.1` 的 `server start-dev` | 与 CI 相同的 dev server；**不是生产持久化部署** |
| Docker | 29.1.3（Ubuntu `docker.io`，手动启动 dockerd，iptables-nft） | GitHub runner 自带 Docker | 不是 rootless，也不是专用 runner VM |
| Playwright Chromium | build 1248（`playwright install --with-deps chromium`） | 同 | — |
| Hermes CLI | v0.22.1（`scripts/install_hermes.py`），只用于对比研究用例 | 同 | 只在第 4 次运行时把 `.hermes/.venv/bin` 加入 PATH |
| Node.js / pnpm | Node v22.14.0，pnpm（nvm 自带） | Node 24，pnpm 10.18.2 | `apps/web/package.json` 要求 `node >=24`；本机只跑了 `tsc --noEmit`，未跑 Next 构建 |
| `pyproject.toml` sha256 | `b982173d5d0a86c947424b0ad557d69f983aa4ef2170ad12e8e2725104a8c6f2` | | |
| `apps/web/pnpm-lock.yaml` sha256 | `7aba2f16e626b17aeb18828a8bd85407bfbb852c43228d467ed5a34a152b3f0e` | | |

服务启动方式（全部只在本 VM，未改仓库配置）：

```bash
sudo pg_ctlcluster 17 main start          # 密码 postgres 仅用于本机一次性库
temporal server start-dev --ip 127.0.0.1 --port 7233 --headless --log-format json
sudo env PATH=/usr/sbin:/usr/bin:/sbin:/bin dockerd
```

所有测试命令共用：

```bash
export AETHER_REQUIRE_SERVICES=1
export AETHER_ADMIN_DATABASE_URL=postgresql://postgres:<本机口令>@127.0.0.1:5432/postgres
export AETHER_TEMPORAL_ADDRESS=127.0.0.1:7233
```

## 3. 基线测试结果（原始输出在 `baseline/`）

| 次序 | 命令 | 结果 | 文件 |
|---|---|---|---|
| 0a | `uv run --locked --no-build python scripts/verify_freeze.py` | `result: passed`，退出码 0 | `baseline/run0_check_pack.txt` |
| 0b | `uv run --locked --no-build python -m unittest discover -s tests -p 'test_*.py'` | 76 项 OK，退出码 0 | `baseline/run0_test_reference.txt` |
| 1 | `pytest -q tests/unit` | **108 passed** | `baseline/run1_unit.txt` |
| 2 | `pytest -q tests/integration`（还没装 Docker / Chromium / Hermes） | 215 passed，**24 failed**，24 项全是环境缺失：依赖 Docker / 容器 runner 20 项、Playwright Chromium 2 项、Hermes CLI 2 项 | `baseline/run2_integration_failures.txt`，`baseline/run2_integration_tail.txt` |
| 3 | 装好 Docker 和 Chromium 后，只重跑第 2 次失败所在的 9 个文件 | 33 passed，2 failed（Hermes CLI 不在 PATH） | `baseline/run3_rerun_after_docker_chromium.txt` |
| 4 | `PATH=.hermes/.venv/bin:$PATH pytest -q tests/unit tests/integration` | **347 passed，1 warning，294.63s，退出码 0** | `baseline/run4_full_unit_integration.txt`，`baseline/run4_full_unit_integration_junit.xml` |
| 5 | `cd apps/web && pnpm typecheck` | 退出码 0（Node 22） | `baseline/run5_web_typecheck.txt` |

结论：在依赖补齐的前提下，基线的产品测试套件全部通过（347 项），与第五轮修复报告记录的 347 passed 一致。下文的缺陷都是现有测试没有覆盖的组合场景，不是现有测试失败。

## 4. 已知缺失（影响结论边界）

1. **没有真实模型 key**（`AETHER_LIVE_MODEL_KEY/BASE/ALIAS` 均未配置）。所有 live 路径用例只替换 `litellm.completion` 传输（或指向本机伪造的 OpenAI 兼容 HTTP 服务），数据库、Temporal、产物存储、验收器都是真实的。凡是结论依赖真实供应商的用量回报、计费、tokenizer、拒答/截断行为或提示词注入下的模型行为，一律标为「需要真实环境」。
2. **没有安装 OpenHands SDK 和 BGE/ML 环境**（`environments/openhands`、`environments/ml` 是独立锁文件，未建）。编码执行器和向量检索只做了代码阅读与已有 Docker 用例。
3. **Temporal 是 dev server，内存存储**。多 worker、持久化、历史长度上限、升级期间在跑工作流的行为只能在本 VM 上模拟（用 `Replayer` 和同进程 worker），不等于生产集群。
4. **Docker 是普通 dockerd**，不是规格要求的专用 rootless daemon / runner VM。沙箱网络与凭证隔离只证明到「本机 `--network none` 容器」这一层。
5. **未做非 loopback / TLS 部署、未做多主机、未做浏览器端人工操作**；控制台只跑了已有的 Playwright 用例。
6. 漏洞库离线（`STATUS.json` 里 `vulnerability_db: offline_not_run`），本轮没有做依赖漏洞扫描。
7. 第五轮附件里的 SQLite 74 项隔离套件没有在本机重跑；本轮的复现全部改写为真实 PostgreSQL / Temporal 用例。

## 5. 源码包与去敏

审计直接在 git 检出上进行，源码以上面的 SHA 为准，本轮不另外打源码包。若要把同一版本发给外部审计方，按审计技能第 1 步执行：

```bash
git archive --format=zip -o aether-d6ed382.zip d6ed382b539c8e03994b593e1cdc33a6dbdb207a
```

并在包内说明下列替换。对基线 328 个受控文件做了正则扫描（OpenAI/Anthropic/AWS/GitHub/Slack/Google 形态的 key、私钥块、带口令的 URL、`api_key=` 类赋值），共 24 处命中，全部是占位符，没有真实密钥：

| 类别 | 位置 | 性质 |
|---|---|---|
| `postgresql://postgres:postgres@…` | `.github/workflows/ci.yml:84`，`reports/HANDOFF.md`，`reports/audits/gpt_round{1,2,4,5}/REPAIR_RESULT.md` | CI 一次性服务容器口令 |
| `postgresql://postgres:admin-secret@…` | `tests/integration/test_runner.py:159`，`tests/integration/test_verification.py:183` | 测试里检查"不泄露"的假口令 |
| `sk-live-connector-secret`、`sk-test-secret-not-for-logs` | `tests/integration/test_console_api.py:261`，`tests/integration/test_gateway.py:15` | 脱敏测试用的假 key |
| `SENSITIVE_SUMMARY_SHOULD_NOT_RETURN` | `tests/unit/test_context.py:21` | 测试字面量 |
| f-string 模板 | `packages/storage/aether_storage/bootstrap.py:99`，`tests/integration/test_migration.py:34,73` | 运行时拼接，无字面口令 |

本轮新增产物的密钥扫描结果见 `FINDINGS.md` 末尾「交付物校验」。
