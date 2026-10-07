# Tasks: c14-memory-engine-catalog

## 1. DB schema + 仓储 + 服务

- [ ] 1.1 Alembic 迁移：`tenant_memory_engine_bindings`（`tenant_id` PK、`engine_id`、`tenant_policy_revision` BIGINT DEFAULT 0、`created_at`/`updated_at`/`updated_by`）+ `tenant_memory_engine_events`（id、tenant_id、engine_id、action CHECK IN ('initial','switch')、revision、actor、created_at，`(tenant_id, engine_id)` 索引）；expand-only，downgrade drop 两表
- [ ] 1.2 模型 `bootstrap/db/models/memory_engine.py` + 仓储 `bootstrap/db/repository/memory_engine_repo.py`（get/ensure-幂等/switch 单事务（UPDATE revision+1 + INSERT 事件）/list_events；IntegrityError 幂等回读）
- [ ] 1.3 目录 + 服务 `bootstrap/memory_binding.py`：`MemoryEngineCatalogEntry`/`MEMORY_ENGINE_CATALOG`（default=default_on、rachael=opt_in；无 inspector）、`TenantMemoryEngineBindingService`（ensure_binding/get_active_engine/switch（校验→revision+1→事件）/describe（目录+ready+active）/进程内快照缓存；自定义异常 `UnknownEngineError`/`EngineNotReadyError`/`EngineNotSelectableError`）

## 2. 运行时接线

- [ ] 2.1 work-start 解析：`agent/memory/work_binding.py`（ContextVar bind/current/reset）+ `AgentLoopDeps.memory_engine_resolver` + `AgentLoop.bind_memory_engine_resolver()`；`_process_with_runtime_admission`（及 consolidation lease 入口）lease 内解析一次并 bind（含 revision 读取，供后续 plan 消费；异常日志 + 空 bind）
- [ ] 2.2 `TurnState.memory_engine` 字段；`PassiveTurnPipeline.run` 顶部从 work binding 落 state（空则直查 resolver 兜底）
- [ ] 2.3 检索穿线：`RetrievalRequest.engine_binding` + before_turn 模块传参 + `AgenticRAGPipeline` 选择规则改为 binding 优先（有绑定→单引擎直查，缺失回退 primary+日志；无绑定→现行为）
- [ ] 2.4 工具穿线：`ToolExecutionContext.memory_engine`（+ `tool_kwargs()` 注入）；before_reasoning 阶段从 `state.memory_engine` 戳入
- [ ] 2.5 多引擎工具注册：`register_memory_meta_tools` 跨引擎合并（同名分发器按 `memory_engine` kwarg 分发、schema 取 primary；单引擎行为不变）；registry document 记录 engine_id；`tenant_visible_names` 按 active engine 的 tool_profile 过滤 engine 注入工具（dev/无绑定路径保持全量）
- [ ] 2.6 ingest 独占：`bootstrap/memory.py` 多引擎时 per-engine `EventBus` 包装（TurnCommitted/ConsolidationCommitted 按快照判定 active，其余透传）；单引擎零包装
- [ ] 2.7 装配：`bootstrap/app.py` 建 binding 服务（PG durable 模式）+ `bind_memory_engine_resolver` + provisioning 后 `ensure_binding` + `build_chat_server(memory_engines=...)`；`config.example.toml` 双引擎示例与开关注释；核对生产 `config.toml` `[memory] engine` 含 `default`

## 3. API 端点

- [ ] 3.1 `bootstrap/chat_api.py`：`GET /api/memory/engines`（目录+ready+active；仅 `memory_engines` 装配时存在）+ `PUT /api/memory/engines/active`（`_EngineSelectionBody` 模块级；校验目录/opt-in/readiness → 404/403/409；同 engine 幂等；成功返回新状态）；auth + `_resolve_endpoint_identity` 三行模式
- [ ] 3.2 端点错误码与幂等语义按 spec（`unknown_engine`/`engine_not_selectable`/`engine_not_ready`；同 engine 不提升 revision）

## 4. 前端 selector

- [ ] 4.1 `frontend/chat/src/memory.ts`（fetchMemoryEngines/setActiveEngine，404 → 功能不可用）+ `MemoryEngineSelector` 组件（目录项能力/状态/active 展示、不可用禁选、错误行、提交态）；App 集成（PG+auth 模式才渲染）
- [ ] 4.2 `npm run typecheck` + `npm run lint` + `npm run build:chat`，产物提交 `static/chat/`

## 5. 测试与验收

- [ ] 5.1 `tests/memory_engines/`（conftest 仿 persona scratch DB）：目录只读（GET 无 inspector、客户端字段无授权效果）；负向三连（目录外 404 / not ready 409 / 未放行 403）+ 未认证 401；revision 提升 + 同 engine 幂等不提升；单 active 约束 + ensure 幂等 + 并发竞态；切换生效时机（work 内冻结、下 work 生效）；数据隔离（switch 后旧引擎数据不变 + 事件表可按 (tenant_id, engine_id) 追踪）+ 非 active 引擎不写入；inspector admin-only（不入目录、PUT 拒绝）
- [ ] 5.2 多引擎注册/分发单测（双引擎启动不崩溃；`recall_memory` 按租户 active engine 分发；C7 目录过滤）+ 检索选择规则单测（binding 命中/缺失回退/无绑定现行为）
- [ ] 5.3 依赖范围检查：grep 无 BM25/ParadeDB/pg_search/jieba/reranker 导入（新模块）；PR diff 范围核对（不碰 C13 召回内部与 C8 snapshot 生成）
- [ ] 5.4 全量回归：`NEXUS_REQUIRE_PG=1 NEXUS_TEST_PG_URL=... uv run --no-sync pytest tests/ -q --ignore=tests/test_web_chat_e2e_dev.py`，对比基线 1935 passed 无新增失败；pyright 无新增错误

## 6. 证据与归档

- [ ] 6.1 `openspec/evidence/c14-memory-engine-catalog/`：任务证据 md + 回归输出 + 依赖范围检查输出 + checklist 回填
- [ ] 6.2 `openspec validate --change c14-memory-engine-catalog`；spec sync（archive 时 merge 到 `openspec/specs/memory-engine-catalog/`）；PILOT_ROADMAP checklist §5.9.10 第 14 项与 M-P1 行回填；归档合并 main
