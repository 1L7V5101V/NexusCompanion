# C7 工具隔离 — 实施任务

> 依据 design.md ADR-1..8 与 specs/tenant-tool-isolation；证据统一落 `openspec/evidence/c7-tool-isolation/`。
> 管理闭环：任务 checkbox → `openspec status` → evidence → 仅在有 evidence 时更新 `PILOT_ROADMAP_PROJECT_CHECKLIST` 状态。
> 约定：实现默认在 main 直接分任务提交（同 C8 后期模式）；PG 集成测试跑 `NEXUS_REQUIRE_PG=1`（本地 Docker PG 5433）。

## 1. 前置核对与契约冻结

- [x] 1.1 **程序化工具清单**：脚本导出当前实际注册的全部 tool id + toolsets 归属 + 现有 risk 标签（**含 memory engine tool_profile 经壳类动态注入的工具，按注册来源归类**，如 rachael 的 `reinforce_memory`），与 design ADR-3 白名单逐项核对（发现清单外工具回到 design 补决策）。验证：清单文件入 evidence，diff 无未决项
  → 完成（2026-09-27）：`scripts/tool_inventory.py` + evidence `tool-inventory.{json,md}`；对账收敛（未决项 0）；三项决策回写 design：`tool_search` 加入允许、workspace MCP 管理（`mcp_add/remove/list`）归关闭、`peer_agent` 修正为 `delegate_*` 动态名类别；发现 `AgentRestartTool`/`WorkspaceMcp*` 注册点模块缺失（运行时零注册，design 已注明）
- [ ] 1.2 **effect 等级逐工具映射表**：按 ADR-4 为每个白名单内工具定 effect 初值与 `requires_compensation`，白名单外工具默认关闭（**覆盖 memory engine tool_profile 动态注入工具，按引擎声明 risk 映射，见 ADR-3 类别规则**）；产出映射表入 evidence 并在 PR 描述中列全（第二双眼睛核对）。验证：映射表评审通过
- [x] 1.3 **`tool_audit_events` 迁移设计**：alembic 迁移（控制面 PG）+ SQLite 单机等价落点定案（复用 control-plane 双 adapter 模式或显式日志兜底，二选一并写入 design 附录）。验证：迁移在 scratch 库 `upgrade head` 通过
  → 完成（2026-09-27）：定案 = PG 新表 `tool_audit_events`（迁移 `b3f7a1c5d9e2`，与 C2 `tool_calls` 边界 = 追加型审计流 + `tool_call_id` 软引用 + `rejected` 态）+ SQLite 单机结构化日志兜底（不建表，见 design ADR-6 附录）；scratch 库 `upgrade head` 全链验证通过（evidence `task-1.3-migration.txt`）

## 2. ToolExecutionContext 与 registry 语义反转（ADR-1/2）

- [x] 2.1 新增 frozen `ToolExecutionContext`（`infra/` 或 `agent/tools/`，含 `path_resolver` 引用）+ 工厂（从 turn/channel 派生）。验证：单元测试覆盖字段不可变与派生来源
  → 完成（2026-09-27）：`agent/tools/context.py`（frozen dataclass + `ResourceScope` + `TenantPathResolver` Protocol + `tool_kwargs()` 注入面）；`tests/test_tool_execution_context.py` 5 项单测（不可变/空 tenant fail-closed/空 turn 拒绝/kwarg 只暴露三身份键/scope 判定）全绿
- [x] 2.2 `ToolRegistry.execute` 支持 `context=` 必填路径：合并语义反转为 context 优先，arguments 归属字段丢弃；`set_context()` 降级为非授权兼容 shim。验证：新增单测——模型参数携带 `tenant_id`/`session_key` 被忽略；全量工具调用点改造后 `pytest -q -W error tests/` 全绿
  → 完成（2026-09-27）：registry `execute(name, arguments, context=)` 三层合并（arguments 剥离 `TRUST_ARGUMENT_FIELDS` → 兼容 `_context` 仅非可信键 → `context.tool_kwargs()` 最高优先级）；`set_context()` 丢弃 trust 键并告警。穿线：`DefaultReasoner.run_turn` 构造 context（msg/session/turn 派生；account/principal 真实值随 task 3.2/6.1 接 C5 补全）→ `Reasoner.run(tool_context=)` → `_execute_tool` 闭包；`looping/core._run_agent_loop` + `AgentLoopRunner` 协议同步加参；`looping/handlers.py` spawn 完成链路改显式构造（dev 回退身份）；`before_reasoning` 停止写 trust 键；`spawn.py` 改读注入 kwargs。新负向测试 `tests/test_tool_registry_context.py` 4 项（参数覆盖无效/context 压过 stale/set_context 丢 trust/无 context 宁空不串）；全量回归 **1647 passed, 0 failed**；pyright 37 errors 全部为未触碰文件的既有项（基线计数差异源于 C8/C15 新文件），本改动零新增
- [x] 2.3 reasoning schema 注入按 context 对应 catalog 过滤（`agent/lifecycle/phases/*` 切换）。验证：白名单外工具不出现在 schema 的集成测试
  → 完成（2026-09-27）：`agent/tools/catalog.py`（ADR-3 允许/关闭清单冻结 + `tenant_visible_names()`）；`passive_turn.run()` schema 组装处按租户 principal 过滤（dev/owner 路径不过滤保持现状）；memory engine 注入工具以 `source_type="memory_engine"` 注册标记、按来源放行（`register.py`）。测试 `tests/test_tenant_tool_catalog.py` 6 项（关闭面不可见/dev 全量/不凭空发明/engine 注入可见/未启用引擎不可见/清单互斥）全绿
- [x] 2.4 删除 `_context` 授权语义残留（全量迁移完成后移除兼容路径，不留悬置）。验证：代码 grep 无 `set_context` 授权调用点；pyright 对齐基线
  → 完成（2026-09-27）：`ToolRegistry.set_context/get_context/_context` **整体删除**；非可信提示键（`current_timestamp`/`current_user_source_ref`）收编进 `ToolExecutionContext`（`tool_kwargs()` 注入，模型参数不参与覆盖）；`before_reasoning._SyncToolContextModule` 模块删除（工厂签名收敛）、handlers 改 context 字段；相关测试迁移（p5/p6/spawn/lifecycle）。全量回归 **1651 passed, 0 failed**；pyright 37 errors 全为未触碰文件既有项，零新增

## 3. 三层目录与白名单执行（ADR-3）

- [ ] 3.1 TenantToolCatalog 消费 C8 `TenantRuntimePlan`：per-tenant view = 白名单 ∩ 已启用 ∩ binding 有效；AdminCatalog 保持现状。验证：PG 集成测试——双租户各自目录互异且不含关闭工具
  → 主体完成（2026-09-27）：目录层 `agent/tools/catalog.py` + 两个消费点（schema 过滤 task 2.3、pre-hook 重查 task 3.2）已落地并有单测；PG 双租户集成验证并入 task 8.2 跨租户负向测试（同需 PG 双账号环境），完成后一并勾选
- [ ] 3.2 pre-tool hook 执行前重查（账号状态/binding/白名单/capability），结构化拒绝错误码冻结。验证：suspended 账号调用被拒的负向测试；错误码进协议 fixture（如涉及）
  → 完成（2026-09-27）：`agent/tool_hooks/tenant_gate.py::TenantToolGateHook`（账号状态委托 C8 `RevocationGate` fail-closed；白名单仅对 user principal 重查；错误码冻结 `tool_denied_account_status`/`tool_denied_tenant_scope`）；`ToolExecutionRequest` 增 `tool_context` 字段（passive 两处构造点传入）；bootstrap 在插件 hook 之前注册 gate。binding/capability 重查归 C14（引擎绑定）与后续 capability 系统，design 已注明。测试 `tests/test_tenant_tool_gate.py` 8 项全绿；全量回归 **1659 passed, 0 failed**
- [x] 3.3 `message_push` 服务端绑定目标校验 + `web_search`/`web_fetch` 限流与 SSRF 基线（内网/loopback/metadata 拒绝）。验证：负向测试（任意目标、内网地址被拒）
  → 完成（2026-09-27）：registry 路由字段精化（`ROUTING_ARGUMENT_FIELDS` 仅 user principal 剥离并打标 `_routing_overridden`；dev/owner 保留合法跨目标推送）+ `message_push` 显式拒绝（`push_target_not_allowed`，不静默改址）；SSRF 基线已有实现确认覆盖（loopback/private/link-local/reserved + .local/.localhost 含云 metadata 169.254）；新增 `agent/tools/rate_limit.py` 租户级滑动窗口限流（web 两件接入，仅 user principal）。测试 `tests/test_tool_routing_and_limits.py` 12 项全绿；全量回归 **1688 passed, 0 failed**

## 4. effect policy 与 typed outcome（ADR-4）

- [x] 4.1 工具基类 + `ToolResult` 增加 effect 枚举与 `tool_call_id`；终态枚举接 C2 control plane tool_call 状态。验证：单测——每白名单工具声明 effect；无补偿的 external-write 调用被默认拒绝
  → 完成（2026-09-27）：`agent/tools/base.py` 新增 `ToolEffect`（七级 StrEnum）+ `RISK_TO_EFFECT` 保守映射 + `ToolResult.tool_call_id`；`ToolMeta` 增 `effect`/`requires_compensation`（register/fork 全链携带）；registry.execute 分配并附着 tool_call_id（str 返回路径无法附着，随 7.1 审计管道补全）。effect 初值按 effect-mapping.md 在注册点显式覆盖：message_push/web_search/web_fetch→network、shell/spawn/spawn_manage/task_stop→process-exec、mcp_add/remove+load_skill→admin；engine 注入工具按引擎 risk 自动映射（external-side-effect → external-write 无登记即拒，符合保守规则）。C2 `record_tool_call`/`finish_tool_call` 已存在（CAS 终态），写入接线随 7.1 审计管道一并落地
- [x] 4.2 process-exec/admin 等级对普通 tenant 一律拒绝（shell/spawn/peer_agent 关闭面）。验证：关闭工具直接调用被结构化拒绝的负向矩阵
  → 完成（2026-09-27）：`registry._effect_allowed()` 执行面强制（仅 user principal；dev/owner 不受限）：process-exec/admin 一律拒、external-write 无 `requires_compensation` 拒、其余放行；错误码 `tool_denied_effect`。测试 `tests/test_tool_effect_enforcement.py` 8 项（含 risk 自动映射与 dev 路径不受限）全绿；全量回归 **1667 passed, 0 failed**

## 5. TenantPathResolver（ADR-5）

- [x] 5.1 实现 resolver（attachments/scratch/exports/mcp root + `resolve_relative`）+ 多租户/单机双布局。验证：逃逸矩阵单测（绝对路径/`..`/symlink/越权类别全拒）
  → 完成（2026-09-27）：`agent/tools/path_resolver.py`（`file_root`/`attachments_root`/`scratch_root`/`exports_root`/`mcp_root`/`resolve_relative` + `_tenant_dirname` 跨平台清洗 + `_safe_segment` 拒绝分隔符/点段）；registry `set_path_resolver` + execute 注入；bootstrap 按 `multi_tenant = auth.enabled ∧ backend=postgres` 构造（design ADR-5 开关定案）。测试 `tests/test_tenant_path_resolver.py` 9 项（绝对路径/~ /`..`/深度/空/确定性/跨租户隔离/单机回退/资源段拒绝）全绿
- [x] 5.2 文件工具（read_file/list_dir/write_file/edit_file/read_image_vision）切换到 resolver；写入临时文件+原子替换。验证：跨租户文件互不可达的集成测试；单机模式回归不变
  → 完成（2026-09-27）：五工具统一经 `_effective_root(kwargs, allowed_dir)` 接线（filesystem.py×4 + vision.py×1）；跨租户互不可达 + 单机模式 legacy root 行为不变由 `test_multitenant_roots_isolate_tenants`/`test_single_tenant_mode_keeps_legacy_root` 覆盖（经真实 registry 执行链）；全量回归 **1676 passed, 0 failed**。（原子替换：write_file 既有实现已含临时文件写入路径，本任务未改动其语义）

## 6. 封禁联动与后台任务（ADR-7）

- [x] 6.1 封禁事件 → active tool_call 按 tenant 取消传播（复用 C8 revocation recheck + 连接注册表机制）。验证：封禁时执行中调用收束、其他租户不受影响的集成测试
  → 完成（2026-09-28）：`agent/admission/tool_cancellation.py`（进程级租户取消注册表，`register/unregister/cancel_tenant/active_count`）；`registry._execute_managed` 执行中工具挂入注册表——租户取消转结构化取消结果（`tool_cancelled_account_status`，turn 继续收束）、外层 turn 取消原样上抛；admin suspend/revoke 端点经 `_cancel_tenant_tools` 传播（C1 canonical conversation → tenant_id，fail-safe 不阻断封禁，响应增 `cancelled_tool_calls`）。**3.12 行为核对**：`Task.cancel()` 会把取消转发给被 await 的子任务（外层取消也会让工具任务收到 CancelledError）——仅凭 `task.cancelled()` 无法区分来源，改为 cancel_tenant 对 handle 打标 + 一次性消费判别。测试 `tests/test_tool_cancellation.py` 7 项全绿；全量回归 **1695 passed, 0 failed**；pyright 零新增
- [x] 6.2 spawn/scheduler/task_output/task_stop 保存 owner 并执行前重校验；普通账号只能操作自己租户任务。验证：跨租户 task_stop 被拒的负向测试（spawn 保持关闭面，重点在 scheduler/reminder 路径）
  → 完成（2026-09-28）：`ScheduledJob.owner_tenant_id` + `list_jobs/cancel_job/cancel_job_by_name(tenant_id=)` 租户过滤；schedule/remind 创建时打 owner（context 注入）；list_schedules 只列本租户（user principal）、cancel_schedule 跨租户显式拒绝（`task_foreign_tenant`）；`_execute` 触发即按 owner 重校验（soft 路径补齐，RevocationRejected 上抛保 C8 契约）；`_BackgroundTask.owner_tenant_id` + 两处创建点透传，task_output/task_stop 归属校验（跨租户不 kill/不删）。关闭面确认：shell/task_stop/spawn/spawn_manage 均为 process-exec（4.2 闸门），task_output read-only 靠 owner 校验防护。测试 `tests/test_tool_scheduler_ownership.py` 14 项全绿；全量回归 **1709 passed, 0 failed**；pyright 零新增

## 7. 审计与 C12 §8.2 承接（ADR-6）

- [ ] 7.1 `tool_audit_events` 写入接线（双 adapter）+ 参数脱敏（secret 键只存 hash、内容截断）+ 指标 label 进 C12 白名单。验证：调用后审计行存在且无法还原敏感原文；C12 change §8.2 对应条目可勾选（同步更新 c12-observability-backup/tasks.md）
- [ ] 7.2 drift flow 与插件工具走同一 context/effect 校验（ADR-8），插件工具 catalog 接到同一 catalog 源。验证：插件工具与 drift 文件工具的越界负向测试

## 8. 测试闸门与证据

- [ ] 8.1 **§5.8.8 十二条闸门逐条对账**：每条映射到测试或 evidence 文件，产出对照表。验证：对照表 12/12 有落点
- [ ] 8.2 跨租户并发交错测试（共享 registry 无串租户）。验证：交错测试在 CI/本地稳定通过（≥3 次重跑）
- [ ] 8.3 回归与基线：`NEXUS_REQUIRE_PG=1 pytest -q -W error tests/` + `pyright --level error`（project + tests 两配置）对齐 main 基线。验证：evidence 回归记录
- [ ] 8.4 checklist 回填：webchat-auth-wiring 运维边界解除说明 + C7 状态更新（仅在有 evidence 时）；公网 blocker 更新为「存储切换」
