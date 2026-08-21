"""StorageRuntime / TenantStorage seam 测试（M4H-2 C）。

验证：
- for_tenant 廉价：PG 下不同 tenant 的 view 共享同一 backend（不新开连接）；
- view.close() 不关 backend，仅 runtime.close() 关；
- sqlite 显式 single-user：for_tenant(a).memory is for_tenant(b).memory；
- PG 下不同 tenant 的 view SQL tenant 作用域正确（真实 CRUD 隔离）。
postgres 用例依赖本地 PG，无 PG 自动 skip。
"""
import asyncio
import os
import threading
import time

import psycopg
import pytest

from infra.storage.interfaces import TenantContext
from infra.storage.runtime import StorageRuntime

PG_URL = os.environ.get(
    "NEXUS_TEST_PG_URL",
    "postgresql://nexus:nexus_dev@localhost:5433/nexus",
)


def _pg_alive() -> bool:
    try:
        conn = psycopg.connect(PG_URL, connect_timeout=2)
    except psycopg.Error:
        return False
    conn.close()
    return True


@pytest.fixture
def pg_alive() -> None:
    if not _pg_alive():
        pytest.skip(f"本地 PG 不可用（{PG_URL}），跳过 postgres runtime 测试")


def _tenant(tag: str) -> TenantContext:
    return TenantContext(tenant_id=f"runtime_{tag}_{os.getpid()}")


def _make_runtime(tmp_path) -> StorageRuntime:
    return StorageRuntime(None, tmp_path / "memory.db", tmp_path / "sessions.db")


# ── sqlite：显式 single-user ─────────────────────────────────────────────


def test_sqlite_for_tenant_returns_shared_stores(tmp_path) -> None:
    runtime = _make_runtime(tmp_path)
    try:
        a = runtime.for_tenant(_tenant("a"))
        b = runtime.for_tenant(_tenant("b"))
        assert a.memory is b.memory
        assert a.sessions is b.sessions
        assert a.tenant.tenant_id != b.tenant.tenant_id
    finally:
        runtime.close()


def test_sqlite_tenant_storage_has_no_close(tmp_path) -> None:
    runtime = _make_runtime(tmp_path)
    try:
        view = runtime.for_tenant(_tenant("a"))
        assert not hasattr(view, "close")
    finally:
        runtime.close()


def test_sqlite_runtime_close_then_for_tenant_raises(tmp_path) -> None:
    runtime = _make_runtime(tmp_path)
    runtime.close()
    with pytest.raises(RuntimeError, match="closed"):
        runtime.for_tenant(_tenant("a"))


def test_sqlite_runtime_close_is_idempotent(tmp_path) -> None:
    runtime = _make_runtime(tmp_path)
    runtime.close()
    runtime.close()


# ── postgres：资源所有权与显式 shutdown（M4H-3）────────────────────────────


@pytest.mark.postgres
def test_pg_runtime_owns_pool_and_bounded_executor(pg_alive, tmp_path) -> None:
    runtime = StorageRuntime(PG_URL, "unused.db", "unused.db", pool_size=4)
    try:
        # bounded executor：线程数取 pool_size（ADR §1.3 预算）
        assert runtime._executor is not None
        assert runtime._executor._max_workers == 4
        # backend 的 pool 容量 = StorageConfig.pool_size，配置真实生效
        assert runtime._memory_backend._pool.size == 4
        assert runtime._session_backend._pool.size == 4
    finally:
        runtime.close()


def test_sqlite_runtime_has_no_executor(tmp_path) -> None:
    runtime = _make_runtime(tmp_path)
    try:
        assert runtime._executor is None
    finally:
        runtime.close()


@pytest.mark.postgres
def test_pg_runtime_close_shuts_executor_then_pool(pg_alive, tmp_path) -> None:
    runtime = StorageRuntime(PG_URL, "unused.db", "unused.db")
    try:
        view = runtime.for_tenant(_tenant("a"))
    finally:
        runtime.close()
    # executor 已 shutdown：拒绝新提交
    with pytest.raises(RuntimeError):
        runtime._executor.submit(lambda: None)
    # pool 已关闭：borrow 拒绝
    assert view.memory._backend._closed
    assert view.sessions._backend._closed


@pytest.mark.postgres
def test_pg_runtime_close_is_idempotent_with_executor(pg_alive, tmp_path) -> None:
    runtime = StorageRuntime(PG_URL, "unused.db", "unused.db")
    runtime.close()
    runtime.close()


# ── postgres：tenant-bound view 共享 backend ──────────────────────────────


@pytest.mark.postgres
def test_pg_for_tenant_views_share_backend(pg_alive, tmp_path) -> None:
    runtime = StorageRuntime(PG_URL, "unused.db", "unused.db")
    try:
        a = runtime.for_tenant(_tenant("a"))
        b = runtime.for_tenant(_tenant("b"))
        # 独立 view（轻量对象），但共享同一 backend 连接/锁，不新开连接
        assert a.memory is not b.memory
        assert a.memory._backend is b.memory._backend
        assert a.sessions._backend is b.sessions._backend
    finally:
        runtime.close()


@pytest.mark.postgres
def test_pg_view_close_is_noop(pg_alive, tmp_path) -> None:
    runtime = StorageRuntime(PG_URL, "unused.db", "unused.db")
    try:
        a = runtime.for_tenant(_tenant("a"))
        a.memory.close()
        a.sessions.close()
        assert not a.memory._backend._closed
        assert not a.sessions._backend._closed
    finally:
        runtime.close()


@pytest.mark.postgres
def test_pg_runtime_close_closes_backends(pg_alive, tmp_path) -> None:
    runtime = StorageRuntime(PG_URL, "unused.db", "unused.db")
    try:
        a = runtime.for_tenant(_tenant("a"))
        assert not a.memory._backend._closed
        assert not a.sessions._backend._closed
    finally:
        runtime.close()
    assert a.memory._backend._closed
    assert a.sessions._backend._closed


@pytest.mark.postgres
def test_pg_tenant_sql_scoping(pg_alive, tmp_path) -> None:
    runtime = StorageRuntime(PG_URL, "unused.db", "unused.db")
    try:
        suffix = os.getpid()
        store_a = runtime.for_tenant(_tenant("a")).memory
        store_b = runtime.for_tenant(_tenant("b")).memory

        store_a.upsert_item(
            "event", f"scoped-a-{suffix}", None, source_ref=f"ref-a-{suffix}"
        )
        store_b.upsert_item(
            "event", f"scoped-b-{suffix}", None, source_ref=f"ref-b-{suffix}"
        )

        # 跨 tenant 的 source_ref 检索返回空（互不可见），而非报错
        assert store_a.has_item_by_source_ref(f"ref-b-{suffix}") is False
        assert store_b.has_item_by_source_ref(f"ref-a-{suffix}") is False
        # 各自 tenant 内的 source_ref 可见
        assert store_a.has_item_by_source_ref(f"ref-a-{suffix}") is True
        assert store_b.has_item_by_source_ref(f"ref-b-{suffix}") is True
    finally:
        runtime.close()


# ── run_db：同步 DB 调用移出 event loop（M4H-3 commit 4）────────────────────


@pytest.mark.postgres
async def test_pg_run_db_runs_db_in_executor(pg_alive, tmp_path) -> None:
    runtime = StorageRuntime(PG_URL, "unused.db", "unused.db", pool_size=2)
    loop_thread = threading.get_ident()
    try:
        store = runtime.for_tenant(_tenant("run_db")).memory

        def _upsert_sync() -> str:
            # executor 线程 ≠ event loop 线程：证明调用确实离开 event loop。
            assert threading.get_ident() != loop_thread
            return store.upsert_item(
                "event", "run-db-executor", None, source_ref="ref-run-db"
            )

        item_id = await runtime.run_db(_upsert_sync)
        assert item_id.startswith(("new:", "reinforced:"))
        # 写结果可跨线程读回（pool 连接由 view 的 thread-local 管理）。
        assert store.has_item_by_source_ref("ref-run-db") is True
    finally:
        runtime.close()


@pytest.mark.postgres
async def test_pg_run_db_after_close_raises(pg_alive, tmp_path) -> None:
    runtime = StorageRuntime(PG_URL, "unused.db", "unused.db")
    runtime.close()
    with pytest.raises(RuntimeError):
        await runtime.run_db(lambda: 1)


async def test_sqlite_run_db_runs_inline(tmp_path) -> None:
    runtime = _make_runtime(tmp_path)
    try:
        loop_thread = threading.get_ident()

        def _probe() -> str:
            # sqlite 无 executor：run_db 直接同步执行，线程保持 event loop 线程。
            return f"thread-{threading.get_ident()}"

        assert await runtime.run_db(_probe) == f"thread-{loop_thread}"
    finally:
        runtime.close()


# ── event-loop lag probe（M4H-3 commit 7）───────────────────────────────────


@pytest.mark.postgres
async def test_event_loop_lag_probe(pg_alive, tmp_path) -> None:
    """可重复 probe：run_db 隔离下 DB 调用不阻塞 event loop。

    同一条 ``pg_sleep(0.2)`` DB 调用两种执行方式对照：
    - 控制组：直接在 loop 线程同步调用（反模式），heartbeat 漏拍 ~200ms；
    - 隔离组：经 ``run_db`` 走 executor，heartbeat 保持 ~10ms 周期。

    probe 直接借出 pool 连接执行 pg_sleep，只为制造确定时长的 DB 调用；
    生产路径的隔离已由 test_pg_run_db_runs_db_in_executor 覆盖。
    """
    runtime = StorageRuntime(PG_URL, "unused.db", "unused.db", pool_size=2)
    try:
        interval = 0.01
        gap_history: list[float] = []

        def _slow_db() -> None:
            with runtime._memory_backend._pool.connection() as conn:
                conn.execute("SELECT pg_sleep(0.2)")

        async def measure(blocking: bool) -> float:
            """窗口内最大心跳间隔：blocking=True 时直接同步调 DB（阻塞 loop）。"""
            gap_history.clear()
            stop = asyncio.Event()

            async def heartbeat() -> None:
                prev = time.monotonic()
                while not stop.is_set():
                    await asyncio.sleep(interval)
                    now = time.monotonic()
                    gap_history.append(now - prev)
                    prev = now

            hb = asyncio.ensure_future(heartbeat())
            await asyncio.sleep(0.03)
            if blocking:
                _slow_db()
            else:
                await runtime.run_db(_slow_db)
            await asyncio.sleep(0.03)
            stop.set()
            await hb
            return max(gap_history)

        blocked_max = await measure(blocking=True)
        isolated_max = await measure(blocking=False)
        # 控制组确实阻塞过（pg_sleep 生效），对照才有意义
        assert blocked_max >= 0.15
        # 隔离组心跳保持 ~10ms 量级；若 DB 调用进了 loop 会漏拍至 ~200ms
        assert isolated_max < 0.08
    finally:
        runtime.close()
