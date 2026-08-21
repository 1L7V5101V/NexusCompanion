"""storage interface seam 测试：adapter 结构满足 Protocol，tenant seam 可定义。

PG adapter 的结构性符合由 pyright（factory 返回注解）与契约矩阵（M4H-1 slice 4）
覆盖；本文件只做无 PG 依赖的 runtime 结构检查。
"""

import pytest

from infra.storage.interfaces import (
    MemoryStorage,
    SessionStorage,
    TenantContext,
    TenantResolver,
)
from memory2.store import MemoryStore2
from session.store import SessionStore


def test_memory_sqlite_adapter_satisfies_protocol(tmp_path: object) -> None:
    store = MemoryStore2(tmp_path / "mem.db", vec_dim=8)
    try:
        assert isinstance(store, MemoryStorage)
    finally:
        store.close()


def test_session_sqlite_adapter_satisfies_protocol(tmp_path: object) -> None:
    store = SessionStore(tmp_path / "sessions.db")
    try:
        assert isinstance(store, SessionStorage)
    finally:
        store.close()


def test_tenant_context_frozen_and_holds_id() -> None:
    ctx = TenantContext(tenant_id="tenant-a")
    assert ctx.tenant_id == "tenant-a"
    with pytest.raises(Exception):
        ctx.tenant_id = "other"  # type: ignore[misc]


class _FakeResolver:
    def resolve(self, identity: object) -> TenantContext:
        return TenantContext(tenant_id="tenant-a")


def test_tenant_resolver_seam_structural() -> None:
    resolver = _FakeResolver()
    assert isinstance(resolver, TenantResolver)
    assert resolver.resolve(object()).tenant_id == "tenant-a"
