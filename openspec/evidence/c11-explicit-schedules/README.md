# C11 验收证据 — 运行方式

## 环境

本地 PostgreSQL 用便携实例（不是 Docker），端口 5433、角色 `nexus`：

```bash
P=/d/Projects/postgres/pgsql/bin
"$P/pg_ctl.exe" -D "D:\\Projects\\postgres\\data5433" -l "D:\\Projects\\postgres\\logfile5433" start
"$P/pg_isready.exe" -h 127.0.0.1 -p 5433        # -> 127.0.0.1:5433 - accepting connections
```

测试默认 URL `postgresql://nexus:nexus_dev@localhost:5433/nexus` 写死在
`tests/conftest.py`，实例起着即可，不需要额外环境变量。

## 命令

C11 套件（PG-backed + 纯计算 contract，共 45 项）：

```bash
NEXUS_REQUIRE_PG=1 uv run --no-sync pytest -q -W error tests/schedules/
```

legacy 调度路径不回归（88 项，零改动通过，task 6.11）：

```bash
uv run --no-sync pytest -q -W error tests/test_scheduler_service.py \
  tests/test_schedule_tool.py tests/test_remind_tool.py \
  tests/test_tool_scheduler_ownership.py tests/test_job_store.py tests/test_fire_at.py
```

全量回归（`NEXUS_REQUIRE_PG=1`，PG 不可达即报错而非静默 skip）：

```bash
uv run --no-sync python scripts/regression.py \
  --evidence openspec/evidence/c11-explicit-schedules/pytest-regression.txt
```

类型检查：`uv run --no-sync pyright <本次改动的文件>`，判据 0 errors。
`bootstrap/tools.py` 的 `preloadable` / `agent.tools.agent_restart` / `TurnLogger`
等 error 是仓库既有基线（见 `openspec/evidence/c12-observability-backup/pyright-project-full.txt`），
不由本 change 引入，也不由本 change 修。

## 结果（2026-10-09）

- 全量回归：**2004 passed / 0 failed / 0 errors / 0 skipped**，16m06s，
  `NEXUS_REQUIRE_PG=1`（跳过数为 0 是关键——有 skip 就说明集成测试没真跑）。
- `tests/schedules/`：**46 passed**（`pytest-schedules.txt`）。
- legacy 调度路径 4 个测试文件零改动通过（task 6.11），与 schedules 合跑 156 passed。
- pyright：本次新增/改动的自有文件 **0 errors**（仓库基线 warning 不计）。

## ⚠️ 一次踩过的坑（影响证据可信度）

各 PG 测试套件的 session fixture 会 `DROP DATABASE IF EXISTS nexus_<x>test` 再重建。
**同一时刻跑两个 pytest 进程会互相删掉对方的 scratch 库**，产出成片的 `F`/`E` 与
`数据库 "nexus_c7test" 不存在` 这类假失败。本目录早先一次全量输出
（`54 failed, 1882 passed, 68 errors`）就是两个全量进程 + 一个前台切片三方并发所致，
已丢弃、不作为验收证据；`pytest-regression.txt` 是单进程独占运行的那一次。

同一次独占全量还暴露了两个我自己的真实缺陷（都已修，见 `diff-scope-check.md` 与 design ADR-7）：
批量导入面仍带着旧 `scheduled_jobs` 的 `TableSpec`（`tests/migration` 炸 33 个 error），
以及 DST contract 测试用墙上钟面算术冒充瞬时算术、看着通过其实什么都没测。

## 文件

- `diff-scope-check.md` — task 7.1 的 diff 范围检查（tick 恢复语义分离断言）
- `pytest-regression.txt` — 全量回归原始输出（2004 passed）
- `regression-runner.log` — `scripts/regression.py` 的运行日志（含退出码）
- `pytest-schedules.txt` — `tests/schedules/` 的验收输出（46 passed）
