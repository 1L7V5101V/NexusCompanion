# task 2 — 运行时接线（per-work 解析 / 检索 / 工具分发 / ingest 门控）

## 实现锚点

- **work-start 解析一次**：`agent/work_binding.py`（ContextVar，与 C8 snapshot
  lease 同型：work 作用域运行时状态，非授权边界）；`AgentLoopDeps.
  memory_engine_resolver` + `AgentLoop.bind_memory_engine_resolver()`（同
  `bind_persona_resolver` 模式）；`agent/looping/core.py::_process_with_runtime_
  admission` 在 `work_runtime_lease` 作用域内解析并 `bind_work_engine()`，finally
  reset——**work 内零二次解析**。
- **显式穿线**：`TurnState.memory_engine`（`agent/lifecycle/types.py`）←
  `PassiveTurnPipeline.run` 顶部 `current_work_engine()`；before_turn 模块
  （`agent/lifecycle/phases/before_turn.py::_PrepareContextModule`）传
  `engine_binding=state.memory_engine` → `ContextStore.prepare(..., engine_binding)`
  → `RetrievalRequest.engine_binding`；before_reasoning 阶段
  `ToolExecutionContext.memory_engine`（`agent/tools/context.py`，`tool_kwargs()`
  注入 `memory_engine`，模型参数不可覆盖）。
- **检索选择规则**（`agent/retrieval/default_pipeline.py`）：`engine_binding` 非空
  → 直查该引擎（§4.2 每 tenant 单 active）；绑定引擎未构建 → 回退 primary + 警告
  日志；无绑定 → 现行为（单引擎直查 / 双引擎 Router+Sandbox 融合，dev 路径不变）。
  **不触碰** `DefaultMemoryEngine`/`Retriever` 内部（C13 边界）。
- **多引擎工具**：`register_memory_tools_for_engines`（`agent/tools/meta/
  register.py`）——同名工具（recall_memory）注册 `_EngineDispatchTool` 分发器
  （schema 取 primary，执行按 `memory_engine` kwarg 分发、缺省/未知回退
  primary），修复双引擎重复注册启动崩溃；独有工具直注并在 registry document 标
  `source_name=<engine_id>`。C7 目录（`agent/tools/catalog.py`）：
  engine 注入工具以 **active engine 过滤为准**（覆盖静态白名单命中；
  source_name 空的分发器恒可见；无绑定 dev 路径全量可见——现状行为不变）。
- **ingest 独占**：`bootstrap/memory.py` 多引擎时给每引擎
  `event_publisher` 传 `_EngineScopedEventBus` 视图（TurnCommitted/
  ConsolidationCommitted 按租户 active engine 过滤，其余透传；单引擎零包装）。
  reader 由 `bootstrap/app.py` 在 binding 服务就绪后
  `EngineIngestGate.bind_reader()` 后绑定。
- **装配**（`bootstrap/app.py`）：`EngineIngestGate` 创建 → `build_core_runtime
  (engine_ingest_gate=...)`（经 `ToolsetDeps.engine_ingest_gate` 穿线）；PG
  durable 模式建 `TenantMemoryEngineBindingService`（ready_engines=进程内已构建
  引擎、user_selection_allowed=`[memory] user_engine_selection`）→ bind gate
  reader + `bind_memory_engine_resolver`；`create_auth_runtime(binding_ensure=...)`
  → `CanonicalAgentExecutor.provision` 幂等建立初始绑定（未装配时 work-start/
  GET 懒补齐兜底）；`build_chat_server(memory_engines=...)`。
- 配置：`MemoryConfig.user_engine_selection`（默认 true）+ `config.example.toml`
  注释示例；生产 `config.toml`（不入库）本地改 `engine = "default,rachael"`。

## 测试证据（tests/memory_engines/test_runtime_selection.py）

| 验收条目 | 测试 | 结果 |
| --- | --- | --- |
| work 内冻结 / 下个 work 生效（验收 4） | `test_work_binding_frozen_within_work` | PASS |
| 管线按 binding 路由（验收 4） | `test_pipeline_routes_by_engine_binding` | PASS |
| 绑定引擎缺失回退 primary | `test_pipeline_binding_fallback_to_primary` | PASS |
| 无绑定单引擎旧行为 | `test_pipeline_no_binding_single_engine` | PASS |
| 双引擎注册不崩溃 + 分发器标注（验收 2/6） | `test_dual_engine_registration_no_crash_and_dispatcher` | PASS |
| 分发器按 work engine 分发/缺省回退 | `test_dispatcher_routes_by_work_engine` | PASS |
| C7 目录按 active engine 过滤（未启用引擎不可见） | `test_tenant_catalog_filters_by_active_engine` | PASS |
| 非 active 引擎不处理 TurnCommitted + 快照未命中 fail-open（验收 5） | `test_ingest_gate_active_engine_only` | PASS |
| reader 异常 fail-open + async handler | `test_ingest_gate_async_handler_and_reader_exception_fail_open` | PASS |

## 范围核对（验收 9）

- 不碰 C13：`DefaultMemoryEngine`/`Retriever`/dense/keyword/RRF 内部零 diff；
  检索层改动仅 `RetrievalRequest` 新字段与管线选择分支。
- 不碰 C8 RuntimeSnapshot 生成：`agent/plugins/snapshot.py`/生成路径零 diff；
  work-start 复用既有 `work_runtime_lease` 时机。
- pyright（13 个触碰模块）：0 errors（461 warnings 为既有 Unknown 类）。
- 依赖范围 grep（新模块）：无 paradedb/pg_search/jieba/reranker/bm25 导入
  （仅文档注释提及 C13 解耦）。
