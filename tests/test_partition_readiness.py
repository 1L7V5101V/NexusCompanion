"""M4H-4 commit 5：写路径 fail-fast readiness 测试。

覆盖：未建分区时 upsert_item / upsert_consolidation_event / merge_item_raw
抛 ``PartitionNotReady``（绝不懒建 DDL）；建分区后写入成功且缓存填充；
只读查询不要求分区（读走父表，缺分区返回空而非报错）。
postgres 用例依赖本地 PG，无 PG 自动 skip。
"""
import os
import uuid

import psycopg
import pytest

from infra.storage.partitioning import PartitionNotReady, partition_name_for_tenant
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
        pytest.skip(f"本地 PG 不可用（{PG_URL}），跳过 partition readiness 测试")


def _unique_tenant(prefix: str) -> str:
    return f"ready_{prefix}_{os.getpid()}_{uuid.uuid4().hex[:6]}"


def _clean_pg(tenant: str) -> None:
    conn = psycopg.connect(PG_URL, autocommit=True)
    try:
        for table in ("consolidation_events", "memory_replacements", "memory_items"):
            conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant,))
    finally:
        conn.close()


def _open_store(tenant: str) -> PostgresMemoryStore:
    return PostgresMemoryStore(PG_URL, tenant_id=tenant, vec_dim=DIM)


@pytest.mark.postgres
def test_write_without_partition_fails_fast(pg_alive) -> None:
    tenant = _unique_tenant("basic")
    store = _open_store(tenant)
    try:
        with pytest.raises(PartitionNotReady, match=tenant):
            store.upsert_item(
                "note", "未建分区的写入", None, source_ref="ready:1"
            )
        # 测试前置建分区后写入成功
        provision_partition(PG_URL, tenant)
        assert store.upsert_item(
            "note", "已建分区的写入", None, source_ref="ready:1"
        ).startswith(("new:", "reinforced:"))
    finally:
        store.close()
        _clean_pg(tenant)


@pytest.mark.postgres
def test_all_write_paths_fail_fast(pg_alive) -> None:
    tenant = _unique_tenant("paths")
    store = _open_store(tenant)
    try:
        with pytest.raises(PartitionNotReady):
            store.upsert_consolidation_event(
                source_ref="r1", summary="Event A", embedding=[0.0] * DIM
            )
        with pytest.raises(PartitionNotReady):
            store.merge_item_raw(
                "id-x", "merged", "h-x", [0.0] * DIM, {"k": "v"}
            )
    finally:
        store.close()
        _clean_pg(tenant)


@pytest.mark.postgres
def test_reads_do_not_require_partition(pg_alive) -> None:
    tenant = _unique_tenant("reads")
    store = _open_store(tenant)
    try:
        # 读走父表：无匹配分区返回空而非报错（写才 fail-fast）
        assert store.vector_search([0.0] * DIM, top_k=5) == []
        assert store.get_all_with_embedding() == []
        assert store.list_by_type("note") == []
    finally:
        store.close()
        _clean_pg(tenant)


@pytest.mark.postgres
def test_probe_fills_cache_after_partition_created(pg_alive) -> None:
    tenant = _unique_tenant("cache")
    store = _open_store(tenant)
    try:
        name = provision_partition(PG_URL, tenant)
        assert name not in store._backend._partitions_known
        store.upsert_item("note", "写入触发探测", None, source_ref="ready:2")
        # 只读 to_regclass 探测确认后填充缓存，后续写不再探测
        assert name in store._backend._partitions_known
        assert partition_name_for_tenant(tenant) == name
    finally:
        store.close()
        _clean_pg(tenant)
