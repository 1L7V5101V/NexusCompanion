"""StorageRuntime / TenantStorage seam 测试（M4H-2 C）。

验证：
- for_tenant 廉价：PG 下不同 tenant 的 view 共享同一 backend（不新开连接）；
- view.close() 不关 backend，仅 runtime.close() 关；
- sqlite 显式 single-user：for_tenant(a).memory is for_tenant(b).memory；
- PG 下不同 tenant 的 view SQL tenant 作用域正确（真实 CRUD 隔离）。
postgres 用例依赖本地 PG，无 PG 自动 skip。
"""
import os

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
