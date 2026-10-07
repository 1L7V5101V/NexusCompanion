# task 5 — 验收标准对照（task-14-memory-engine-catalog 9 条）

判定基准：`tests/memory_engines/`（23 tests，pytest.mark.postgres，scratch DB
`nexus_memory_engines_test`）+ 全量回归 + 范围检查。

| # | 验收标准 | 证据 | 结果 |
| --- | --- | --- | --- |
| 1 | 只读 catalog 来自服务端允许目录（客户端字段只是设置请求） | `test_catalog_is_frozen_server_side`；API：`test_memory_engines_api_full_ready`（目录仅 default/rachael、smuggled 字段 PUT → 404 unknown_engine）；未装配 404（`test_memory_engines_api_unmounted`） | PASS |
| 2 | 未授权 engine 切换被拒（不在目录 / not ready / 不允许用户侧切换） | `test_unauthorized_switch_rejected`（服务层三连 + 零副作用）；API：`unknown_engine`(404)/`engine_not_ready`(409)/`engine_not_selectable`(403)（`test_memory_engines_api_rachael_not_ready`、`test_memory_engines_api_selection_locked`）+ 未认证 401；同 engine 幂等不提升 | PASS |
| 3 | active binding 持久化到 PG + 提升 `tenant_policy_revision` | `test_switch_persists_and_bumps_revision`（原子 UPDATE +1、switch 事件、新实例重读生效）；API revision 断言 | PASS |
| 4 | 进行中 work 不换引擎；下一 work 取新 plan 后生效 | `test_work_binding_frozen_within_work`（work-start 一次解析 → ContextVar 冻结，切换后当轮仍旧引擎、下轮新引擎）；`test_pipeline_routes_by_engine_binding`（检索按 binding 单引擎路径）；`test_dispatcher_routes_by_work_engine`（工具同 work 同引擎） | PASS |
| 5 | 切换不迁移/合并/删除旧引擎数据；记录按 `tenant_id + engine_id` 可追踪 | `test_switch_does_not_touch_engine_data_traceability`（仓储公共 API 范围断言 = 仅 binding 元数据操作 + 事件表 (tenant_id, engine_id) 历史）；`test_ingest_gate_active_engine_only`（非 active 引擎不写入） | PASS |
| 6 | 每 tenant 始终有且只有一个 active engine；初始绑定为 default | `test_ensure_binding_initial_default_and_idempotent`、`test_single_active_constraint_and_concurrent_ensure`（PK 约束 + 并发 ensure 单行）、`test_concurrent_switch_keeps_single_row`（原子 revision 一致性） | PASS |
| 7 | 不依赖 BM25/ParadeDB/jieba/reranker（与 C13 解耦） | 依赖范围 grep（10 个新模块 import 检查 0 命中，仅文档注释提及 C13 解耦）；测试不依赖 pg_search（scratch DB 仅 vector/pg_trgm 扩展）；`git diff main --stat` 无 `plugins/*/engine.py` 召回内部改动 | PASS |
| 8 | inspector 标为 admin-only，非普通 tenant 开关 | 目录不含 inspector（`test_catalog_is_frozen_server_side` + GET 断言）；PUT inspector → 404 unknown_engine；inspector 为 admin observability 插件（dashboard admin-gated，现状保持） | PASS |
| 9 | 本 task 不触碰召回链路改造（C13）与 RuntimeSnapshot 生成（C8） | PR diff 范围核对：`agent/retrieval/*` 仅 `RetrievalRequest.engine_binding` 新字段与 `AgenticRAGPipeline` 选择分支；`agent/plugins/snapshot.py`、`DefaultMemoryEngine`/`Retriever` 内部零改动（`git diff main --stat` 留档） | PASS |

补充回归修复（保持既有测试语义）：

- `_PrepareContextModule` 对 `prepare` 做签名探测（`engine_binding` 为带默认值新
  kwarg），未声明的 ContextStore 实现（测试 stub 等）零改动兼容
  （`tests/test_load_metrics.py` 的 StubContextStore 路径回归通过）。
- `object.__new__(AgentLoop)` 最小 harness（tests/test_turn_pipelines.py）按 C8
  先例补 `_memory_engine_resolver = None` 接线位。
- `test_tool_kwargs_expose_identity_keys_only` 契约扩展 `memory_engine` 键
  （服务端派生路由键，非客户端可伪造字段；`account_id/session_id/turn_id` 仍不暴露）。
- `tests/test_runtime_smoke.py` 的 `build_core_runtime` patch 收 `**kwargs`。
