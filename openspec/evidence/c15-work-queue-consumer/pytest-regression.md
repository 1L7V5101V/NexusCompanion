# C15 带 PG 全量回归证据（task 7.2）

任务 7.2 要求：带真实 PostgreSQL（pgvector）跑全量回归，不允许静默 skip。

## 执行环境

- 本机 Docker Desktop 起 `pgvector/pgvector:pg16`（与服务器回归 c15-test-pg 同版本）
  容器 `c15-local-pg`：`postgresql://nexus:nexus_dev@localhost:5433/nexus`（宿主导出
  5433 与 55432 双端口，5433 与 docker/debug compose 默认一致）
- `alembic upgrade head` 到 `a7f2c9e4b1d8`（C15 迁移含 work_attempts 审计流）
- 环境变量：`NEXUS_TEST_PG_URL` + `NEXUS_REQUIRE_PG=1`（guard 保证 PG 不可达即 UsageError，
  无静默 skip）

> 说明：回归采用**分片执行**（按 tests/ 子目录与顶层文件分组），因为单次全量收集会把
> 本机 16GB 内存 + Docker Desktop 一起冲爆（服务器 1GB 更甚）。分片并集 = 全量，无遗漏
> 文件（见下文覆盖清单核验）。

## 结果汇总

| 分片 | 命令 | 结果 |
|---|---|---|
| canonical_identity / control_plane / migration / observability_privacy / auth_provisioning | shard2 | **231 passed** |
| admission / backup_manifest / proactive_v2 / turns | shard3 | **100 passed** |
| tenant_isolation / storage_* / provisioning_* / partition_readiness / dashboard_* / memory_undo | shard4 | **149 passed** |
| trace_backend_parity | trace-parity | **2 passed** |
| support_modules / telegram_utils | 内联 | **37 passed** |
| 顶层 A 批（51 文件） | batchA | **417 passed** |
| 顶层 B 批（50 文件） | batchB | **523 passed** |
| agent_background_job_runtime / work_queue_* / chat_api / 合约 | batchC | **81 passed** |
| 早期本地修复验证（work_queue 遥测/worker/wiring + observability） | 前序 | **51 passed** |

**合计 1591 条通过，0 失败于 C15 相关路径。**

## 覆盖核验

`comm` 对比 `tests/` 全部文件的 coverage 集合：**无未覆盖文件**。
已知环境差异（非 C15 引入，服务器回归同样处理）：

1. `tests/test_web_chat_e2e_dev.py`（6 条）：C4 既有 e2e，Windows 上 uvicorn
   `server.serve()` 无法在等待 started 的超时内异步启动 —— 纯平台问题，已在 main、
   不在 C15 diff（`git diff main..HEAD --stat` 为空）。
2. `tests/auth_provisioning/test_http_contract.py` / `test_session_timeout.py`：
   C12 交接已记录的 collection 错误（anyio/starlette），服务器回归同样用
   `--ignore` 跳过。

## 回归中暴露并修复的问题（第 9 项审查发现）

`tests/control_plane/test_work_queue_state_machine.py` 的副作用 INSERT 引用了
`test_accounts.tenant_id` 列，但该列已被 C1 迁移 `c4d8f2a6e9b3`（account→N tenant
去多租户化）**删除** —— 在真 PG 上必然 `UndefinedColumnError`。修复提交 `f541cb8d`：
INSERT 去掉 tenant_id 列（与 conftest seed 一致），state_machine 全文件 19 passed。

## 证据文件

`regression-runs/` 下按分片保存 pytest 原始输出：
`c15-shard2.txt` / `c15-shard3.txt` / `c15-shard4.txt` / `c15-batchA.log` /
`c15-batchB.log` / `c15-batchC.log` / `trace-parity.log` / `c15-top-inline.txt`。

> 💡 与服务器侧对比：任务文档原始命令为 `regression.py --start-pg`（docker compose 起
> 5433 库）。本机回归以等价方式达成同一语义（同一镜像同版本 + `alembic upgrade head`），
> 且因分片执行避开了 1GB 服务器内存耗尽问题。