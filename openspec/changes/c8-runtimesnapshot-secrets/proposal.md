# C8 RuntimeSnapshot lease + hook failure + tenant secret/revocation

> 对应任务计划：`openspec/openspec-tasks-bundle/task-08-runtimesnapshot-secrets.md`（PILOT_ROADMAP §5.9.10 第 8 项）。
> 输入的已冻结决策（§5.9.16 全节 / §5.9.7 per-task context / §10 DECIDED RuntimeSnapshot/hooks/revocation）不在此重复论证，design.md 逐条引用。
> 跨阶段 change：本 change 先覆盖 **P0 段（lease coverage audit + revocation recheck）**；P2 段（per-task context + hook failure 收尾 + secret 加密）按 task-08 P2 段在本 change 内继续收口，每段独立验收。

## Why

RuntimeSnapshot 基础设施（编译/存储/lease/ContextVar 绑定/发布事务/drain）已在 `agent/plugins/snapshot.py` 落地，但按 §5.9.16「Passive、Proactive、Drift、consolidation、optimizer、recovery work 和 plugin job 在 work start 时各取得一次 snapshot lease」审计现状：

- **Passive 全入口无 lease**：`AgentLoop._process_with_runtime_admission` / `_process` 不持有 snapshot lease，渠道消息、control-plane turn、scheduler soft 任务全部在未绑定 snapshot 的上下文里执行（`bootstrap/tools.py` 里 `getattr(loop, "bind_runtime_snapshot_store", None)` 静默 no-op）；
- **ProactiveLoop 生产构造未传 `runtime_snapshot_store`**（`bootstrap/proactive.py::build_proactive_runtime`），生产 proactive tick 走的是无 lease 的静态回退路径，机制存在但未接线；
- **maintenance 侧无 lease**：`trigger_memory_consolidation`（control-plane 入口）、`_MarkdownConsolidationWorker` 后台任务、`MemoryOptimizer.optimize`/`MemoryOptimizerLoop.run` 都不绑定 snapshot；
- **旧 snapshot 可绕过 revocation**：代码中不存在任何「副作用前再读当前账号/策略状态」的 recheck 接缝，work 一旦启动，账号 suspension/revoked 状态变化对其完全不可见；
- **hook failure 无分层**：`ToolExecutor` 对 post_tool_use 是无超时 fail-open，`EventBus.observe`/`fanout` 无有界 timeout，违反 §5.9.16「gate/interceptor fail-closed；fanout/telemetry 有界 timeout」；
- **无 per-task tenant 接缝**：`TenantRuntimeResolver` / `TenantRuntimePlan` / `PluginInvocationContext` / `tenant_policy_revision` 代码中零命中，contribution 无稳定 `contribution_id`/`binding_policy`/`tenant_configurable` 元数据；
- **tenant secret 无静态加密**：全仓无加密原语，插件凭据/密钥以明文存于 config 与 KV。

C8 是批次 1 可早启的并行根；`TenantRuntimePlan` 是 C7（E4：TenantToolCatalog 解析）与 C14（D7：tenant plugin catalog/memory engine slot）的前置 seam。

## What Changes

- **新模块 `agent/admission/revocation.py`** — `RevocationGate`（副作用前 revocation recheck 接缝）：可注入 `TenantStatusProvider`；provider 异常/未知状态 fail-closed 拒绝；Pilot 未接账号库时 provider=None 显式 dev-open（结构化日志记录，不做静默降级）。
- **lease 覆盖补全（§5.9.16 work-start lease）**：
  - `agent/plugins/snapshot.py` 新增 `work_runtime_lease(store)` 统一入口：work start 取一次 lease + ContextVar 绑定，退出统一释放；store 未接线时保持既有行为；
  - `AgentLoop`：deps 增加 `runtime_snapshot_store`/`revocation_gate`，新增 `bind_runtime_snapshot_store()`（接通 `bootstrap/tools.py` 既有 seam），`_process_with_runtime_admission` work start 取 lease + revocation recheck（覆盖渠道消息/control turn/scheduler soft 任务）；
  - `ProactiveLoop`：`bootstrap/proactive.py` 显式传 `runtime_snapshot_store`，tick 开始处 revocation recheck；
  - `trigger_memory_consolidation` 与 `_MarkdownConsolidationWorker` 后台 maintenance task：work start 各取一次 lease；
  - `MemoryOptimizer.optimize`/`MemoryOptimizerLoop.run`：work start 取 lease；
  - `PluginJobRuntime._run_one`：执行前 revocation recheck（lease 已有）；
  - `SchedulerService._execute`：instant 直推前 revocation recheck（soft 经 passive 路径已被覆盖）。
- **新模块 `agent/plugins/tenant_plan.py`** — §5.9.16/§5.9.7 per-task 接缝：`ContributionMeta`（`contribution_id` + 固定 hook/tool/job 类型 + `binding_policy=required|default_on|opt_in` + `tenant_configurable`）、不可变 `TenantRuntimePlan`（`snapshot_id`/`tenant_id`/`tenant_policy_revision`/enabled contributions）、`TenantRuntimeResolver.resolve(snapshot, tenant_id, tenant_policy_revision, bindings)`（未在 plan 中的 contribution 不可见/不可调用）、`PluginInvocationContext`（每次 hook/tool/job 调用显式携带，插件实例不得保存跨 await 的 tenant 状态）。
- **hook failure 分层（§5.9.16）**：`ToolExecutor` pre-tool hook 超时/异常/缺 context 一律 deny（fail-closed），post_tool_use 与 EventBus `observe`/`fanout` 每观察者有界 timeout（默认 5s，可配）+ 失败记录、不改写已提交终态。
- **新模块 `core/crypto/secret_box.py`** — tenant secret 静态加密：AES-256-GCM（`cryptography` 库），sealed 格式 `v1:<key_id>:<nonce>:<ct>`；keyring 多 key 支持 rotation（active 加密、按 key_id 解密、撤销旧 key 后解密失败）；key source = workspace `keys/` 文件（design.md ADR-7 冻结）；`requirements.txt` 显式声明 `cryptography`。
- **snapshot 发布失败回退与 dormant 安装测试**：候选编译/发布失败保留旧 committed snapshot（不发布半成品、不清空当前可用）；插件安装只登记 package/manifest 不执行插件代码。
- **P0 lease coverage audit 报告**：`openspec/evidence/c8-runtimesnapshot-secrets/` 登记 Passive/Proactive/Drift/maintenance/plugin job 全入口 lease 证据与测试入口（每类入口一条测试）。

## Capabilities

### New Capabilities

- `runtimesnapshot-secrets`：全入口 work-start snapshot lease（含进行中 work 不切 snapshot）、副作用前 revocation recheck（fail-closed）、hook failure 分层（gate fail-closed / fanout 有界 timeout）、per-task `TenantRuntimePlan`/`PluginInvocationContext` 接缝与 contribution 元数据（`binding_policy`/`tenant_configurable`）、tenant secret 静态加密与 rotation、snapshot 发布失败保留旧 snapshot、dormant 安装语义。

### Modified Capabilities

- 无（`admission-queue-recovery` 的 lane/queue 行为不变；本 change 只在 work start 增加 lease 与 gate 检查）。

## Non-Goals（明确不做）

- **不建设插件安全设施**（§10 DECIDED）：不做插件签名、恶意代码扫描、sandbox、来源证明、复杂依赖审计；管理员上传=信任，插件以 in-process trusted code 运行。
- **不触碰 C7 工具边界**：`TenantRuntimePlan` 只交付解析 seam；TenantToolCatalog/tool allowlist 执行归 C7。
- **不触碰 C14 边界**：memory engine 选择 UI、engine 切换存储迁移归 C14；本 change 只交付 `binding_policy` 元数据与 plan 过滤语义。
- **不做账号库接线**：`TenantStatusProvider` 的真实账号状态源（accounts 表/admin 操作）归 C5；本 change 交付 gate 接缝、fail-closed 语义与负向测试，Pilot 默认 provider=None dev-open。
- **不实现 tenant binding durable 存储**：`tenant_policy_revision` 与 binding/config 的 PG 化归 C14/C5；resolver 接受调用方传入 revision，dev 默认 `r0`。
- **不做 P3 恢复演练**：revocation/secret rotation 的 runbook 演练归 P3。
- **不改变 proactive/drift 业务语义**：Drift 仍由 proactive tick 路由驱动，在 proactive lease 内执行（本 change 以 audit 记录该事实，不为 drift 单独取 lease）。

## Impact

- **代码**：新增 `agent/admission/revocation.py`、`agent/plugins/tenant_plan.py`、`core/crypto/secret_box.py`；修改 `agent/plugins/snapshot.py`、`agent/plugins/jobs.py`、`agent/looping/ports.py`、`agent/looping/core.py`、`agent/scheduler.py`、`agent/tool_hooks/executor.py`、`bus/event_bus.py`、`proactive_v2/loop.py`、`proactive_v2/memory_optimizer.py`、`core/memory/markdown.py`、`bootstrap/proactive.py`、`bootstrap/tools.py`、`bootstrap/app.py`（接线）。
- **配置**：`[agent.plugins]` 新增可选 `hook_timeout_seconds`（fanout 有界 timeout，默认 5.0）；无其他新配置段。
- **依赖**：`cryptography`（显式声明；venv 已有 49.0.0）。
- **测试**：新增 `tests/c8/`（lease coverage 测试矩阵、热更新不切 snapshot、revocation 负向测试、hook failure 矩阵、plan/metadata 断言、发布失败回退、dormant 安装、secret 加密/rotation）；既有 turn/proactive/plugin/observability 测试回归。
- **行为变化**：所有 work start 将持有 snapshot lease（进行中热更新不再影响执行中 work 的 hook/模块来源）；生产 proactive tick 从静态回退路径切换为 lease 绑定路径（同一 snapshot 内容，行为等价）；revocation gate 在 provider 异常时拒绝副作用（此前无检查点）。
