## Why

M-P1 最后一项 capability：显式用户 schedule（reminder/cron）目前落在全局 `schedules.json` + 内存 `asyncio.create_task`（`agent/scheduler.py`），owner 只有 `channel/chat_id`（模型可在 dev/owner 路径指定任意推送目标），one-shot 超过 5 分钟 grace 被静默丢弃（无 miss 记录、admin 不可见），execution attempt/terminal outcome/delivery intent 不持久，重启恢复只对 JSON 内存快照做「过期即丢弃」。PILOT_ROADMAP §5.9.14 与 §10 DECIDED 已冻结目标语义（tenant/account/conversation owned durable business work、`(job_id, scheduled_for)` 幂等、suspend 暂停/revoke 禁用、recurring 不回放全部 missed），§5.9.6 把「显式用户 schedule/execution」列为 P1 公网门禁 PostgreSQL durable control plane 的必收对象。依赖前置 C5（owner principal）、C1（tenant/canonical conversation 派生）、C2（outbox intent 模式）均已归档；C7 已提供租户工具隔离与触发前 revocation 重校验的既有锚点。

## What Changes

- **新增 `scheduled_jobs` / `schedule_executions` 两表 + Alembic 迁移（expand-only）**：job 行显式携带 `tenant_id` / `account_id` / `conversation_id`（FK canonical_conversations）与**服务端解析冻结的 delivery binding**（`delivery_channel` + `delivery_target`，创建时解析、模型/客户端不可提交）；`schedule_spec_json`（trigger/tier/when/cron/interval）、IANA `timezone`、`revision`（计划版本）；execution 行以 `(job_id, scheduled_for)` 唯一幂等，持久化 attempt、terminal outcome（`succeeded/failed/missed/skipped`）、`skip_reason`、触发时的 `schedule_timezone`/`schedule_revision` 与产出的 delivery intent/message 引用。
- **新增 `ScheduleRepository`（PG）+ 触发事务**：到期认领（幂等 claim）→ instant 直接产生 canonical assistant message + `outbound_delivery_intents` 行（idempotency `sched:<execution_id>`，投递完全复用 C2/C10 delivery 状态机与既有 delivery worker）；soft 经 agent loop 生成内容后同事务落 message + intent；execution 终态与 message/intent 同事务收束。
- **新增 `DurableSchedulerService`（`bootstrap/schedule_durable.py`，PG 后端激活）**：1s tick 复用既有时间计算（`compute_fire_at`/`next_cron_fire`/`LatencyTracker`）；one-shot 超 5min grace（PROPOSED DEFAULT 保留）→ `missed` 且 admin 可查、不静默删除；recurring 不回放全部 missed——跳到下一未来 occurrence 并对跳过边界记录 `skipped` 行；启动恢复扫描把中断 `running` execution 标 `failed`（不重放副作用，`(job_id, scheduled_for)` 去重）。
- **状态联动**：账号 `suspended` → 暂停新执行（tick 挂起、不产生 skip 刷屏），恢复后按 misfire 规则从下一未来 occurrence 继续；账号 `revoked` → job 置 `revoked` 禁用；触发前保留 C7 `RevocationGate` 重校验（fail-closed）。
- **工具面收口**：`ScheduleTool`/`RemindTool` 的 delivery 目标在 durable 路径一律取 `ToolExecutionContext` 服务端注入值并冻结为 binding（user principal 的模型参数本就被 C7 registry 剥离，负向测试固化）；新增 `suspend_schedule`/`resume_schedule` 语义入口（工具层 list/cancel 语义不变）；非 PG 后端沿用旧 `SchedulerService`（dev 单用户兼容路径，零改动）。
- **新增 admin misfire 查看面（`/api/admin/schedule*`）**：jobs 列表（status 过滤）、executions 列表（含 `missed`/`skipped` 过滤）、suspend/resume/revoke 处置。
- **不触碰**：proactive/optimizer tick 实现本体（`proactive_v2/modules_schedule.py` 与 work queue `proactive/optimizer` flow 零改动，恢复语义分离由 PR diff 范围检查验证）；C2 outbox/delivery 表 schema（只按 intent 模式消费）；`schedules.json` 旧文件在 PG 模式下不再读取（仅非 PG dev 派生物，不自动迁移）。

## Capabilities

### New Capabilities

- `explicit-schedules`: tenant/account/conversation owned 显式用户 schedule（durable 存储、服务端 delivery binding、`(job_id, scheduled_for)` 幂等执行、misfire/recurring 恢复语义、suspend/revoke 状态联动、重启恢复、outbox 投递、admin misfire 查看、IANA 时区与 DST contract、与 proactive/optimizer tick 的分离边界）。

### Modified Capabilities

<!-- 无 SHALL 级变更：webchat-durable-storage / delivery 相关 spec 的 outbox 语义不变，
本 change 是该 intent 模式的第二个写入方；tenant-tool-isolation 的路由字段剥离规则
本就要求 user principal 不可指定推送目标，本 change 在 schedule 域固化同一语义。 -->

## Impact

- **代码**：`alembic/versions/`（新迁移）、`bootstrap/db/models/schedule.py`（新）、`bootstrap/db/repository/schedule_repo.py`（新）、`bootstrap/schedule_durable.py`（新 durable 服务）、`agent/scheduler.py`（legacy 保留 + 新增工具面 Protocol/`create_job` 异步包装）、`agent/tools/schedule.py`、`agent/tools/remind.py`（创建路径走 binding 冻结）、`bootstrap/toolsets/schedule.py`（后端条件装配）、`bootstrap/tools.py`（durable scheduler 注入 session_factory）、`bootstrap/auth/api.py`（admin 端点）、`check_schedules.py`（PG 查询适配）、`agent/config_models.py`（`[scheduler] misfire_grace_seconds`）。
- **测试**：`tests/schedules/`（新：创建冻结/幂等/misfire/recurring/suspend/revoke/重启恢复/owner 负向/DST contract/admin 端点）；既有 `tests/test_scheduler_service.py` 等保持通过（legacy 路径不回归）；全量回归。
- **部署**：生产（backend=postgres）重启后 scheduler 从 PG 恢复；存量 `schedules.json` 任务不自动迁移（dev 派生物），如需保留由用户/管理员按新工具重建；delivery 全部经 outbox（WebChat/Telegram 已注册路由）。
