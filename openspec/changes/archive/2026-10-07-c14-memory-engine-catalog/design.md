## Context

C8 交付了 `TenantRuntimePlan` seam（`agent/plugins/tenant_plan.py`：`engine_binding`
与 `tenant_policy_revision` 字段已预留）但无生产写入方；binding/config 的 durable
存储与 revision 提升按冻结决策归本 change。当前引擎选择是进程级：`config.memory.engine`
（`bootstrap/memory.py::build_memory_runtime` 按序构建、首个为 primary），检索管线
`AgenticRAGPipeline` 按「引擎数量」选路（1 个→直查；2 个→Router/Sandbox 双源融合），
与租户无关；memory 工具在注册期绑定单一 engine 实例，双引擎同名工具（`recall_memory`）
重复注册会启动崩溃（roadmap §4 已知缺陷，C7 目录把 engine_binding 过滤留给 C14）。
两个引擎都在构造期自订阅 `TurnCommitted`（default 还订阅 `ConsolidationCommitted`），
并存时会对所有 tenant 双写。inspector（`plugins/default_memory/plugin.py`）是 recall
观测插件，dashboard 已 admin-gated。

## Goals / Non-Goals

- **Goals**：服务端冻结目录 + 每 tenant 单 active binding（PG）+ revision 提升；
  work-start 解析一次、进行中 work 不换引擎；检索/工具按 active engine 路由；切换
  不迁移数据、历史可追踪；`GET/PUT /api/memory/engines` + WebChat selector；
  多引擎并存可用（修工具重名崩溃、ingest 独占）。
- **Non-Goals**：C13 召回质量改造（dense/keyword/RRF 内部、ParadeDB/jieba/reranker）；
  C8 RuntimeSnapshot 生成/lease 机制改动；per-tenant 插件 contribution binding 的
  全面 durable 化（C14 只覆盖 memory engine slot；其他 contribution 保持 resolver
  语义）；引擎级数据迁移工具；多进程 binding 失效广播（Pilot 单实例）。

## Decisions

### ADR-1 目录 = 代码冻结常量；DB 只存 binding/revision/历史

§5.9.16 固定表由代码表达（`bootstrap/memory_binding.py::MEMORY_ENGINE_CATALOG`：
`default`=default_on+初始、`rachael`=opt_in+可选实现；inspector 不入目录），避免
「目录存 DB 再被 DB 改」的越权面。DB 三张对象：`tenant_memory_engine_bindings`
（`tenant_id` 主键 + `engine_id` + `tenant_policy_revision` BIGINT + 审计列——主键
即单 active 约束）、`tenant_memory_engine_events`（`(tenant_id, engine_id, action,
revision, actor)` 的 initial/switch 历史，满足「按 tenant_id + engine_id 可追踪」）。
revision 以整数存、以 `r<n>` 字符串供 plan 消费；同 engine 重复 PUT 幂等不提升。

### ADR-2 「管理员允许」的 Pilot 形态 = 配置开关 + readiness

rachael 的 opt_in 放行取 `[memory] user_engine_selection`（用户侧切换总开关，默认
true）∧ 引擎已构建（`engine_names` 含 rachael）。not ready（未构建）/未放行/目录外
分别返回 409/403/404 机器可读码（`engine_not_ready`/`engine_not_selectable`/
`unknown_engine`）。生产 `[memory] engine` 须含 `default`（初始绑定必须 ready）。

### ADR-3 per-work 解析一次：lease 内 ContextVar + TurnState 显式穿线

work-start（`AgentLoop._process_with_runtime_admission` 等 snapshot lease 作用域内）
经 `MemoryEngineBindingService.resolve_active_engine(tenant)` 解析一次，写入
work-scoped ContextVar（`agent/memory/work_binding.py`，与 C8 snapshot lease 同一
「work 作用域运行时状态」模式；非授权边界——授权已由 C7 ToolExecutionContext 承担），
并由 `PassiveTurnPipeline.run` 顶部落入 `TurnState.memory_engine`（新增字段）后全程
显式穿线：before_turn 模块把它放进 `RetrievalRequest.engine_binding`（新字段）；
before_reasoning 阶段戳进 `ToolExecutionContext.memory_engine`（新可信字段，经
`tool_kwargs()` 注入，模型/客户端不可覆盖）。解析异常 fail-open 到进程 primary
（readiness 校验保证 binding 只指向 ready 引擎；降级只影响路由不影响授权）。
work 内零二次解析 → 满足「进行中 work 不换引擎」；下一 work lease 重新解析 → 生效。

### ADR-4 检索管线：binding 优先单引擎路径，双源融合降级为无绑定路径

`AgenticRAGPipeline.retrieve` 选择规则从「按引擎数量」改为「按 `engine_binding`」：
有绑定 → 直查该 engine（`engines[engine_binding]`，缺失时回退 primary 并记日志）；
无绑定（dev/未接线路径）→ 保持现行为（单引擎直查 / 多引擎 Router+Sandbox 融合）。
生产租户 turn 恒有绑定 → 恒单 active engine（§4.2 冻结），融合代码不删除也不被
租户路径触发。`DefaultMemoryEngine`/`Retriever` 内部零改动（不碰 C13）。

### ADR-5 多引擎并存：同名工具分发器 + C7 目录按 active engine 过滤 +
ingest 独占门控

- **工具**：`register_memory_meta_tools` 改为跨引擎合并注册——同名工具（
  `recall_memory`）注册为一个分发器（schema 取 primary 引擎声明，执行按
  `context.tool_kwargs()["memory_engine"]` 分发到对应 engine 实例；缺省回退
  primary），仅单引擎声明的工具照旧直注。C7 `tenant_visible_names` 对
  `source_type="memory_engine"` 的工具按「该 tenant active engine 的 tool_profile
  声明」过滤（registry document 记录 `engine_id`），未启用引擎的工具 schema 不可见。
- **ingest**：`bootstrap/memory.py` 构建多引擎时给每个 engine 的
  `event_publisher` 传入 per-engine 包装（`_EngineScopedEventBus`：`on()` 对
  TurnCommitted/ConsolidationCommitted 包 active-engine 判定，其余透传），判定读
  binding 服务的进程内快照（work-start 解析与 GET/PUT 均会刷新缓存，事件总在
  turn 内晚于解析触发；未命中租户 fail-open 并记日志）。单引擎构建时零包装（行为
  逐字节不变）。

### ADR-6 API/前端沿用 C9/C10 模式

端点为 `bootstrap/chat_api.py` 闭包路由（`durable_runtime + auth_runtime` 双条件
装配，dev 路径不注册 → 404 不泄露）；错误沿用 `{"detail": "<snake_case>"}`；
pydantic body 模块级定义。前端按 feature 模块（`memory.ts`，裸 fetch +
same-origin）+ 聊天头部轻量 selector 组件；构建产物入 `static/chat/`。

### ADR-7 装配与 provisioning

`bootstrap/app.py`：PG durable 模式建 `TenantMemoryEngineBindingService
(webchat_durable.session_factory)`；work-start resolver 经 `AgentLoop.
bind_memory_engine_resolver()` 后接线（同 `bind_persona_resolver` 模式）；provisioning
执行器在建 tenant 分区后 `ensure_binding(tenant)`；chat server 装配
`memory_engines=`（服务 + readiness 视图），None 时端点不注册。

## Risks / Trade-offs

- 「管理员允许」用配置而非 DB 策略表 → Pilot 可接受；后续 per-tenant 策略随
  contribution binding 全面 durable 化扩展。
- ingest 门控快照未命中的租户 fail-open（双写）→ work-start 解析必然先于
  TurnCommitted 触发，正常流不出现；事件流旁路（如历史回放）由测试锁定语义。
- 存量租户懒补齐绑定到 `default`，当前以 rachael 运行的 tenant 需一次切换 →
  部署说明写明，属一次性操作、无数据迁移。
- TurnState/ToolExecutionContext/RetrievalRequest 新增字段均为带默认值的扩展，
  dev/单引擎路径零感知。

## Migration Plan

1. Alembic 迁移（expand-only 两表）→ 2. 服务/端点/前端上线（binding 表空，运行时
懒补齐）→ 3. 生产 `[memory] engine` 增加 `default` 并重启（双引擎 ready）→
4. 需要的租户经 selector 切回 `rachael`。任一步可独立回滚（downgrade drop 两表；
配置回退单引擎后所有租户走 primary，行为与今日一致）。

## Open Questions

- 无。rachael 的 per-tenant 配置覆盖/KV 隔离随 contribution binding durable 化扩展，
  本 change 不覆盖（spec 未声明该 SHALL）。
