"""租户 → 画像存储入口的解析器（c18-tenant-scoped-memory ADR-3 / ADR-4）。

装配根有四个（optimizer、dashboard、proactive、maintenance），它们都要读写某个租户的画像。
原来它们手里拿的是进程级共享的文件 store，这就是跨租户泄漏的入口本身。ADR-3 定的替代方案
是「持有解析器，处理哪个租户的数据时才解析」，不用 contextvar——contextvar 会让「这一轮到底
属于谁」变成隐式全局状态，而这正是本次要消掉的那类东西。

后端按配置选：PG 每租户一个绑定的 store（构造零 I/O）；SQLite 只有固定租户，实例按租户缓存
（它自己开连接，重复构造会重复开文件）。`close()` 交给装配根，解析器负责回收自己开过的连接。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from core.memory.profile_store import MemoryProfileStoreApi, ProfileStoreResolver

__all__ = ["ProfileAccess", "ProfileStoreResolver", "build_profile_access"]


@dataclass
class ProfileAccess:
    """一个可调用 + 一个 close 的组合。

    做成 dataclass 而不是返回裸 lambda，是因为 SQLite 那条路真的会开文件：装配根必须有能力
    在 shutdown 时把这些连接关掉，否则每轮泄漏一个 fd。
    """

    resolve: ProfileStoreResolver
    close: Callable[[], None] = field(default=lambda: None)


def build_profile_access(
    config: Any,
    *,
    memory_db_path: Path | None = None,
    session_factory: Any | None = None,
) -> ProfileAccess:
    """按 `config.storage` 建画像存储解析器。

    `session_factory` 给了就直接用（PG durable 装配点已有现成的），没给才按 `postgres_url`
    自己建 engine——两条路都是 PG 的正规入口，但绝不能两边各建一套连接池，所以优先用传入的。
    """
    backend = str(getattr(config.storage, "backend", "sqlite") or "sqlite")

    if backend == "postgres":
        from agent.memory_pg import PgMemoryProfileStore

        factory = session_factory
        owned_engine: Any | None = None
        if factory is None:
            from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
            from sqlalchemy.pool import NullPool

            from bootstrap.work_queue import async_pg_url

            url = async_pg_url(str(getattr(config.storage, "postgres_url", "") or ""))
            if not url:
                raise ValueError(
                    "storage.backend=postgres 但 postgres_url 为空，无法解析画像存储"
                )
            owned_engine = create_async_engine(url, poolclass=NullPool)
            factory = async_sessionmaker(owned_engine, expire_on_commit=False)

        def _resolve_pg(tenant_id: str) -> MemoryProfileStoreApi:
            _require_tenant(tenant_id)
            return PgMemoryProfileStore(factory, tenant_id)

        def _close_pg() -> None:
            if owned_engine is None:
                return
            owned_engine.sync_engine.dispose()

        return ProfileAccess(resolve=_resolve_pg, close=_close_pg)

    from core.memory.profile_sqlite import SqliteMemoryProfileStore
    from infra.storage.tenancy import DEFAULT_TENANT

    if memory_db_path is None:
        raise ValueError("SQLite 后端建画像存储需要 memory_db_path")
    cache: dict[str, Any] = {}

    def _resolve_sqlite(tenant_id: str) -> MemoryProfileStoreApi:
        _require_tenant(tenant_id)
        if tenant_id != DEFAULT_TENANT:
            # 单机运行时只读 DEFAULT_TENANT。这里不静默改写也不新建空桶：给出这个 id 说明
            # 上游租户归属接错了，让它响亮地失败比悄悄建一个没人读的行好。
            raise ValueError(
                f"SQLite 后端只有固定租户 {DEFAULT_TENANT!r}，收到 {tenant_id!r}"
            )
        store = cache.get(tenant_id)
        if store is None:
            store = SqliteMemoryProfileStore(memory_db_path, tenant_id)
            cache[tenant_id] = store
        return store

    def _close_sqlite() -> None:
        for store in cache.values():
            store.close()
        cache.clear()

    return ProfileAccess(resolve=_resolve_sqlite, close=_close_sqlite)


def _require_tenant(tenant_id: str) -> None:
    """空归属一律拒绝（task 4.3 的 fail-closed 判据）。"""
    if not str(tenant_id or "").strip():
        raise ValueError("缺少 tenant_id，拒绝解析画像存储")
