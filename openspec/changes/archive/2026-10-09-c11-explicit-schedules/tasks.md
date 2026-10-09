## 1. Schema 与模型

- [x] 1.1 Alembic 迁移 `e8b4c2a6d9f1_c11_scheduled_jobs.py`（`down_revision='c7e9a3f1b5d4'`，raw SQL，expand-only）：`scheduled_jobs` + `schedule_executions` 两表、CHECK 枚举、`UNIQUE (job_id, scheduled_for)`、claim/misfire/admin 索引（design ADR-1）
- [x] 1.2 ORM `bootstrap/db/models/schedule.py`：`ScheduledJobModel` / `ScheduleExecutionModel` + 状态/原因枚举常量（`SCHEDULE_JOB_STATUSES`、`SCHEDULE_EXECUTION_STATUSES`），`__all__` 挂入 models 包
- [x] 1.3 `[scheduler].misfire_grace_seconds` 配置（`agent/config_models.py`，默认 300，PROPOSED DEFAULT 保留；`config.example.toml` 注释示例）
- [x] 1.4 schedules 退出 SQLite→PG 导入面（`scripts/import_to_pg.py`、`scripts/migrate/importer.py` 的 `scheduled_jobs` TableSpec 与 transform、`scripts/migrate/verify.py` 列期望、`tests/migration/` 表清单）：旧 JSON 行无法映射 owner 三元组，且同名字段已被 durable 表占用（design ADR-7）

## 2. Repository（PG）

- [x] 2.1 `bootstrap/db/repository/schedule_repo.py::ScheduleRepository`：`create_job`（owner 三元组 + binding 冻结 + conversation fail-closed 解析）、`list_jobs`（tenant/status 过滤）、`get_job`、`set_job_status`（suspend/resume/revoke，revision +1）、`revoke_jobs_by_account`
- [x] 2.2 认领/收束事务（design ADR-3）：`claim_due_jobs`（到期扫描 + execution 插入幂等 + missed/skipped 分支 + next 前进）、`complete_execution`（sequence 分配 + canonical message + delivery intent `sched:<execution_id>` + execution 终态 + last_outcome，单事务）、`fail_execution`
- [x] 2.3 恢复扫描：`sweep_interrupted_executions`（running → failed `interrupted_by_restart`，recurring 前进 / one-shot 收束）+ `list_executions`（status 过滤，admin/恢复共用）

## 3. Durable 调度服务

- [x] 3.1 `bootstrap/schedule_durable.py::DurableSchedulerService`：`run()/stop()` 接口对齐 legacy；启动恢复扫描 → 1s tick（claim → instant 内容 / soft `process_direct` → complete/fail 收束）；`RevocationGate` 触发前重验保留
- [x] 3.2 触发时 fail-closed 链：账号状态（suspended 挂起不刷屏 / revoked 置态）→ RevocationGate → telegram binding active 校验（`binding_inactive` skip）（design ADR-2）
- [x] 3.3 时间计算复用 `agent/scheduler.py`（`compute_fire_at`/`next_cron_fire`/`LatencyTracker`），不复制实现
- [x] 3.4 装配门控：`build_scheduler` 按 `storage.backend` 选择 durable/legacy；`ScheduleManager` Protocol + legacy async 包装（`create_job/list_jobs/cancel_jobs/suspend_job/resume_job`）；`bootstrap/tools.py` 传 session factory 依赖
- [x] 3.5 `check_schedules.py` 适配：PG 后端查询面 / JSON 兜底

## 4. 工具面

- [x] 4.1 `ScheduleTool`/`RemindTool` 创建路径走 `ScheduleManager.create`：binding 只取服务端组装值（user principal 的模型 channel/chat_id 剥离语义保持并固化测试）；durable 路径冻结 binding
- [x] 4.2 `ListSchedulesTool`/`CancelScheduleTool` 面向 Protocol（tenant 隔离语义不变）；新增 `suspend_schedule`/`resume_schedule` 工具（id/name，租户隔离同 cancel）
- [x] 4.3 `bootstrap/toolsets/schedule.py` 注册新工具 + durable 服务注入

## 5. Admin misfire 查看面

- [x] 5.1 `bootstrap/auth/api.py`：`GET /api/admin/schedules`（status 过滤）、`GET /api/admin/schedules/executions`（missed/skipped/终态过滤，limit）、`POST /api/admin/schedules/{id}/suspend|resume|revoke`；admin principal 门控（沿用 telegram-bindings 模式），仅 PG 后端注册
- [x] 5.2 非 admin principal 访问 → 404/403（与既有 admin 端点行为一致）

## 6. 测试（tests/schedules/，PG-backed + 纯计算 contract）

- [x] 6.1 conftest：复用 control_plane scratch-DB/alembic 模式（`tests/schedules/conftest.py`）
- [x] 6.2 创建与 owner 负向：模型提交任意 channel/chat_id ≠ 冻结 binding（负向）；无 canonical conversation fail-closed；tenant 隔离 list/cancel
- [x] 6.3 幂等：同 `(job_id, scheduled_for)` 双触发只一次副作用（execution 唯一 + intent 一对一）
- [x] 6.4 misfire：one-shot 超 grace → `missed` + admin 可查 + 无副作用；grace 内正常执行
- [x] 6.5 recurring：跨多 occurrence 恢复不回放（≤1 条 skipped + 下一未来 occurrence）；不产生 6 次补执行
- [x] 6.6 状态联动：suspended 挂起（无 skip 刷屏）→ resume 按规则前进；revoke → 禁用；admin suspend/resume/revoke 端点
- [x] 6.7 重启恢复：running → failed（`interrupted_by_restart`）不重放副作用；`(job_id, scheduled_for)` 去重
- [x] 6.8 DST contract（纯计算，无 PG）：spring-forward 缺失时刻前进、fall-back 首次出现单次触发、UTC 对照、interval 绝对推进
- [x] 6.9 投递链路：execution 收束产出 pending intent（`sched:<execution_id>`），delivery worker 按既有状态机投递；投递失败不回改 execution 终态
- [x] 6.10 admin 端点权限：非 admin → 404/403
- [x] 6.11 既有 legacy 测试不回归（`test_scheduler_service.py`/`test_schedule_tool.py`/`test_remind_tool.py`/`test_tool_scheduler_ownership.py` 零改动通过）

## 7. 验证与归档

- [x] 7.1 PR diff 范围检查：无 `proactive_v2/`、无 work queue flow handler、无 C2 outbox schema 改动（tick 恢复语义分离断言）
- [x] 7.2 pyright + 全量回归（PG 实库）全绿
- [x] 7.3 evidence 目录：验收测试输出 + diff 范围检查记录
- [x] 7.4 归档 change、spec `explicit-schedules` sync、checklist/roadmap 回填（M-P1 收口）
