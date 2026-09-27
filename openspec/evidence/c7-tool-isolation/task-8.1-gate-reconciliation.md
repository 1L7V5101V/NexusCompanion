# C7 task 8.1 — §5.8.8 十二条工具隔离闸门对账表

> 依据 `openspec/PILOT_ROADMAP.md:911-926` 十二条闸门；每条映射到 C7（及 C8/C5 承接）
> 的测试或 evidence 文件。目标：**12/12 有落点**。
> 对照日期：2026-09-28；全量基线 `pytest -q -W error tests/` = 1735 passed。

| # | 闸门 | 落点（测试 / evidence / 实现文件） | 状态 |
| --- | --- | --- | --- |
| 1 | `ToolExecutionContext` 含 account/tenant/session/turn 四件 | `agent/tools/context.py`（frozen dataclass + fail-closed）；`tests/test_tool_execution_context.py` 5 项 | ✓ 有落点 |
| 2 | 禁止模型/客户端传入可信 identity/tenant/root/目标 chat/权限字段 | registry 语义反转（`TRUST_ARGUMENT_FIELDS` 剥离 + `context.tool_kwargs()` 注入 + `set_context` 删除，task 2.2/2.4）；`tests/test_tool_registry_context.py` 4 项（参数覆盖无效/set_context 丢 trust 等） | ✓ 有落点 |
| 3 | memory/message/session/attachment/dashboard 查询强制 tenant-bound repository | C8 `StorageRuntime.for_tenant` + `infra/storage/tenancy.py`（服务端过滤）；C7 工具侧经 `TenantPathResolver` + canonical repo（6.1 `_cancel_tenant_tools` 用 canonical 查询跨 C1 身份链）；dashboard/admin 查询在 C5/C8 承接 | ✓ 有落点（C8/C5 承接，工具侧 C7 锁定） |
| 4 | 所有文件工具绑定 `TenantPathResolver` | `agent/tools/path_resolver.py`（task 5.1）+ 五工具统一接线（task 5.2）；`tests/test_tenant_path_resolver.py` 9 项 + `test_multitenant_roots_isolate_tenants`/`test_single_tenant_mode_keeps_legacy_root` | ✓ 有落点 |
| 5 | 普通 tenant 关闭 shell / peer_agent / 插件管理 / 全局 MCP 管理 | effect 闸门（task 4.2，process-exec/admin 对 user 拒绝）+ catalog 关闭清单（task 2.3）；`tests/test_tool_effect_enforcement.py` 8 项 + `tests/test_tenant_tool_catalog.py` 6 项 + `tests/test_tool_scheduler_ownership.py::test_spawn_shell_task_stop_closed_face_for_user` | ✓ 有落点 |
| 6 | 用户 MCP 使用 tenant namespace / 独立 binding / runtime / 工具 catalog | **Pilot 定案：用户 MCP 整体关闭**（ADR-8，`mcp_add`/`mcp_remove` 不进白名单 + 负向测试）；`tests/test_tenant_tool_catalog.py::test_tenant_schema_excludes_closed_tools`（mcp_add 不可见）+ 禁止清单互斥测试；开放路径经 tenant catalog（task 3.1/8.2 承接） | ✓ 有落点（关闭面） |
| 7 | `message_push` 只能使用服务端已绑定并授权的目标 | task 3.3：`ROUTING_ARGUMENT_FIELDS` 剥离 + `push_target_not_allowed` 显式拒绝；`tests/test_tool_routing_and_limits.py` 12 项 | ✓ 有落点 |
| 8 | 后台任务保存 owner tenant，output/stop/execute 前重校验 | task 6.2：`ScheduledJob.owner_tenant_id` + list/cancel 租户过滤 + `_BackgroundTask.owner_tenant_id` + task_output/task_stop owner 校验 + scheduler `_execute` 触发前 revocation recheck；`tests/test_tool_scheduler_ownership.py` 14 项 | ✓ 有落点 |
| 9 | drift flow 与插件自带 shell/filesystem/send 工具复用同一 scope/effect policy | task 7.2：drift 工具带同一 `ToolExecutionContext`（dev 回退身份）+ `DriftPathResolver` 逃逸面 + 插件工具同 registry/effect 源；`tests/test_tool_drift_plugin_flow.py` 9 项 | ✓ 有落点 |
| 10 | 每次工具调用具备 tenant/account/turn/tool/effect/status/耗时审计字段 | task 7.1：`tool_audit_events`（双 adapter）+ 脱敏；`tests/test_tool_audit.py` 17 项（含字段完整性、失败/取消/拒绝分类、secret 不入行） | ✓ 有落点 |
| 11 | 跨租户负向测试 + 并发交错测试（共享 Registry 不串租户） | 负向：`tests/test_tool_cancellation.py`（租户级取消不波及他租户）、`tests/test_tool_scheduler_ownership.py`、`tests/test_tool_routing_and_limits.py`、`tests/test_tool_audit.py`；并发交错：task 8.2 专项测试（PG，≥3 次重跑）+ `tests/test_tool_cancellation.py` 双租户并发 turn 交错 | ✓ 有落点（8.2 承接主交错矩阵） |
| 12 | 用户 MCP 不能读取平台 env / 其他 tenant 目录 / DB 连接 / Docker / socket 资源 | 用户 MCP 关闭面（同 #6）：`mcp_add`/`mcp_remove` 白名单外 + schema 不可见（`test_tenant_schema_excludes_closed_tools`）；无 MCP 注入能力则无资源面可触达 | ✓ 有落点（关闭面） |

## 结论

12/12 闸门全部有测试或 evidence 落点（#3 由 C8/C5 承接并已在 C7 工具侧锁定；
#6/#12 由「Pilot 用户 MCP 整体关闭」定案满足——关闭面负向测试锁定）。
全量回归 1735 passed；pyright 零新增。