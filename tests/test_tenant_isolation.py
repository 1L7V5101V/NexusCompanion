"""A/B tenant 隔离综合测试（PG-gated）。

覆盖 memory CRUD / vector_search、session store 与 SessionManager 复合键、
undo 与跨 tenant source_ref 的越权隔离。每测试用唯一 tenant
（iso_a_{pid} / iso_b_{pid}），teardown 清理。

HTTP 层 dashboard 越权用例见 test_dashboard_api.py
::test_dashboard_sessions_and_memories_isolated_by_tenant；undo 引擎层用例见
test_memory_undo.py::test_undo_isolated_by_tenant。
"""
from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest

from infra.storage.interfaces import TenantContext
from infra.storage.runtime import StorageRuntime
from memory2.store import VEC_DIM
from session.manager import SessionManager
from tests.provision_util import provision_partition

PG_URL = os.environ.get(
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


@pytest.fixture
def pg_url() -> str:
    if not _pg_alive(PG_URL):
        pytest.skip(f"本地 PG 不可用（{PG_URL}），跳过 tenant isolation")
    return PG_URL


def _unique_tenant(prefix: str) -> str:
    return f"iso_{prefix}_{os.getpid()}_{uuid.uuid4().hex[:6]}"


def _clean_pg(url: str, tenant: str) -> None:
    conn = psycopg.connect(url, autocommit=True)
    for table in ("consolidation_events", "memory_replacements", "memory_items"):
        conn.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant,))
    conn.execute("DELETE FROM messages WHERE tenant_id = %s", (tenant,))
    conn.execute("DELETE FROM sessions WHERE tenant_id = %s", (tenant,))
    conn.close()


@pytest.fixture
def iso(pg_url: str) -> Iterator[tuple[str, str]]:
    a = _unique_tenant("a")
    b = _unique_tenant("b")
    # 写路径已 fail-fast（M4H-4）：memory 写前先由测试前置建好分区。
    provision_partition(pg_url, a)
    provision_partition(pg_url, b)
    yield a, b
    _clean_pg(pg_url, a)
    _clean_pg(pg_url, b)


def _item_id(result: str) -> str:
    return result.split(":", 1)[1]


def _result_list(result: dict[str, object], key: str) -> list[str]:
    value = result[key]
    assert isinstance(value, list)
    return [str(item) for item in value]


def _emb(base: int, mag: float = 1.0) -> list[float]:
    v = [0.0] * VEC_DIM
    v[base] = mag
    return v


@pytest.mark.postgres
def test_memory_crud_and_vector_search_isolated(
    tmp_path: Path, pg_url: str, iso: tuple[str, str]
) -> None:
    """A 写入的记忆，B 的 vector_search / get / dashboard 列表都不可见。"""
    a, b = iso
    runtime = StorageRuntime(pg_url, tmp_path / "m.db", tmp_path / "s.db", vec_dim=VEC_DIM)
    try:
        va = runtime.for_tenant(TenantContext(tenant_id=a))
        vb = runtime.for_tenant(TenantContext(tenant_id=b))
        id_a1 = _item_id(
            va.memory.upsert_item(
                memory_type="preference", summary="A 喜欢奶茶", embedding=_emb(0), source_ref="iso:a:1"
            )
        )
        id_a2 = _item_id(
            va.memory.upsert_item(
                memory_type="event", summary="A 昨晚散步", embedding=_emb(2), source_ref="iso:a:2"
            )
        )
        id_b = _item_id(
            vb.memory.upsert_item(
                memory_type="preference", summary="B 喜欢咖啡", embedding=_emb(1), source_ref="iso:b:1"
            )
        )

        # 用 B 的向量在 A 的 view 查询：即使强相似也绝不可见（SQL 已按 tenant 作用域）
        hits_a = va.memory.vector_search(_emb(1), top_k=10)
        assert {str(h["id"]) for h in hits_a} & {id_b} == set()
        # A 查自己的向量能命中
        assert id_a1 in {str(h["id"]) for h in va.memory.vector_search(_emb(0), top_k=10)}

        # 用 A 的向量在 B 的 view 查询同样不可见；B 查自己能命中
        hits_b = vb.memory.vector_search(_emb(0), top_k=10)
        assert {str(h["id"]) for h in hits_b} & {id_a1, id_a2} == set()
        assert id_b in {str(h["id"]) for h in vb.memory.vector_search(_emb(1), top_k=10)}

        # get_items_by_ids 跨 tenant 返回空而非报错
        assert va.memory.get_items_by_ids([id_b]) == []
        assert vb.memory.get_items_by_ids([id_a1, id_a2]) == []

        # dashboard 列表只回本 tenant
        items_a, total_a = va.memory.list_items_for_dashboard()
        assert total_a == 2 and {str(i["id"]) for i in items_a} == {id_a1, id_a2}
        items_b, total_b = vb.memory.list_items_for_dashboard()
        assert total_b == 1 and {str(i["id"]) for i in items_b} == {id_b}
    finally:
        runtime.close()


@pytest.mark.postgres
def test_session_store_isolated_by_tenant(
    tmp_path: Path, pg_url: str, iso: tuple[str, str]
) -> None:
    """同 key 不同 tenant 的 session 不串；list_sessions 只回本 tenant。"""
    a, b = iso
    runtime = StorageRuntime(pg_url, tmp_path / "m.db", tmp_path / "s.db")
    try:
        va = runtime.for_tenant(TenantContext(tenant_id=a))
        vb = runtime.for_tenant(TenantContext(tenant_id=b))
        va.sessions.create_session(key="tg:100", metadata={"title": "A room"})
        vb.sessions.create_session(key="tg:100", metadata={"title": "B room"})

        assert va.sessions.get_session_meta("tg:100")["metadata"]["title"] == "A room"
        assert vb.sessions.get_session_meta("tg:100")["metadata"]["title"] == "B room"
        # 不存在的 key 返回 None（而非报错）
        assert va.sessions.get_session_meta("tg:999") is None

        items_a, total_a = va.sessions.list_sessions_for_dashboard()
        items_b, total_b = vb.sessions.list_sessions_for_dashboard()
        assert total_a == 1 and {s["key"] for s in items_a} == {"tg:100"}
        assert total_b == 1 and {s["key"] for s in items_b} == {"tg:100"}
    finally:
        runtime.close()


@pytest.mark.postgres
def test_session_manager_composite_key(
    tmp_path: Path, pg_url: str, iso: tuple[str, str]
) -> None:
    """SessionManager 以 (tenant_id, key) 复合键缓存，同 key 跨 tenant 不串。"""
    a, b = iso
    runtime = StorageRuntime(pg_url, tmp_path / "m.db", tmp_path / "s.db")
    try:
        mgr = SessionManager(tmp_path, storage_runtime=runtime)
        sa = mgr.get_or_create(a, "shared-key")
        sa.messages.append(
            {"role": "user", "content": "A 的消息", "timestamp": "2026-08-21T00:00:00+00:00"}
        )
        mgr.save(sa)

        # B 用同 key 得到全新会话，读不到 A 的消息
        sb = mgr.get_or_create(b, "shared-key")
        assert sb.messages == []
        # A 的会话仍带自己的消息（缓存与持久化都不串）
        assert [m["content"] for m in mgr.get_or_create(a, "shared-key").messages] == ["A 的消息"]
    finally:
        runtime.close()


@pytest.mark.postgres
def test_undo_and_cross_tenant_source_ref_isolated(
    tmp_path: Path, pg_url: str, iso: tuple[str, str]
) -> None:
    """A 的 undo 只影响 A；跨 tenant source_ref 检索返回空而非报错。"""
    a, b = iso
    runtime = StorageRuntime(pg_url, tmp_path / "m.db", tmp_path / "s.db")
    try:
        va = runtime.for_tenant(TenantContext(tenant_id=a))
        vb = runtime.for_tenant(TenantContext(tenant_id=b))
        # 两边都有 source_ref="iso:shared:1" 的记忆
        id_a = _item_id(
            va.memory.upsert_item(
                memory_type="preference", summary="A 的偏好", embedding=None, source_ref="iso:shared:1"
            )
        )
        id_b = _item_id(
            vb.memory.upsert_item(
                memory_type="preference", summary="B 的偏好", embedding=None, source_ref="iso:shared:1"
            )
        )
        # B 再写一条 A 没有的 source_ref
        vb.memory.upsert_item(
            memory_type="event", summary="B 专属事件", embedding=None, source_ref="iso:b:2"
        )

        res = va.memory.undo_by_message_sources(["iso:shared:1"])
        assert _result_list(res, "affected_ids") == [id_a]
        assert va.memory.get_items_by_ids([id_a])[0]["status"] == "superseded"
        # B 的同 source_ref 记忆不受 A 的 undo 影响
        assert vb.memory.get_items_by_ids([id_b])[0]["status"] == "active"

        # 跨 tenant source_ref 检索：A 查 B 专属的 source_ref 返回空
        _, total = va.memory.list_items_for_dashboard(source_ref="iso:b:2")
        assert total == 0
    finally:
        runtime.close()
