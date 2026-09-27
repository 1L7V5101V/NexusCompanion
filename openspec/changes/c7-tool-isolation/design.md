# C7 工具隔离 — design

> 对应 roadmap §5.8（工具多租户隔离全节）与 §5.9.7（ToolExecutionContext 注入边界）。
> 本 design 冻结编码前决策；实现任务见 tasks.md，行为契约见 specs/tenant-tool-isolation。

## 现状事实（design 引用的代码行）

| 域 | 事实 | 影响 |
| --- | --- | --- |
| `agent/tools/registry.py:124-132` | `ToolRegistry._context: dict` 共享可变，`set_context()` 原地 update | 并发 turn 共用一份"当前身份"，无隔离 |
| `agent/tools/registry.py:274-281` | `execute(name, arguments)` 按 `{**self._context, **arguments}` 合并 | **模型参数可覆盖系统注入的 tenant 字段（§5.8.1 硬冲突）** |
| `bootstrap/tools.py` | toolsets 装配含 `spawn`；`risk="external-side-effect"` 粗粒度标签 | 无 effect 等级语义；普通 tenant 能拿到 spawn |
| `agent/tools/filesystem.py:280-282` | `read_file` 等 name 为 property；`_resolve_path` 仅 `allowed_dir + resolve()` | 无 tenant 资源类别 root，无逃逸面审计 |
| `agent/tool_hooks/`（C8） | pre-tool gate 已有 fail-closed 接缝；`revocation_gate=dev_open` 已在启动日志生效 | 封禁联动复用该接缝，不新建第二套 gate |
| C8 P2 已交付 | `TenantRuntimePlan`/`Resolver`/`PluginInvocationContext` 接缝，TenantToolCatalog 消费**归 C7** | 三层目录挂在 TenantRuntimePlan 的 tool catalog slot |
| C12 §8.2 | E10 tool_call 记录点无 owner（2026-09-20 清点） | 本 change 承接（proposal What Changes 已声明） |

## ADR-1 ToolExecutionContext 形态与派生点

**冻结**：frozen dataclass（§5.8.1 字段全集），在 `ConversationRuntime` 收束本轮执行时构造一次（`turn_id` 已知后），随 turn 执行链传递；drift flow 与插件 hook 收到的 context 由同一 turn 派生。后台任务只保存 ownership（account/tenant/tool/binding 引用），真正执行前**重新解析并校验**账号与 binding 状态（§5.9.7 第 1 条），不持久化完整 context。

**拒绝方案**：把 context 塞进 `ToolRegistry` 全局字段（重蹈 `_context` 覆辙）；或由 channel 层直接构造（channel 只有入口凭据，没有 turn_id 与 resource_scope）。

## ADR-2 registry 迁移：context 显式化 + 兼容 shim

**冻结**：目标 API `execute(name, arguments, context=ToolExecutionContext)`。合并语义改为 **context 永远优先**：arguments 中的归属字段（tenant/account/session/chat/principal）一律丢弃；非归属的普通业务参数照常传递。迁移期保留 `set_context()`/`get_context()` 作为兼容 shim（仅用于日志与兼容适配，不参与授权），全量迁移后删除（本 change 内完成，不留悬置）。

**分阶段**：先让 `execute` 支持 context 参数并反转合并顺序（单一 commit 内完成语义反转，避免中间态"部分调用有 context"），再逐工具切换到从 context 取资源范围。

## ADR-3 三层目录挂在 TenantRuntimePlan

**冻结**：不新建第二套 registry。GlobalToolRegistry = 现有 `ToolRegistry`（只读定义层）；TenantToolCatalog = C8 `TenantRuntimePlan` 解析出的 per-tenant 可见工具 view（白名单 ∩ 全局已启用 ∩ binding 有效）；AdminToolCatalog = 现有 Dashboard/owner 全量（现状保持，不在本 change 扩面）。reasoning 阶段按当前 context 的 catalog 注入 schema；pre-tool hook（C8 executor）在执行前重查账号状态/binding/白名单——schema 可见不是防线（§5.8.2）。

**白名单冻结**（P1 初始，精确 tool id，依据 §5.9.7 第 4 条与 §5.8.3 表；实现时以 `tasks.md` T2 的程序化清单核对为准）：

- 允许：`recall_memory`、`memorize`、`forget_memory`、`search_messages`、`fetch_messages`、`read_file`、`list_dir`、`write_file`、`edit_file`、`read_image_vision`、`message_push`（服务端绑定目标）、`schedule`、`remind`、`list_schedules`、`cancel_schedule`、`web_search`、`web_fetch`（限流 + SSRF 防护）
- 默认关闭（普通 tenant）：`shell`、`spawn`/`spawn_manage`、`task_output`/`task_stop`（随 spawn 关闭）、`peer_agent`、`load_skill`（插件/skill 管理面）、system MCP 管理、`memory_signal`（内部信号）
- `text`/`stream_text` 为非注册执行面工具，不进目录决策

## ADR-4 effect policy 载体与 typed outcome

**冻结**：`ToolResult`/工具基类新增 `effect` 枚举字段（七级，§5.8.3）；存量 `risk="external-side-effect"` 标签映射为 `external-write`（保守映射，实现时逐个核对）。每次调用分配 `tool_call_id`（turn 内唯一），终态枚举 `success/failed/cancelled/timed_out/unknown`——由 C2 control plane 的 tool_call 状态承载，不新发明第二套状态机。`external-write`/`process-exec`/`admin` 无幂等+outcome 登记即拒绝：登记表 = 代码内声明（`requires_compensation: bool`），Pilot 不建配置表。

**P1 白名单工具的 effect 初值**：memory/message/schedule 类 = `read-only` 或 `tenant-local-write`；文件写 = `tenant-local-write`；`message_push`/`web_search`/`web_fetch` = `external-read`/`network`；白名单内无 `external-write` 工具（`write_file` 限租户目录内，归 `tenant-local-write`）。

## ADR-5 TenantPathResolver 与兼容布局

**冻结**：新建 `TenantPathResolver`（§5.8.5 接口：`attachments_root`/`scratch_root`/`exports_root`/`mcp_root`/`resolve_relative`）。多租户（auth 启用）布局 `workspace/tenants/<tenant_id>/...`；**单机/SQLite 模式回退现有 workspace 根**（Telegram 单机体验不变，与 storage 双 adapter 同哲学）。解析规则：拒绝绝对路径、`..`、symlink 逃逸；写入用临时文件 + 原子替换；大小/深度/扩展名限制进 resolver 常量（§5.8.5 末段）。

**目录迁移**：多租户模式下不迁移存量文件（Pilot 从空历史开始是既定决策）；单机模式根不变故无迁移。

## ADR-6 审计落点（C12 §8.2 承接）

**冻结**：新建控制面 PG 表 `tool_audit_events`（字段 = §5.8.6 的统一 schema：`request_id, account_id, tenant_id, session_id, turn_id, tool_binding_id, tool_name, effect_class, status, arguments_redacted/arguments_hash, duration_ms, error_code, created_at`），alembic 迁移随本 change；SQLite 单机模式写本地等价表（或显式声明不落库并日志兜底，tasks 定夺——倾向复用 control-plane 双 adapter 模式）。脱敏：secret/credential 形态参数（键名含 token/key/secret/password）只存 hash；文件内容类参数截断至定长摘要。C12 §8.2 勾选条件 = 本表接线 + redaction + 对应指标 label 进白名单。

## ADR-7 封禁联动与取消传播

**冻结**：C8 pre-tool revocation recheck 已覆盖"执行前拒新"；本 change 补"执行中收束"——封禁事件（admin suspend/revoke，C5 service 层）→ 按 tenant 取消注册表中的 active tool_call（复用 WebSocket 账号断开同一注册表机制 §5.3），工具不响应取消则由其既有 timeout 兜底（§3.3 行为基线）。**不做**跨实例通知（单进程 Pilot）。

## ADR-8 用户 MCP 与 drift/插件工具

**冻结**：用户 MCP 在本 change 保持整体关闭（`mcp_add`/`mcp_remove` 不进白名单 + 负向测试锁定 §5.8.8 第 12 条）；drift flow 与插件自带工具经同一 `ToolExecutionContext` 与 effect policy（§5.8.8 第 9 条）——插件工具在 C8 的 `PluginInvocationContext` 中已有 tenant 接缝，本 change 把 tool catalog 与 effect 校验接到同一处，不另立旁路。

## 测试策略

- 单元：registry 合并语义反转（context 优先）、resolver 逃逸矩阵、effect 拒绝矩阵、审计脱敏。
- 集成（PG scratch 库）：双账号跨租户负向（工具资源互不可达）、封禁传播、后台任务重校验。
- 交错测试：两租户 turn 并发共享 registry，断言无串租户（§5.8.8 第 11 条）。
- 回归：全量 `pytest -q -W error tests/` + `pyright --level error` 对齐基线（含 NEXUS_REQUIRE_PG=1）。

## 风险

- registry 语义反转会触碰所有工具调用点——以类型系统（context 必填参数）+ 全量回归兜底；预计改动集中在 `agent/tools/registry.py`、`agent/lifecycle/phases/*`、`bootstrap/tools.py`。
- effect 等级映射若错标（把 external-write 标成 tenant-local-write）会放开越权写——tasks 里安排逐工具核对清单并要求第二双眼睛（review checklist）。
