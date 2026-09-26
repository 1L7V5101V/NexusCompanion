"""PG pg_search BM25 测试（C13 验收 4，ADR-4）——纯 fake 连接，不依赖真 PG。

覆盖：probe 分支（可用+已安装 / 缺一逐级短路）、probe+索引缓存、
`pg_available_extensions` 名匹配与 `pg_extension` 安装检查、CREATE INDEX 幂等
语句构造、`keyword_search_bm25` 查询构造（OR 短语、类型/scope/时间过滤、
batch_size 放大）、结果映射与 limits 截断、以及未启用/异常时降级返回 []。
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import pytest

from infra.storage.postgres_memory_store import (
    PostgresMemoryBackend,
    PostgresMemoryStore,
)


class _FakeCursor:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows
        self._pos = 0

    def fetchone(self) -> tuple[object, ...] | None:
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


class _FakeConnection:
    """按 SQL 片段路由的 psycopg 风格连接（execute 返回 cursor，commit no-op）。"""

    def __init__(
        self,
        *,
        probe_rows: list[tuple[object, ...]],
        search_rows: list[tuple[object, ...]] = [],
    ) -> None:
        self.probe_rows = probe_rows
        self.search_rows = search_rows
        self.executed: list[tuple[str, tuple[object, ...]]] = []
        self.commits = 0

    def execute(self, sql: str, params: tuple[object, ...] = ()) -> _FakeCursor:
        self.executed.append((sql, params))
        if "pg_available_extensions" in sql:
            rows = self.probe_rows
        elif "pg_extension" in sql:
            rows = self.probe_rows[1:]
        else:
            rows = self.search_rows
        return _FakeCursor(rows)

    def commit(self) -> None:
        self.commits += 1


class _FakePool:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn

    @contextmanager
    def connection(self) -> Iterator[_FakeConnection]:
        yield self._conn


def _probe_backend(
    probe_rows: list[tuple[object, ...]],
) -> tuple[PostgresMemoryBackend, _FakeConnection]:
    conn = _FakeConnection(probe_rows=probe_rows)
    backend = PostgresMemoryBackend.__new__(PostgresMemoryBackend)
    backend._url = "postgresql://fake"
    backend._vec_dim = 3
    backend._lock = threading.RLock()
    backend._closed = False
    backend._fts_available = False
    backend._pg_search_probed = False
    backend._pg_search_available = False
    backend._partitions_known = set()
    backend._pool = _FakePool(conn)
    backend._local = threading.local()
    return backend, conn


def _store(backend: PostgresMemoryBackend) -> PostgresMemoryStore:
    store = PostgresMemoryStore.__new__(PostgresMemoryStore)
    store._backend = backend
    store._owns_backend = False
    store._tenant_id = "test"
    return store


# ---------------------------------------------------------------------------
# probe：可用 + 已安装都要满足
# ---------------------------------------------------------------------------


def test_probe_requires_available_and_installed() -> None:
    backend, conn = _probe_backend([(1,), (1,)])
    store = _store(backend)

    assert store.ensure_bm25_ready() is True
    assert backend._pg_search_probed is True
    assert backend._pg_search_available is True
    # 探测两条 + 幂等建索引。
    assert len(conn.executed) == 3
    index_sql = [sql for sql, _ in conn.executed if "CREATE INDEX" in sql]
    assert index_sql == [
        "CREATE INDEX IF NOT EXISTS memory_items_bm25_idx "
        "ON memory_items USING bm25 (id, summary) WITH (key_field='id')"
    ]
    assert conn.commits == 1


def test_probe_false_when_extension_not_listed() -> None:
    backend, conn = _probe_backend([])
    store = _store(backend)

    assert store.ensure_bm25_ready() is False
    # 未可用 → 不建索引、不探测 pg_extension。
    assert not any("CREATE INDEX" in sql for sql, _ in conn.executed)
    assert not any("pg_extension" in sql for sql, _ in conn.executed)
    assert score_count(conn) == 0


def test_probe_false_when_extension_not_installed() -> None:
    backend, conn = _probe_backend([(1,)])
    store = _store(backend)

    assert store.ensure_bm25_ready() is False
    assert not any("CREATE INDEX" in sql for sql, _ in conn.executed)


def test_probe_cache_reuses_first_result() -> None:
    backend, conn = _probe_backend([(1,), (1,)])
    store = _store(backend)

    assert store.ensure_bm25_ready() is True
    conn.executed.clear()
    assert store.ensure_bm25_ready() is True
    # 缓存命中：不再执行任何 SQL。
    assert conn.executed == []


def test_probe_failure_fail_open_false() -> None:
    backend, _conn = _probe_backend([(1,), (1,)])

    def boom(*_a: object, **_k: object) -> _FakeCursor:
        raise RuntimeError("pg_search probe exploded")

    _conn.execute = boom  # type: ignore[method-assign]
    store = _store(backend)

    assert store.ensure_bm25_ready() is False
    assert backend._pg_search_available is False
    assert store.keyword_search_bm25(["缓存"]) == []


def score_count(_conn: _FakeConnection) -> int:
    return sum(1 for sql, _ in _conn.executed if "pdb.score" in sql)


# ---------------------------------------------------------------------------
# keyword_search_bm25：查询构造与结果映射
# ---------------------------------------------------------------------------

_ROW = (
    "id-1",
    "event",
    "用户调试了 Redis 缓存配置",
    "src-1",
    "2026-01-01T00:00:00Z",
    "2026-01-01T00:00:00Z",
    3,
    "2026-02-01T00:00:00Z",
    0,
    12.5,
)


def _search_backend(rows: list[tuple[object, ...]]) -> tuple[PostgresMemoryBackend, _FakeConnection]:
    conn = _FakeConnection(probe_rows=[(1,), (1,)], search_rows=rows)
    backend, _ = _probe_backend([(1,), (1,)])
    backend._pool = _FakePool(conn)
    return backend, conn


def test_keyword_search_bm25_builds_query_and_maps_row() -> None:
    backend, conn = _search_backend([_ROW])
    store = _store(backend)
    assert store.ensure_bm25_ready() is True

    hits = store.keyword_search_bm25(
        ["缓存", "配置", "其他"],
        memory_types=["event", "profile"],
        scope_channel="tg",
        scope_chat_id="1",
        require_scope_match=True,
    )
    assert len(hits) == 1
    hit = hits[0]
    assert hit["id"] == "id-1"
    assert hit["memory_type"] == "event"
    assert hit["summary"] == "用户调试了 Redis 缓存配置"
    assert hit["keyword_score"] == 12.5
    assert hit["_reinforcement"] == 3
    assert hit["_updated_at"] == "2026-02-01T00:00:00Z"
    assert hit["_emotional_weight"] == 0

    sql, params = conn.executed[-1]
    assert "summary @@@ %s" in sql
    assert "memory_type IN (%s,%s)" in sql
    assert "extra_json::jsonb->>'scope_channel'" in sql
    assert "tenant_id=%s" in sql
    # OR 短语为全部词条（去重按 dict 序）；首参是 tenant。
    assert params[:1] == ("test",)
    assert params[1] == '"缓存" OR "配置" OR "其他"'
    assert "LIMIT %s" in sql


def test_keyword_search_bm25_short_terms_filtered() -> None:
    backend, conn = _search_backend([_ROW])
    store = _store(backend)
    assert store.ensure_bm25_ready() is True

    # 全部为 <2 字词条：cleaned 为空，直接返回 []，不执行任何 pdb.score 查询。
    hits = store.keyword_search_bm25(["缓", "配", ""])
    assert hits == []
    assert score_count(conn) == 0


def test_keyword_search_bm25_time_filter_enlarges_batch_and_filters_rows() -> None:
    backend, conn = _search_backend([_ROW])
    store = _store(backend)
    assert store.ensure_bm25_ready() is True

    window_start = datetime(2025, 12, 31, tzinfo=timezone.utc)
    window_end = datetime(2026, 1, 2, tzinfo=timezone.utc)
    hits = store.keyword_search_bm25(
        ["缓存"],
        limit=8,
        time_start=window_start,
        time_end=window_end,
    )
    assert len(hits) == 1

    sql, params = conn.executed[-1]
    assert "happened_at >= %s" in sql
    assert "happened_at < %s" in sql
    # 时间过滤时 batch_size 放大到候选上限。
    assert int(params[-1]) >= 8


def test_keyword_search_bm25_time_filter_excludes_out_of_range() -> None:
    backend, _conn = _search_backend([_ROW])
    store = _store(backend)
    assert store.ensure_bm25_ready() is True

    window_start = datetime(2026, 1, 2, tzinfo=timezone.utc)
    window_end = datetime(2026, 1, 3, tzinfo=timezone.utc)
    hits = store.keyword_search_bm25(
        ["缓存"],
        limit=8,
        time_start=window_start,
        time_end=window_end,
    )
    # 不在时间窗内的行被丢弃。
    assert hits == []


def test_keyword_search_bm25_limit_break() -> None:
    rows = [_ROW, tuple(list(_ROW[:-1]) + [3.0])]
    backend, _conn = _search_backend(rows)
    store = _store(backend)
    assert store.ensure_bm25_ready() is True

    hits = store.keyword_search_bm25(["缓存"], limit=1)
    assert len(hits) == 1


def test_keyword_search_bm25_degrades_when_query_fails() -> None:
    backend, conn = _search_backend([_ROW])
    store = _store(backend)
    assert store.ensure_bm25_ready() is True

    def boom(*_a: object, **_k: object) -> _FakeCursor:
        raise RuntimeError("pdb.score unavailable")

    conn.execute = boom  # type: ignore[method-assign]
    assert store.keyword_search_bm25(["缓存"]) == []


def test_pg_backend_never_claims_fts5() -> None:
    backend, _conn = _probe_backend([(1,), (1,)])
    assert backend._fts_available is False


# ---------------------------------------------------------------------------
# scope 未启用时下降级路径仍可用
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("require_scope_match", [False, True])
def test_keyword_search_bm25_scope_filter_optional(require_scope_match: bool) -> None:
    backend, conn = _search_backend([_ROW])
    store = _store(backend)
    assert store.ensure_bm25_ready() is True

    hits = store.keyword_search_bm25(
        ["缓存"],
        require_scope_match=require_scope_match,
        scope_channel="tg",
        scope_chat_id="1",
    )
    assert len(hits) == 1
    sql, _params = conn.executed[-1]
    if require_scope_match:
        assert "extra_json::jsonb->>'scope_channel'" in sql
    else:
        assert "extra_json::jsonb->>" not in sql