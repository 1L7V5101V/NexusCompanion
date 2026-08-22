# M4 存储工厂与接线（Phase 1 Storage）

> 来源：[phase1-storage.md](phase1-storage.md) M4
> 分支：`feature/scaling-phase1-storage`
> 日期：2026-08-20

## 目标

Phase 1 已为 `MemoryStore2` / `SessionStore` 增加 PostgreSQL 后端（`PostgresMemoryStore` / `PostgresSessionStore`，M2/M3 完成并验证）。M4 是**接线**：建立工厂，让 `config.storage.backend` 决定返回哪个 store，并把全部生产构造点改走工厂。

- `backend="sqlite"` 时行为与现状完全一致（零回归）
- `backend="postgres"` 时全链路使用 PG 存储
- 测试直连构造（`MemoryStore2(path, vec_dim=...)`、`SessionStore(path)`、`SessionManager(workspace)`）保持可用

## 工厂 API（`infra/storage/factory.py`，新建）

```python
def create_store(
    config: StorageConfig, sqlite_path: str | Path, *,
    tenant_id: str = "default", vec_dim: int = VEC_DIM,
) -> MemoryStore2 | PostgresMemoryStore:
    # sqlite → MemoryStore2(sqlite_path, vec_dim=vec_dim)
    # postgres → 先探测 schema，再 PostgresMemoryStore(config.postgres_url, tenant_id=tenant_id, vec_dim=vec_dim)
    # else → ValueError(f"Unknown backend: {config.backend}")

def create_session_store(
    config: StorageConfig, sqlite_path: str | Path, *,
    tenant_id: str = "default",
) -> SessionStore | PostgresSessionStore:
    # sqlite → SessionStore(sqlite_path)
    # postgres → 先探测 schema，再 PostgresSessionStore(config.postgres_url, tenant_id=tenant_id)
```

说明：`StorageConfig`（`agent/config_models.py`）只有 `backend` / `postgres_url` / `pool_size`，**无 `sqlite_path` 字段**——sqlite 路径由调用方（workspace 相关）显式传入。PG store 自剥离 `postgresql+psycopg://` 前缀。

### schema 前置探测（postgres 分支）

构造 store 前开短命探测连接，`SELECT to_regclass(%s)` 查 `memory_items`（session 侧查 `sessions`）；`None` 则抛：

```
RuntimeError("PostgreSQL schema 未初始化（缺少表 {table}），请先执行：alembic upgrade head")
```

理由：PG store 构造不建表（schema 由 alembic 管），且 `init_workspace` 在 postgres 后端下跳过建库——不做前置检查的话，忘跑 migration 的失败点会推迟到首次真实查询（`relation "sessions" does not exist`），难定位。构造期一次性开销，可接受。

## 接线改动（按文件）

| 文件 | 改动 |
|------|------|
| `infra/storage/factory.py` | 新建，如上 |
| `plugins/default_memory/engine.py` | :575 换 `create_store(self._config.storage, db_path, vec_dim=...)`；`_v2_store` 注解与 `_require_v2_store` 放宽为 `MemoryStore2 \| PostgresMemoryStore`；`ensure_workspace_storage` 加 `config` kwarg，postgres 后端跳过空建；删模块级 `_source_ref_message_ids` / `_undo_store_by_message_sources` / `_restore_replacements_for_undo`，`undo_by_message_sources` 改薄包装委托 store 方法 |
| `plugins/default_memory/memory_plugin.py` | `ensure_workspace_storage` 补传 `config=config` |
| `memory2/memorizer.py` | `merge_item` 改 `get_item_for_dashboard`（`_db` 私有耦合点），删 `import json as _json` |
| `session/manager.py` | `SessionManager.__init__` 加可选 `session_store: SessionStore \| None = None`，直连构造兼容 |
| `bootstrap/tools.py` | `SessionStore(workspace / "sessions.db")` fallback 与 `SessionManager(workspace)` 均换工厂，单一 store 实例流向下游 |
| `bootstrap/dashboard_api.py` | `config: Config \| None = None` 经 `run_dashboard_api` / `build_dashboard_server` / `_build_dashboard_uvicorn_config` 进 `create_dashboard_app`，sessions.db 换工厂（config 为 None 时回退直连）；`app.py` 补传 `config=self.config` |
| `bootstrap/init_workspace.py` | sessions.db 建库块包 `if config.storage.backend != "postgres":` |
| `docker/debug/runtime_race_probe.py` | `SessionManager(self.workspace)` 上方加注释（探针仅支持 sqlite） |
| `proactive_v2/presence.py` | `self._store.db_path` 改 `getattr(self._store, "db_path", "")`（PG 无此属性） |

### 不在本 Phase 范围（三处，仅加注释）

1. **proactive 日志库**：`bootstrap/dashboard_api.py` `ProactiveDashboardReader` 直连 `sqlite3` 读 passive/proactive/drift 三库，非 sessions.db——`storage.backend` 不影响此处。
2. **markdown 旧记忆系统**：`dashboard_api.py` 的 `memory_store` 参数与 `MemoryStore(workspace)`、`init_workspace.py:150` `MemoryStore(workspace)`、`core/memory/markdown.py`——与 `storage.backend` 无关，保持不动。dashboard 记忆读走 `memory_runtime.engine`（memory2 后端），engine 换工厂后 dashboard 记忆读取**自动**变 PG 后端。
3. **`runtime_race_probe`**：debug 探针必须无需 PG 即可运行，race 语义以 SQLite 建模。

## undo 语义收口

`undo_by_message_sources` 从 engine.py 模块级函数收进两个 store（`memory2/store.py` 与 `infra/storage/postgres_memory_store.py`）为公共方法，`_source_ref_message_ids` 提为 `memory2/store.py` 模块级 helper，PG store 从那里 import。**不复用** `mark_superseded_batch`（内部 commit 破坏 undo 单事务原子性）；单 `with self._lock:` + 末尾一次 commit。`dry_run=True` 只预览不改写。双后端语义经 `tmp/verify_undo.py` 验证一致（id 因 `time.time()` 生成不同，只比结构）。

## 测试

### parity 已入 tests/（后续存储层改动的硬约束）

M2/M3 的 `tmp/parity_smoke.py` / `parity_session_smoke.py` 已迁入 **`tests/test_storage_parity.py`**（`test_memory_parity` / `test_session_parity`，`@pytest.mark.postgres`，无 PG 自动 skip，PG URL 可经 `NEXUS_TEST_PG_URL` 覆盖，确定性 embedding，每测试独立 tenant）。断言口径保留：向量 top-k 只比较集合与明确项的 score，不比较 `score=0` 精确平局的顺序（HNSW 与 KNN 截断顺序可不同）。

**M4 之后任何存储层改动必须保持 `tests/test_storage_parity.py` 绿色。**

### 工厂测试（`tests/test_storage_factory.py`）

- sqlite / postgres 各 store 类型断言、未知 backend `ValueError`
- **schema 探测负向测试**：建 scratch 空库（`CREATE DATABASE`）→ `create_store` 指向它 → 断言抛含 `alembic upgrade head` 的 `RuntimeError`

### 回归集

`test_memory_undo` / `test_dashboard_api` / `test_presence` / `test_message_lookup_tool` / `test_spawn_completion_flow` / `test_logic_modules` / `test_channel_base` / `test_storage_factory` / `test_storage_parity`。

> 已知与本 Phase 无关的既有失败（M4 改动前即在干净分支上复现）：`test_spawn_completion_flow` 2 项（`MemoryServices(engine=...)` 构造器与 `ports.py` 的 `engines` 字段漂移）、`test_dashboard_api` 插件面板 2 项（TS 面板构建产物缺失）。

## 验证记录（M4 完成）

- 每完成一步接线即跑 `tests/test_storage_parity.py`，全程绿色
- 回归集（除上述既有失败外）全绿：`test_memory_undo` / `test_presence` / `test_message_lookup_tool` / `test_logic_modules` / `test_channel_base` / `test_storage_factory` / `test_storage_parity` 共 58 passed
- 改动文件 `py_compile` 通过
