# Task-07 — tenant tool context/resource/effect isolation（tool-context-isolation）

> 编号对应 PILOT_ROADMAP §5.9.10 第 7 项。状态标记复用 §8。跨阶段：P0.5 最小接缝 → P1 普通 tenant 开放内置工具 → P2 完成 + 负向测试。用户 MCP（UMCP）为派生节点后置。

## 元数据

- **所属阶段**：P0.5 最小接缝 → P1 普通 tenant 开放内置工具 allowlist → P2 收尾（资源/effect 隔离 + 负向测试）
- **§5.9 引用**：§5.8（工具隔离全节）、§5.9.7（ToolExecutionContext 注入边界）、§6.1 C（tool capability inventory：P0 盘点矩阵）、§5.8.8（最小工具隔离闸门）
- **§6 出口条件引用**：P0.5「完成 ToolExecutionContext、TenantToolCatalog 和统一 ToolPolicy 的最小接缝；P1 认证/资源 scope/负向测试前不得公网暴露 tenant-facing 工具」；P2 出口含工具隔离负向测试 + 用户 MCP 前置
- **状态**：planned

## 设计结论注记（2026-09-05）

> 与 canonical-identity 的 account→N tenant 扩展同批落注记（openspec change `2026-09-05-c1-account-multi-tenant`，见 task-01 注记）。本任务状态仍 `planned`，以下为设计共识，实现时按此展开。

**ToolExecutionContext 结论。** 每 Work 注入一次、`frozen` 不可变；系统派生字段（`account_id` / `tenant_id` / `session_id` / channel 等）由系统侧注入，模型生成或客户端传来的参数只能进 `arguments`，不能覆盖系统字段；`execute` 不再做 `{**system_context, **arguments}` 式扁平合并。

现状缺陷对照（`agent/tools/registry.py`）：共享可变 `_context: dict` + `set_context(**kw)`，`execute` 按 `{**self._context, **arguments}` 合并（`arguments` 后写胜出）。两个篡改向量：(a) 进程内并发 turn/任务改写同一把共享 dict（前一个 `set_context` 覆盖后一个或反之）；(b) 模型参数 / `arguments` 覆盖系统字段。set-then-call 窗口使「调用时读到的是哪份 context」不确定。ToolExecutionContext 的解法是消除共享可变状态与 set-then-call 窗口：系统字段与工具参数分命名空间、按 Work 每 call 传入冻结对象，授权依据是 per-call 注入的 tenant/account，而不是 registry 共享上下文（§5.8.1 执行上下文是唯一可信 scope 来源）。

**为什么不存 `canonical_conversation_id`。** account→N 模型下每个 agent = tenant = 恰好一条 canonical conversation，`tenant_id` 已 1:1 锚定该 agent 的规范会话，再存 `conversation_id` 冗余。工具层按 tenant 取资源（tenant-bound repo / memory），不该绑到具体会话表行；conversation 归属与 binding 是路由 / 持久化层（C10）的事。

**account→N tenant 模型注记。** 一个账号（登录主体）可拥有多个 agent，每个 agent 一个 tenant（即一个 canonical conversation，各自的记忆 / persona 域在 tenant 内，C9）。identity seam 已随 `2026-09-05-c1-account-multi-tenant` 落地（`resolve_by_tenant` 恒单一三元组，账号级 `list_agents` 枚举）。对本任务的影响：ToolExecutionContext 的 `tenant_id` 语义从「资源边界」扩展为「同时是当前 agent 的标识」，字段集合不变；左依赖 C5 + C8 不变。

## 目标

落地不可变 `ToolExecutionContext`（系统派生字段优先且不可被模型/客户端参数覆盖；P0 起 `set_context()` 不再承担授权）；`GlobalToolRegistry` / `TenantToolCatalog` / `AdminToolCatalog` 分层 + 统一 `ToolPolicy`；`TenantPathResolver`；P1 精确 allowlist（精确 tool id，§5.8.8 最小闸门）；普通 tenant 关闭 shell / peer_agent / plugin mgmt / system MCP；`tool_call_id` + audit；capability inventory（§6.1 C 每类工具一行）；跨租户负向 + 并发交错测试。

## 输入

- 上游 change 产出：C5（auth principal，D3）、C8（TenantRuntimePlan seam，E4）、C1（tenant 派生）
- roadmap 冻结决策：§5.8.1（执行上下文是唯一可信 scope 来源）~ §5.8.8、§5.9.7、§6.1 C（capability inventory 字段表 + 当前代码盘点 2026-08-28）
- 现有代码锚点：`agent/tools/registry.py`（GlobalToolRegistry）、`agent/tool_hooks/executor.py`（ToolExecutor pre/invoker/post hooks）、`agent/tools/filesystem.py`、`agent/tools/message_push.py`、`agent/tools/spawn.py`、`agent/mcp/registry.py`
- 依赖前置：C5（D3）+ C8（E4）

## 输出

- 代码：`ToolExecutionContext` 注入、catalog 分层、`TenantPathResolver`、`ToolPolicy`、`tool_call_id` + audit
- 规范：capability matrix（§6.1 C 表格：tool identity/ownership/side effect/timeout/cancellation/retry/idempotency/outcome query/compensation/terminal mapping）
- 测试/证据：参数覆盖负向测试、repository 调用断言、spec + 配置断言、并发交错负向测试、matrix 文档 + 测试、audit 记录检查、UMCP 开放前置检查

## 验收标准

**P0.5 段（最小接缝）**

- [ ] 完成 `ToolExecutionContext` / `TenantToolCatalog` / 统一 `ToolPolicy` 最小接缝；无 P1 认证、资源 scope 和负向测试前不公网暴露 tenant-facing 工具 — 验证：接缝测试 + dev 门禁检查

**P1 段（普通 tenant 开放内置工具）**

- [ ] 系统 context 不可被参数覆盖；`set_context()` 不再承担授权 — 验证：参数覆盖负向测试
- [ ] tenant-facing 工具全部走 tenant-bound repository / 服务端过滤 — 验证：repository 调用断言
- [ ] P-1 allowlist spec 列出精确 tool id（recall_memory/memorize/forget_memory、search/fetch_messages、文件读写、read_image_vision、message_push、schedule/reminder、web search/fetch）；shell / spawn / peer_agent / plugin mgmt / system MCP 默认关闭 — 验证：spec + 配置断言
- [ ] 越权跨租户：共享 Registry context 不串租户 — 验证：并发交错负向测试

**P2 段（收尾）**

- [ ] capability matrix 覆盖每类启用工具的 timeout/cancel/retry/幂等/outcome query/compensation/terminal mapping（§6.1 C 字段全列；不假设所有工具能力一致） — 验证：matrix 文档 + 测试
- [ ] 每次 tool call 产生 `tool_call_id` + audit 终态 — 验证：audit 记录检查
- [ ] 用户 MCP 仅在 tenant namespace / secret ownership / runtime 隔离 / ToolExecutionContext / §5.8.8 负向测试**全部通过**后开放（UMCP）——不作为 P1 Token 登录的发布依赖 — 验证：UMCP 开放前置检查

> 判定「真正完成」而非「执行过」：参数覆盖负向、跨租户并发交错负向必须有失败即拒绝的测试；capability matrix 每行有可复现证据（代码 + 测试），不是文档声明。

## 独立性边界（不与其他任务重复）

- 本任务拥有：`ToolExecutionContext`/catalog 分层/`ToolPolicy`/`TenantPathResolver`/capability matrix/tool_call_id+audit
- 本任务不触碰：RuntimeSnapshot 生成（C8）、auth 端点（C5）、attachment 上传（C6）、memory engine 选择 UI（C14）
- 共享 seam 协议：消费 C8 的 `TenantRuntimePlan`（E4：TenantToolCatalog 由 TenantRuntimePlan 解析）；为 UMCP（D4）提供 ToolExecutionContext + 负向测试闸门

## 依赖

- **左依赖（必须先完成）**：C5（D3）+ C8（E4，TenantRuntimePlan seam）
- **右依赖（本任务前置于）**：UMCP（D4：用户 MCP capability，7 是前置；P2 后置独立 capability）
- **可并行**：C6 / C9 / C10 / C11 / C14（均为 C5 下游，与 C7 无相互依赖）

## 风险与需冻结决策

- §6.1 C「先盘点再统一语义」：第一轮只覆盖 Pilot 启用工具，以代码和现有测试为准；Shell/MessagePush/MCP/文件类分别记录，不因都实现 `Tool.execute()` 就假设能力相同。
- §10 DECIDED「副作用工具契约」：已产生副作用先 outcome query 再 compensation；结果未知标 `unknown`，不盲重试。
- 风险：allowlist 精确性（漏开危险工具）→ 配置断言 + 负向测试双保险；`set_context()` 遗留授权路径 → 验收第 1 条负向测试强制移除。