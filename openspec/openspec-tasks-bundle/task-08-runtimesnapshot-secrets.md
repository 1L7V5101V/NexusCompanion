# Task-08 — RuntimeSnapshot lease + hook failure + tenant secret/revocation（runtimesnapshot-secrets）

> 编号对应 PILOT_ROADMAP §5.9.10 第 8 项。状态标记复用 §8。跨阶段：P0 lease coverage audit → P2 per-task context + revocation gate 收尾。

## 元数据

- **所属阶段**：P0 lease coverage audit → P2 per-task context + revocation gate 收尾
- **§5.9 引用**：§5.9.16（RuntimeSnapshot/hooks/credentials/revocation 全节 + memory engine 插件固定表）、§5.9.7（per-task context）、§10 DECIDED（RuntimeSnapshot/hooks/revocation）
- **§6 出口条件引用**：P0 出口「完成 RuntimeSnapshot lease coverage audit，证明 Passive/Proactive/Drift/maintenance/plugin job 都按 5.9.16 绑定 snapshot，且旧 snapshot 不能绕过 revocation」；P2 出口含 per-task tenant context + hook failure/revocation gate
- **状态**：planned

## 目标

落地全入口 snapshot lease 覆盖证明（Passive/Proactive/Drift/maintenance/plugin job 在 work start 各取得一次 lease；进行中 work 不切 snapshot；旧 snapshot 不得绕过 suspension/revocation/secret rotation/资源 ownership）；`installed` / `active generation` / `tenant binding` 三态分离（可安装后 dormant）；`TenantRuntimeResolver` 按 (`snapshot_id`,`tenant_id`,`tenant_policy_revision`) 生成不可变 `TenantRuntimePlan`；`PluginInvocationContext`（插件实例不得保存跨 await 的当前 tenant 状态，ContextVar 只做观测不做授权）；hook policy 分层（gate/interceptor fail-closed；fanout/telemetry 有界 timeout 且不反向改写已提交终态）；副作用前 revocation recheck；tenant secret 静态加密 + rotation；contribution_id/binding_policy 元数据固化（`required|default_on|opt_in` + `tenant_configurable`）。

## 输入

- 上游 change 产出：无（P0 lease coverage audit 可早启，是并行根之一）
- roadmap 冻结决策：§5.9.16 全节、§10 DECIDED（RuntimeSnapshot/hooks/revocation：P0 固化最小正确性 gate、contribution hook/type/binding policy、tenant provisioning 默认 binding/config/namespace、统一 invocation seam、副作用前 revocation recheck；不建设插件安全扫描/sandbox/滚动重启）
- 现有代码锚点：`plugins/`（plugin 目录结构）、`plugins/default_memory/memory_plugin.py`（engine infrastructure）、`plugins/default_memory/plugin.py`（recall inspector）、`agent/` 插件挂载点
- 依赖前置：无

## 输出

- P0：lease coverage audit 报告 + 测试（所有入口绑定 snapshot 且旧 snapshot 不绕过 revocation）
- 代码：`TenantRuntimeResolver` + `TenantRuntimePlan` + `PluginInvocationContext`、hook failure policy（gate fail-closed / fanout 有界 timeout）、snapshot compile 失败保留旧 snapshot
- 加密：tenant secret 静态加密 + rotation 测试（算法归 §10 OPEN FOR P-1 SPEC）
- 元数据：contribution_id / hook/tool/job 固定类型 / binding_policy / tenant_configurable 固化
- 测试/证据：audit 报告 + 测试、执行中热更新测试、revocation 负向测试、hook failure 测试、元数据断言、grep + 加密测试、失败回退测试

## 验收标准

**P0 段（lease coverage audit）**

- [ ] P0 audit 覆盖 Passive/Proactive/Drift/maintenance/plugin job 全入口且测试证明 lease 绑定 — 验证：audit 报告 + 全入口测试（`openspec/evidence/` 复现）
- [ ] 进行中 work 不切 snapshot — 验证：执行中热更新测试
- [ ] 旧 snapshot 无法绕过 suspension/revocation/secret rotation（副作用前 recheck） — 验证：revocation 负向测试

**P2 段（per-task context + revocation gate 收尾）**

- [ ] gate/interceptor 超时/异常/上下文缺失 fail-closed；fanout/telemetry 有界 timeout 且不反向改写已提交终态 — 验证：hook failure 测试矩阵
- [ ] contribution_id 稳定 + 固定 hook/tool/job 类型 + `binding_policy=required|default_on|opt_in` + `tenant_configurable`；未在 plan 中的 contribution 不可见/不可调用/不可隐式触发 — 验证：元数据断言
- [ ] tenant secret 静态加密；不进 tool schema/模型参数/普通日志/metrics label/错误字符串 — 验证：grep + 加密测试
- [ ] snapshot compile/publish 失败保留旧 committed snapshot，不发布半成品，不清空当前可用 — 验证：失败回退测试
- [ ] 插件安装只登记不执行代码；tenant 只能启停策略允许的 contribution，不能创造 AgentLoop 不存在的新 Hook — 验证：dormant 安装测试

> 判定「真正完成」而非「执行过」：lease coverage audit 以 **每类入口一条测试**为证据（不是「已审计」一句话）；revocation 负向测试必须证明旧 snapshot 持有时副作用被拒。

## 独立性边界（不与其他任务重复）

- 本任务拥有：`TenantRuntimePlan` / `PluginInvocationContext` / lease coverage audit / tenant secret 加密 / hook failure policy
- 本任务不触碰：工具 allowlist 执行（C7，只提供 TenantRuntimePlan seam）、memory engine 选择 UI（C14，只提供 tenant plugin catalog + engine slot）、auth 端点（C5）
- 共享 seam 协议：为 C7（E4）提供 `TenantRuntimePlan` 解析的 TenantToolCatalog；为 C14（D7）提供 tenant plugin catalog/memory engine slot 元数据（binding_policy 表）；为 C12 提供 snapshot/revocation 观测

## 依赖

- **左依赖（必须先完成）**：无（P0 audit 可早启，并行根）
- **右依赖（本任务前置于）**：C7（E4）、C14（D7：tenant plugin catalog）
- **可并行**：C1 / C3 / C13

## 风险与需冻结决策

- §10 OPEN FOR P-1 SPEC「Digest/encryption/key rotation」：tenant secret 静态加密算法/key source/rotation 由本 task 与 C5 的 design 指定；密钥轮换/撤销的生效边界必须有测试。
- §10 DECIDED「RuntimeSnapshot/hooks/revocation」：Pilot 不建设插件签名/恶意代码扫描/sandbox/来源证明/复杂依赖审计（管理员上传=信任）；代码变更走新 generation + 旧 lease drain。
- 风险：`memory_plugin.py`（engine infrastructure）与 `plugin.py`（inspector）当前混在 `default_memory` 下 — 按 §5.9.16 拆：engine runtime/自动 ingest/engine-specific tools 按统一 memory plugin 契约接入；inspector 标为 admin-observability，不能当普通 tenant 开关（具体归 C14）。
