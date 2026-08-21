"""PG 连接池封装测试（PG-gated，M4H-3 commit 2）。

覆盖 PostgresPool 基础行为：借出/归还、max_size 对应 pool_size、configure
（register_vector / row_factory=dict_row）对每条连接生效、close 后拒绝借出。
并发 / 超时 / 耗尽 / aborted recovery 在后续 commit 补。
"""
from __future__ import annotations

import os

import psycopg
import pytest
from pgvector.psycopg import register_vector
from psycopg.rows import dict_row

from infra.storage.pool import PostgresPool

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
        pytest.skip(f"本地 PG 不可用（{PG_URL}），跳过 pool 测试")
    return PG_URL


def test_pool_borrow_and_return(pg_url: str) -> None:
    pool = PostgresPool(pg_url, min_size=1, max_size=2, name="t_borrow")
    try:
        with pool.connection() as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        pool.close()


def test_pool_max_size_matches_pool_size(pg_url: str) -> None:
    pool = PostgresPool(pg_url, min_size=1, max_size=7, name="t_size")
    try:
        assert pool.size == 7
    finally:
        pool.close()


def test_pool_configure_registers_vector(pg_url: str) -> None:
    pool = PostgresPool(
        pg_url,
        min_size=1,
        max_size=2,
        configure=register_vector,
        name="t_vec",
    )
    try:
        with pool.connection() as conn:
            row = conn.execute("SELECT '[1,2,3]'::vector").fetchone()
            assert list(row[0].to_list()) == [1.0, 2.0, 3.0]
    finally:
        pool.close()


def test_pool_row_factory_dict(pg_url: str) -> None:
    pool = PostgresPool(
        pg_url,
        min_size=1,
        max_size=2,
        kwargs={"row_factory": dict_row},
        name="t_dict",
    )
    try:
        with pool.connection() as conn:
            row = conn.execute("SELECT 1 AS n").fetchone()
            assert row["n"] == 1
    finally:
        pool.close()


def test_pool_close_rejects_borrow(pg_url: str) -> None:
    pool = PostgresPool(pg_url, min_size=1, max_size=2, name="t_closed")
    pool.close()
    with pytest.raises(RuntimeError):
        with pool.connection():
            pass


def test_pool_reuses_connection_across_borrows(pg_url: str) -> None:
    pool = PostgresPool(pg_url, min_size=1, max_size=2, name="t_reuse")
    try:
        pool.wait()
        with pool.connection() as conn:
            pid1 = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        with pool.connection() as conn:
            pid2 = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        # min_size=1 下池只保留一条连接，归还后复用的应是同一后端连接
        assert pid1 == pid2
    finally:
        pool.close()
