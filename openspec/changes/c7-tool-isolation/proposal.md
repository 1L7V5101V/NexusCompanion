# C7 工具隔离 — proposal

## Why

WebChat 公网开放前的最后一道安全闸门（PILOT_ROADMAP §5.9.10 第 7 项、checklist current blocker）。当前任何拿到 WebChat session 的主体即拥有全实例工具能力：`ToolRegistry` 的共享可变 `_context` 在并发 turn 间切换授权身份，且 `execute` 按 `{**self._context, **arguments}` 合并——模型参数可以覆盖系统注入的 tenant 字段（§5.8.1 点名的硬冲突）；生产 `toolsets` 含 `spawn`、`shell.py` 在位；文件工具只有 `allowed_dir` 基础校验、无 tenant 路径解析器；工具无 effect policy、无统一审计终态。该状态不得对非 owner 开放。

## What Changes

- **ToolExecutionContext（不可变）**：引入 frozen `ToolExecutionContext`（`request_id`/`account_id`/`tenant_id`/`session_id`/`turn_id`/`channel`/`chat_id`/`principal_type`/`capabilities`/`resource_scope`/`path_resolver`），由服务端从认证身份、可信 channel binding 和当前 turn 派生；工具执行 API 演进为 `execute(name, arguments, context=...)`，系统 context 优先且不可被 arguments 覆盖，共享 `set_context()` 降级为兼容适配并移除授权语义。
- **三层工具目录**：GlobalToolRegistry（内置 + system MCP + admin-only）/ TenantToolCatalog（按 tenant 解析可见工具 view）/ AdminToolCatalog；reasoning 只看到当前 context 对应的 schema，执行前仍重新校验（账号状态、binding、启用、capability、确认/配额）。
- **effect policy 与 typed outcome**：工具声明 `read-only / tenant-local-write / external-read / external-write / network / process-exec / admin` 作用等级；每次调用产生 `tool_call_id` 与审计终态；external-write / process-exec / admin 在无幂等/outcome/补偿策略时默认拒绝，不以字符串错误伪装 typed terminal。
- **TenantPathResolver 与 tenant workspace 布局**：`workspace/tenants/<tenant_id>/{attachments,scratch,exports}` 等资源类别由服务端解析 root；拒绝绝对路径、`..` 逃逸与符号链接逃逸；文件工具统一走 resolver。
- **普通 tenant 默认关闭面**：`shell`、`spawn`/`spawn_manage`、`peer_agent`、plugin 管理、system MCP 管理、用户 MCP（整体不在本 change 开放，负向验证保持关闭）；`message_push` 仅接受服务端绑定目标；drift flow 与插件自带工具复用同一 scope/effect policy。
- **封禁/撤销联动**：账号 `suspended`/`revoked` 时拒绝新调用并按 tenant 取消传播到执行中工具（复用 C8 revocation gate 接缝）；后台任务（spawn/scheduler/task_output/stop）保存 owner tenant 并在真正执行前重新校验。
- **审计字段（C12 §8.2 搭载）**：每次调用记录 tenant/account/turn/工具/effect 等级/状态/耗时/参数脱敏；本 change 落地 C12 §8.2 要求的 tool_call 记录点（E10 指标记录点承接）。
- **测试闸门**：跨租户负向测试 + 并发交错测试（共享 registry context 不串租户）+ §5.8.8 十二条最小闸门逐条验收。

## Capabilities

### New Capabilities

- `tenant-tool-isolation`: 工具调用的多租户隔离契约——ToolExecutionContext 派生与不可覆盖性、三层目录与 tenant 白名单、effect 等级与 typed outcome、tenant 路径解析、封禁联动与后台任务重校验、审计字段与脱敏、跨租户负向边界。

### Modified Capabilities

（无——本 change 不改变既有 capability 的 spec 级行为；`runtimesnapshot-secrets` 的 TenantRuntimePlan/revocation gate 是被本 change 消费的既有接缝，不改其契约。）

## Non-Goals

- **用户 MCP 开放**：不实现 tenant MCP namespace/binding/runtime/catalog（§5.9.7 明确为后续独立 capability）；本 change 只保证其保持关闭并纳入负向测试。
- **外部系统写入的持久授权/确认流**：Pilot 默认拒绝不可逆/高成本/批量操作，确认记录表（arguments_hash/expires_at/one_time）不落地，只在 spec 中冻结拒绝行为。
- **存储切换（SQLite → PG primary）**：独立 change；本 change 的审计写入落在既有控制面库（PG 已在位）。
- **Dashboard admin 面板/总控台 UI**：C12 §8.4 范围。
- **插件安装/激活面改造**：C8 已交付 dormant 安装与 TenantRuntimePlan；本 change 只消费。
- **工具能力矩阵全量补齐**：只为 P1 白名单内工具补 typed outcome，存量 admin-only 工具保持现状。

## Exit Gate

§5.8.8 十二条闸门全部有自动化测试或可复现证据；webchat-auth-wiring 运维边界（evidence README §4「不得对非 owner 开放」）在 checklist 中解除；公网开放 blocker 仅剩存储切换。
