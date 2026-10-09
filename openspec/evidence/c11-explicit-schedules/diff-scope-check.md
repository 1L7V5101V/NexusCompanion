# C11 task 7.1 — PR diff 范围检查（tick 恢复语义分离断言）

检查时间：2026-10-09　分支：`feature/c11-explicit-schedules`　基线：`f00918ab`

## 判据与结果

| 禁止触碰面 | 检查方式 | 结果 |
| --- | --- | --- |
| `proactive_v2/`（proactive/drift/optimizer tick 本体） | `git status --porcelain \| grep proactive_v2/` | 0 命中 |
| work queue flow handler（`proactive/optimizer` flow） | `git status --porcelain \| grep work_queue` | 0 命中 |
| C2 outbox/delivery **表 schema** | `grep bootstrap/db/models/control_plane.py`、`grep f3c8a9d2e7b4` | 0 命中 |

C2 仓储文件 `bootstrap/db/repository/control_plane_repo.py` 有 1 处改动，内容为
把模块内私有的重放帧取号助手 `_record_replay_frame` 改名公开为
`record_replay_frame`（+`__all__` 一行），使 schedule 收束事务复用**同一** seq 分配
实现而不是复制一份。`git diff --stat` = `7 insertions(+), 5 deletions(-)`，
4 个改动点全部是同一标识符的改名（3 个调用点 + 1 个定义 + 1 行 `__all__`），
无 SQL、无状态机、无 `outbound_delivery_intents` schema 变更。

公开该助手的动机是 C11 的一条落地事实：`WebchatDeliveryAdapter` 从
`webchat_replay_frames` 取 `turn.completed` 帧投递，缺帧会退避重试直至 dead_letter。
schedule 写的 assistant message 若不同事务落帧，WebChat 侧提醒必然死信。

## 本 change 的文件清单

新增（schedule 域自有）：

- `alembic/versions/e8b4c2a6d9f1_c11_scheduled_jobs.py`
- `bootstrap/db/models/schedule.py`
- `bootstrap/db/repository/schedule_repo.py`
- `bootstrap/schedule_defaults.py`
- `bootstrap/schedule_durable.py`
- `tests/schedules/`（`__init__.py`、`conftest.py` + 6 个测试文件）

修改（既有文件）：

- `agent/scheduler.py`（`ScheduleManager` Protocol、`JobRef`、`ScheduledJob.when/
  advance_minutes`、legacy async 包装、`resolve_local_wall` DST 归一、
  `GRACE_SECONDS` 指向单一来源、`load_and_recover` 不再丢弃 disabled job）
- `agent/tools/schedule.py`、`agent/tools/remind.py`（面向 Protocol；binding 冻结；
  新增 suspend/resume 工具）
- `agent/config_models.py`、`agent/config.py`（`[scheduler].misfire_grace_seconds`）
- `bootstrap/toolsets/schedule.py`（后端门控 + 新工具注册）
- `bootstrap/toolsets/protocol.py`、`bootstrap/tools.py`（类型与 engine 清理步骤）
- `bootstrap/auth/api.py`、`bootstrap/auth/runtime.py`、`bootstrap/app.py`
  （admin misfire 面 + `schedule_admin` 注入）
- `bootstrap/db/models/__init__.py`、`alembic/env.py`、`bootstrap/db/models/extras.py`、
  `scripts/import_to_pg.py`（旧 `ScheduledJobModel` 清场，ADR-7）
- `scripts/migrate/importer.py`、`scripts/migrate/verify.py`、
  `tests/migration/helpers.py`、`tests/migration/test_results_evidence.py`：schedules
  退出 SQLite→PG 导入面（同一 ADR-7 决定在第二个导入器上的落地）。不改的话
  旧 schema 的 `scheduled_jobs` `TableSpec` 会把 JSON 行 COPY 进结构完全不同的新表，
  且 `TRUNCATE scheduled_jobs` 被 `schedule_executions` 的外键挡下
  （`psycopg.errors.FeatureNotSupported`），实测在 `tests/migration` 里炸出 33 个 error
- `tests/test_bootstrap_toolsets_p1.py`：schedule 工具集注册面新增 suspend/resume
  两项，属预期断言更新
- `check_schedules.py`（按后端分流查询面）
- `config.example.toml`（`[scheduler]` 注释示例）
