"""M4H-4 commit 5c：dashboard/undo 直写入口 READY-only 验证。

ADR §1.2：dashboard/undo 等直写入口只允许 READY tenant，且不在请求内
provisioning。存储层写路径已 fail-fast（5a 覆盖 INSERT 三方法）；本文件
验证 dashboard/undo 的写方法是 UPDATE/DELETE（缺分区时 0 行 no-op、读空），
且绝不触发分区创建（无 request-time provisioning）。
postgres 用例依赖本地 PG，无 PG 自动 skip。
"""
import os
import uuid

import psycopg
import pytest

from infra.storage.partitioning import partition_name_for_tenant
from infra.storage.postgres_memory_store import PostgresMemoryStore
from tests.provision_util import provision_partition

DIM = 1024

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
        pytest.skip(f"本地 PG 不可用（{PG_URL}），跳过 dashboard/undo readiness 测试")


def _unique_tenant(prefix: str) -> str:
    return f"du_{prefix}_{os.getpid()}_{uuid.uuid4().hex[:6]}"


def _clean_pg(tenant: str) -> None:
    conn = psycopg.connect(PG_URL, autocommit=True)
    try:
        for table in ("consolidation_events", "memory_replacements", "memory_items"):
            conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant,))
    finally:
        conn.close()


def _open_store(tenant: str) -> PostgresMemoryStore:
    return PostgresMemoryStore(PG_URL, tenant_id=tenant, vec_dim=DIM)


def _partition_exists(tenant: str) -> bool:
    conn = psycopg.connect(PG_URL)
    try:
        row = conn.execute(
            "SELECT to_regclass(%s)", (partition_name_for_tenant(tenant),)
        ).fetchone()
        return bool(row and row[0] is not None)
    finally:
        conn.close()


def _assert_no_provisioning_triggered(store: PostgresMemoryStore, tenant: str) -> None:
    """直写后分区必须仍不存在且缓存未填充（无 request-time provisioning）。"""
    assert _partition_exists(tenant) is False
    assert partition_name_for_tenant(tenant) not in store._backend._partitions_known


@pytest.mark.postgres
def test_dashboard_update_noop_without_partition(pg_alive) -> None:
    tenant = _unique_tenant("upd")
    store = _open_store(tenant)
    try:
        result = store.update_item_for_dashboard(
            "mem-1", status="active", emotional_weight=5
        )
        assert result is None  # 缺分区 → 0 行更新，dashboard 映射 404
        _assert_no_provisioning_triggered(store, tenant)
    finally:
        store.close()
        _clean_pg(tenant)


@pytest.mark.postgres
def test_dashboard_delete_noop_without_partition(pg_alive) -> None:
    tenant = _unique_tenant("del")
    store = _open_store(tenant)
    try:
        assert store.delete_item("mem-1") is False
        assert store.delete_items_batch(["mem-1", "mem-2"]) == 0
        assert store.delete_by_source_ref("src:1") == 0
        _assert_no_provisioning_triggered(store, tenant)
    finally:
        store.close()
        _clean_pg(tenant)


@pytest.mark.postgres
def test_dashboard_reads_empty_without_partition(pg_alive) -> None:
    tenant = _unique_tenant("read")
    store = _open_store(tenant)
    try:
        items, total = store.list_items_for_dashboard()
        assert items == [] and total == 0
        assert store.get_item_for_dashboard("mem-1") is None
        _assert_no_provisioning_triggered(store, tenant)
    finally:
        store.close()
        _clean_pg(tenant)


@pytest.mark.postgres
def test_undo_noop_without_partition(pg_alive) -> None:
    tenant = _unique_tenant("undo")
    store = _open_store(tenant)
    try:
        result = store.undo_by_message_sources(["src:1"])
        assert result == {
            "affected_ids": [],
            "restored_ids": [],
            "rollback_source_ids": [],
        }
        _assert_no_provisioning_triggered(store, tenant)
    finally:
        store.close()
        _clean_pg(tenant)


@pytest.mark.postgres
def test_dashboard_writes_work_after_provision(pg_alive) -> None:
    tenant = _unique_tenant("prov")
    store = _open_store(tenant)
    try:
        provision_partition(PG_URL, tenant)
        raw_id = store.upsert_item("note", "建分区后的数据", None, source_ref="du:1")
        item_id = raw_id.split(":", 1)[1]
        updated = store.update_item_for_dashboard(item_id, status="active")
        assert updated is not None and updated["id"] == item_id
        assert store.delete_item(item_id) is True
    finally:
        store.close()
        _clean_pg(tenant)
