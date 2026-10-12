"""C18 画像存储的 SQLite 实现（c18-tenant-scoped-memory ADR-10 / ADR-14）。

与 PG 侧 `PgMemoryProfileStore` 同接口、同语义、同方法名：六个方法都来自
`core/memory/profile_store.py` 的 `MemoryProfileStoreApi`，SQL 正文也取自那里，本文件
不出现第二条表正文。单机后端没有第二个租户，但跨租户隔离断言因此在 SQLite 上也能跑，
不再只有 PG 一条路径验过（ADR-10）。

两张表由 `memory2/store.py` 的 `SCHEMA` 建，结构与 PG 侧 alembic `b7d3f5a9c1e4` 同形
（一致性由 `tests/memory/test_profile_schema_parity.py` 比对），本类不建表。

连接是自己开的，不复用 `MemoryStore2` 那条：后者以 `check_same_thread=False` 配一把
自己的 RLock，而本类的方法经 `asyncio.to_thread` 落在工作线程上，共用一条连接就等于让
两个线程绕开同一把锁。两条连接各自加锁，写入的串行化交给 SQLite 的文件锁，
`busy_timeout` 让撞锁时先等而不是立刻抛 `database is locked`。
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.memory.profile_store import (
    ENTRY_APPEND_SQL,
    ENTRY_ASSEMBLY_READ_SQL,
    ENTRY_CATEGORY_READ_SQL,
    PENDING_TAKE_SQL,
    SECTION_READ_SQL,
    SECTION_UPSERT_SQL,
    ProfileAssembly,
    build_assembly,
    pick_section,
    render_entry_rows,
    render_taken_rows,
    require_entry,
    require_section,
)
from infra.storage.tenancy import DEFAULT_TENANT

__all__ = ["SqliteMemoryProfileStore"]

_BUSY_TIMEOUT_MS = 5000


def _iso(value: datetime) -> str:
    """定长 UTC ISO 串。

    `timespec="microseconds"` 不是排版：`isoformat()` 默认在微秒为 0 时把小数位整个省掉，
    省掉的那行与没省的那行排在一起时顺序仍然正确，但长度不齐的串让「按 happened_at 排序」
    这件事需要读者自己论证。补齐之后列内格式唯一，字典序即时间序。

    naive 输入按 UTC 解读。真给出 naive 的是调用方的 bug——PG 侧同一时刻会按会话时区解释，
    两侧从此分叉，所以日期必须由上游带上时区。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _now_iso() -> str:
    return _iso(datetime.now(timezone.utc))


class SqliteMemoryProfileStore:
    """画像专表的 SQLite 实现。

    底层是同步 `sqlite3`，照仓库既有先例（`core/memory/profile_writer.py` 的
    `ProfileFileMaintainer` 包文件 store 的同步方法）用 `asyncio.to_thread` 包成 async，
    不为两种后端在协议层分叉出同步与异步两套签名。
    """

    def __init__(self, db_path: str | Path, tenant_id: str = DEFAULT_TENANT) -> None:
        self.tenant_id = tenant_id
        self._db = sqlite3.connect(str(db_path), check_same_thread=False)
        self._db.execute(f"PRAGMA busy_timeout = {_BUSY_TIMEOUT_MS}")
        self._lock = threading.RLock()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ------------------------------------------------------------------
    # 同步正文：写操作要么落在调用方的连接上（不提交），要么在自己那条上跑完并提交
    # ------------------------------------------------------------------

    @contextmanager
    def _cursor(self, session: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
        """给了连接就用它且不提交；没给就用自己的连接，正常退出时提交。

        锁总是拿：单机常见形状是调用方把本实例那条连接交回给 `take_pending` /
        `read_assembly`，此时不锁就是两个线程在同一条连接上交错执行。

        自己这条路径要显式回滚：`sqlite3` 的隐式事务在语句失败后仍然开着，不关掉就会把之后
        的写入并进这次失败的事务，而 PG 侧的 `async with session` 退出时自动丢弃。
        两侧「失败即无痕」得靠同一个机制，不能一侧靠框架、一侧靠运气。
        """
        with self._lock:
            if session is not None:
                yield session
                return
            try:
                yield self._db
                self._db.commit()
            except BaseException:
                self._db.rollback()
                raise

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """给同后端调用方的一条事务：产出本实例的连接，正常退出时提交、异常时回滚。

        为什么这个入口是**同步**的：本类的 async 方法每次都经 `asyncio.to_thread` 落到
        工作线程，而 `threading.RLock` 只对属主线程可重入——若用 async 上下文管理器把锁持在
        一次 `await` 上，事务里的第二次写入就会在另一个线程上等同一把锁，直接锁死。所以
        「一个事务内的多条写入」必须整段待在同一个线程里：调用方用 `asyncio.to_thread` 把
        整个同步函数体交进来，里面 `with store.transaction() as db:` 连写几条。

        只给同后端用。PG 侧不需要它——PG 的 `async_sessionmaker` 本来就是借用入口。
        """
        with self._cursor(None) as db:
            yield db

    def write_section_sync(
        self, db: sqlite3.Connection, category: str, content: str
    ) -> None:
        """在调用方事务内的同步覆写，与 async `write_section` 共用同一条 SQL 正文。"""
        require_section(category)
        now = _now_iso()
        db.execute(
            SECTION_UPSERT_SQL,
            {
                "id": str(uuid.uuid4()),
                "tenant_id": self.tenant_id,
                "category": category,
                "content": content,
                "created_at": now,
                "updated_at": now,
            },
        )

    def read_section_sync(self, db: sqlite3.Connection, category: str) -> str:
        """在调用方事务内的同步读取。"""
        require_section(category)
        rows = db.execute(SECTION_READ_SQL, {"tenant_id": self.tenant_id}).fetchall()
        return pick_section(rows, category)

    def _read_section_sync(self, category: str) -> str:
        with self._lock:
            rows = self._db.execute(
                SECTION_READ_SQL, {"tenant_id": self.tenant_id}
            ).fetchall()
        return pick_section(rows, category)

    def _write_section_sync(
        self, category: str, content: str, session: sqlite3.Connection | None
    ) -> None:
        with self._cursor(session) as db:
            self.write_section_sync(db, category, content)

    def _append_entry_sync(
        self,
        category: str,
        content: str,
        happened_at: str,
        dedup_key: str | None,
        session: sqlite3.Connection | None,
    ) -> bool:
        with self._cursor(session) as db:
            cursor = db.execute(
                ENTRY_APPEND_SQL,
                {
                    "id": str(uuid.uuid4()),
                    "tenant_id": self.tenant_id,
                    "category": category,
                    "content": content,
                    "happened_at": happened_at,
                    "dedup_key": dedup_key,
                    "created_at": _now_iso(),
                },
            )
        # 与 PG 侧同一个判据：ON CONFLICT DO NOTHING 命中时 rowcount 为 0。实测过这条
        # 行为（Python 3.13 / SQLite 3.53），不是照抄文档。
        return cursor.rowcount > 0

    def _read_entries_sync(self, category: str, max_chars: int) -> str:
        with self._lock:
            rows = self._db.execute(
                ENTRY_CATEGORY_READ_SQL,
                {"tenant_id": self.tenant_id, "category": category},
            ).fetchall()
        return render_entry_rows([str(row[0]) for row in rows], max_chars=max_chars)

    def _take_pending_sync(self, session: sqlite3.Connection) -> str:
        # 实测确认 `DELETE ... RETURNING` 在 SQLite 侧不保证行序，所以 `render_taken_rows`
        # 的排序在两种后端上都是承重的，不是冗余。
        with self._cursor(session) as db:
            rows = db.execute(
                PENDING_TAKE_SQL, {"tenant_id": self.tenant_id}
            ).fetchall()
        return render_taken_rows(rows)

    def read_assembly_sync(
        self, db: sqlite3.Connection, history_max_chars: int = 0
    ) -> ProfileAssembly:
        """在调用方连接上跑两条 SELECT，供同一个同步事务里组合多次读取。"""
        sections = db.execute(SECTION_READ_SQL, {"tenant_id": self.tenant_id}).fetchall()
        entries = db.execute(
            ENTRY_ASSEMBLY_READ_SQL, {"tenant_id": self.tenant_id}
        ).fetchall()
        return build_assembly(
            self.tenant_id, sections, entries, history_max_chars=history_max_chars
        )

    def _read_assembly_sync(
        self, session: sqlite3.Connection, history_max_chars: int
    ) -> ProfileAssembly:
        with self._cursor(session) as db:
            return self.read_assembly_sync(db, history_max_chars)

    # ------------------------------------------------------------------
    # MemoryProfileStoreApi
    # ------------------------------------------------------------------

    async def read_section(self, category: str) -> str:
        require_section(category)
        return await asyncio.to_thread(self._read_section_sync, category)

    async def write_section(
        self, category: str, content: str, *, session: Any | None = None
    ) -> None:
        require_section(category)
        await asyncio.to_thread(self._write_section_sync, category, content, session)

    async def append_entry(
        self,
        category: str,
        content: str,
        *,
        happened_at: datetime,
        dedup_key: str | None = None,
        session: Any | None = None,
    ) -> bool:
        require_entry(category)
        return await asyncio.to_thread(
            self._append_entry_sync,
            category,
            content,
            _iso(happened_at),
            dedup_key,
            session,
        )

    async def read_entries(self, category: str, *, max_chars: int = 0) -> str:
        require_entry(category)
        return await asyncio.to_thread(self._read_entries_sync, category, max_chars)

    async def take_pending(self, session: Any) -> str:
        """在调用方那条连接上取走 pending，不提交——见协议里那条「为什么必填」。"""
        return await asyncio.to_thread(self._take_pending_sync, session)

    async def read_assembly(
        self, session: Any, *, history_max_chars: int = 0
    ) -> ProfileAssembly:
        """在调用方给的连接上跑两条 SELECT。

        `session` 传的是 `sqlite3.Connection`——单机模式下通常就是本实例自己那条。收成
        `Any` 是 ADR-14 明确接受的代价（PG 侧那个参数是 `AsyncSession`，两者无共同接口）。
        """
        return await asyncio.to_thread(
            self._read_assembly_sync, session, history_max_chars
        )
