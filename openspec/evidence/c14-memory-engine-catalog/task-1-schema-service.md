# task 1 — DB schema + 仓储 + 服务（binding/revision/历史/目录）

## 实现

- 迁移 `alembic/versions/c7e9a3f1b5d4_c14_memory_engine_binding.py`（Revises
  f5a9c1e3b7d2）：`tenant_memory_engine_bindings`（`tenant_id` **主键** = 单 active
  约束、`engine_id`、`tenant_policy_revision` BIGINT DEFAULT 0、审计列）+
  `tenant_memory_engine_events`（BIGSERIAL、`(tenant_id, engine_id, created_at)` 索引、
  action CHECK ('initial','switch')）。expand-only，downgrade 仅 drop 两表。
- 模型 `bootstrap/db/models/memory_engine.py`（已并入 `models/__init__.py` 供
  alembic autogenerate 发现）。
- 仓储 `bootstrap/db/repository/memory_engine_repo.py`：get/list/create_initial_
  binding（幂等 + IntegrityError 回读收束竞态）/switch_binding（**原子**
  `UPDATE ... SET revision = revision + 1`，并发切换不丢失更新；单事务写 switch
  事件）/list_events。
- 服务 `bootstrap/memory_binding.py`：`MEMORY_ENGINE_CATALOG`（default=default_on
  初始、rachael=opt_in；**inspector 不入目录**）、`TenantMemoryEngineBindingService`
  （ensure/resolve_active_engine（缺绑定懒补齐）/resolve_revision/switch（校验
  目录→总开关→readiness，同 engine 幂等不提升）/describe/list_events/
  snapshot_active 进程内快照）+ `EngineIngestGate`（reader 后绑定，未命中/异常
  fail-open）。

## 测试证据（tests/memory_engines/test_binding_service.py，pytest.mark.postgres）

| 验收条目 | 测试 | 结果 |
| --- | --- | --- |
| 目录冻结 + inspector 不入目录（验收 1/8） | `test_catalog_is_frozen_server_side` | PASS |
| 初始绑定 default + ensure 幂等（验收 6） | `test_ensure_binding_initial_default_and_idempotent` | PASS |
| 单 active 约束 + 并发 ensure 单行（验收 6） | `test_single_active_constraint_and_concurrent_ensure` | PASS |
| 切换持久化 PG + revision+1 + 事件 + 重启生效（验收 3） | `test_switch_persists_and_bumps_revision` | PASS |
| 同 engine 幂等不提升（验收 2） | `test_switch_same_engine_is_idempotent` | PASS |
| 目录外/无 ready/未放行 三连拒绝且零副作用（验收 2） | `test_unauthorized_switch_rejected` | PASS |
| 切换不触引擎数据 + (tenant_id, engine_id) 可追踪（验收 5） | `test_switch_does_not_touch_engine_data_traceability` | PASS（含仓储公共 API 范围断言） |
| 并发切换单行 + revision==switch 事件数（验收 6） | `test_concurrent_switch_keeps_single_row` | PASS |
| ingest 快照缓存 | `test_snapshot_cache_for_ingest_gate` | PASS |
| GET 视图（ready/selectable/active） | `test_describe_view` | PASS |

命令：`NEXUS_REQUIRE_PG=1 NEXUS_TEST_PG_URL=postgresql://nexus:nexus_dev@localhost:5433/nexus uv run --no-sync pytest tests/memory_engines/ -q` → 23 passed。
