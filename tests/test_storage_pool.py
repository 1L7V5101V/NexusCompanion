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
from psycopg import sql as pgsql
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


# ── transaction rollback / recovery（M4H-3 commit 5）─────────────────────────

_RB_TENANT = "m4h3_rb"
_RB_PARTITION = f"memory_items_{_RB_TENANT}"
_RB_SOURCE = "m4h3-rb-rollback"


def _ensure_rb_partition(pool: PostgresPool) -> None:
    """memory_items 是 LIST 分区表（无 DEFAULT 分区），测试 tenant 需先建分区。

    DDL 不支持 psycopg 参数化字面值（partition bound 里 $1 无法推断类型），
    用 psycopg.sql 构造标识符与字面量；值来自测试常量，非用户输入。
    """
    with pool.connection() as conn:
        conn.execute(
            pgsql.SQL(
                "CREATE TABLE IF NOT EXISTS {} PARTITION OF memory_items "
                "FOR VALUES IN ({})"
            ).format(pgsql.Identifier(_RB_PARTITION), pgsql.Literal(_RB_TENANT))
        )
        conn.commit()


def test_pool_borrow_failure_rolls_back_before_return(pg_url: str) -> None:
    pool = PostgresPool(pg_url, min_size=1, max_size=2, name="t_rollback")
    try:
        _ensure_rb_partition(pool)
        # 借出时先写入再抛异常：异常路径显式 rollback，数据不落库
        with pytest.raises(RuntimeError, match="boom"):
            with pool.connection() as conn:
                conn.execute(
                    "INSERT INTO memory_items (tenant_id, id, memory_type, summary, "
                    "content_hash, source_ref) VALUES (%s,%s,%s,%s,%s,%s)",
                    (
                        _RB_TENANT,
                        "rb-1",
                        "event",
                        "rb-summary",
                        "rb-hash-1",
                        _RB_SOURCE,
                    ),
                )
                raise RuntimeError("boom")
        # rollback 生效：source_ref 未落库
        with pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM memory_items WHERE source_ref=%s",
                (_RB_SOURCE,),
            ).fetchone()
            assert row[0] == 0
    finally:
        pool.close()


def test_pool_aborted_transaction_recovers(pg_url: str) -> None:
    pool = PostgresPool(pg_url, min_size=1, max_size=2, name="t_abort")
    try:
        # 语法错误 → 事务进入 aborted（INERROR）：异常路径 rollback 后连接恢复
        with pytest.raises(psycopg.errors.UndefinedTable):
            with pool.connection() as conn:
                conn.execute("SELECT * FROM memory_items_missing_table")
        # 后续借出正常，不会带出 aborted 状态
        with pool.connection() as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        pool.close()


def test_pool_check_replaces_broken_connection(pg_url: str) -> None:
    pool = PostgresPool(pg_url, min_size=1, max_size=2, name="t_check")
    try:
        pool.wait()
        with pool.connection() as conn:
            pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
        # 用独立连接从后端 kill 掉池里刚归还的这条空闲连接（模拟连接被强杀/网络断）。
        # 不能在池内连接上自杀：min_size=1 下归还后复用的是同一 backend，会误伤当前 borrow。
        killer = psycopg.connect(pg_url)
        try:
            killer.execute("SELECT pg_terminate_backend(%s)", (pid,))
        finally:
            killer.close()
        # check() 主动体检：坏连接被识别移除，后续借出自动换新连接
        pool.check()
        with pool.connection() as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        pool.close()
