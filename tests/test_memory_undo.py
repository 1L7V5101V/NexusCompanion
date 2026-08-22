from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import psycopg
import pytest

from infra.storage.interfaces import TenantContext
from infra.storage.runtime import StorageRuntime
from memory2.store import MemoryStore2
from plugins.default_memory.engine import DefaultMemoryEngine
from tests.provision_util import provision_partition


def _item_id(result: str) -> str:
    return result.split(":", 1)[1]


def _result_list(result: dict[str, object], key: str) -> list[str]:
    value = result[key]
    assert isinstance(value, list)
    return [str(item) for item in value]


def _engine(store: MemoryStore2) -> DefaultMemoryEngine:
    engine = DefaultMemoryEngine.__new__(DefaultMemoryEngine)
    engine._v2_store = store
    engine._storage_runtime = None
    return engine


def _pg_engine(runtime: StorageRuntime) -> DefaultMemoryEngine:
    engine = DefaultMemoryEngine.__new__(DefaultMemoryEngine)
    engine._v2_store = None
    engine._storage_runtime = runtime
    return engine


def test_undo_marks_direct_source_memory_superseded(tmp_path: Path):
    store = MemoryStore2(tmp_path / "memory2.db")
    try:
        engine = _engine(store)
        item_id = _item_id(
            store.upsert_item(
                memory_type="preference",
                summary="用户喜欢简短回复",
                embedding=[0.1, 0.2],
                source_ref="cli:1:1",
            )
        )

        result = engine.undo_by_message_sources(["cli:1:1"])

        assert _result_list(result, "affected_ids") == [item_id]
        assert store.get_items_by_ids([item_id])[0]["status"] == "superseded"
    finally:
        store.close()


def test_undo_dry_run_does_not_change_memory_status(tmp_path: Path):
    store = MemoryStore2(tmp_path / "memory2.db")
    try:
        engine = _engine(store)
        item_id = _item_id(
            store.upsert_item(
                memory_type="preference",
                summary="用户喜欢简短回复",
                embedding=[0.1, 0.2],
                source_ref="cli:1:1",
            )
        )

        result = engine.undo_by_message_sources(["cli:1:1"], dry_run=True)

        assert _result_list(result, "affected_ids") == [item_id]
        assert store.get_items_by_ids([item_id])[0]["status"] == "active"
    finally:
        store.close()


def test_undo_marks_consolidation_window_memory_superseded(tmp_path: Path):
    store = MemoryStore2(tmp_path / "memory2.db")
    try:
        engine = _engine(store)
        base = json.dumps(["cli:1:0", "cli:1:1", "cli:1:2"], ensure_ascii=False)
        history_id = _item_id(
            store.upsert_item(
                memory_type="event",
                summary="用户完成了压缩前的一轮任务",
                embedding=[0.1, 0.2],
                source_ref=f"{base}#h:abc",
            )
        )
        profile_id = _item_id(
            store.upsert_item(
                memory_type="profile",
                summary="用户在测试 undo",
                embedding=[0.2, 0.3],
                source_ref=f"{base}#profile",
            )
        )

        result = engine.undo_by_message_sources(["cli:1:1"])

        assert set(_result_list(result, "affected_ids")) == {history_id, profile_id}
        assert _result_list(result, "rollback_source_ids") == ["cli:1:0", "cli:1:1", "cli:1:2"]
        rows = store.get_items_by_ids([history_id, profile_id])
        assert [row["status"] for row in rows] == ["superseded", "superseded"]
    finally:
        store.close()


def test_undo_restores_old_memory_replaced_by_affected_new_memory(tmp_path: Path):
    store = MemoryStore2(tmp_path / "memory2.db")
    try:
        engine = _engine(store)
        old_id = _item_id(
            store.upsert_item(
                memory_type="preference",
                summary="旧偏好",
                embedding=[0.1, 0.2],
                source_ref="cli:1:old",
            )
        )
        new_id = _item_id(
            store.upsert_item(
                memory_type="preference",
                summary="新偏好",
                embedding=[0.2, 0.3],
                source_ref="cli:1:4",
            )
        )
        old_item = store.get_items_by_ids([old_id])[0]
        new_item = store.get_items_by_ids([new_id])[0]
        store.mark_superseded_batch([old_id])
        store.record_replacements(old_items=[old_item], new_item=new_item, source_ref="cli:1:4")

        result = engine.undo_by_message_sources(["cli:1:4"])

        assert _result_list(result, "affected_ids") == [new_id]
        assert _result_list(result, "restored_ids") == [old_id]
        old_row, new_row = store.get_items_by_ids([old_id, new_id])
        assert old_row["status"] == "active"
        assert new_row["status"] == "superseded"
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 跨 tenant undo 隔离（PG-gated：本地无 PG 则 skip）
# ---------------------------------------------------------------------------

_PG_URL = os.environ.get(
    "NEXUS_TEST_PG_URL",
    "postgresql://nexus:nexus_dev@localhost:5433/nexus",
)


def _pg_alive(url: str) -> bool:
    try:
        conn = psycopg.connect(url, connect_timeout=2)
    except psycopg.Error:
        return False
    conn.close()
    return True


def _unique_tenant(prefix: str) -> str:
    return f"{prefix}_{os.getpid()}_{uuid.uuid4().hex[:6]}"


def _clean_pg(url: str, tenant: str) -> None:
    conn = psycopg.connect(url, autocommit=True)
    for table in ("consolidation_events", "memory_replacements", "memory_items"):
        conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant,))
    conn.execute("DELETE FROM messages WHERE tenant_id = %s", (tenant,))
    conn.execute("DELETE FROM sessions WHERE tenant_id = %s", (tenant,))
    conn.close()


@pytest.mark.postgres
def test_undo_isolated_by_tenant(tmp_path: Path) -> None:
    """tenant A 的 undo 只影响 A 的记忆，不影响 B 的（同 source_ref 也不串）。"""
    if not _pg_alive(_PG_URL):
        pytest.skip(f"本地 PG 不可用（{_PG_URL}），跳过 undo 越权用例")
    tenant_a = _unique_tenant("undo_a")
    tenant_b = _unique_tenant("undo_b")
    # 写路径已 fail-fast（M4H-4）：memory 写前先由测试前置建好分区。
    provision_partition(_PG_URL, tenant_a)
    provision_partition(_PG_URL, tenant_b)
    runtime = None
    try:
        runtime = StorageRuntime(_PG_URL, tmp_path / "m.db", tmp_path / "s.db")
        a = runtime.for_tenant(TenantContext(tenant_id=tenant_a))
        b = runtime.for_tenant(TenantContext(tenant_id=tenant_b))
        id_a = _item_id(
            a.memory.upsert_item(
                memory_type="preference",
                summary="A 的偏好",
                embedding=None,
                source_ref="cli:1:1",
            )
        )
        id_b = _item_id(
            b.memory.upsert_item(
                memory_type="preference",
                summary="B 的偏好",
                embedding=None,
                source_ref="cli:1:1",
            )
        )

        engine = _pg_engine(runtime)
        result = engine.undo_by_message_sources(["cli:1:1"], tenant_id=tenant_a)

        assert _result_list(result, "affected_ids") == [id_a]
        assert a.memory.get_items_by_ids([id_a])[0]["status"] == "superseded"
        assert b.memory.get_items_by_ids([id_b])[0]["status"] == "active"
    finally:
        if runtime is not None:
            runtime.close()
        _clean_pg(_PG_URL, tenant_a)
        _clean_pg(_PG_URL, tenant_b)
