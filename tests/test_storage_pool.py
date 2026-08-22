"""PG 连接池封装测试（PG-gated，M4H-3 commit 2）。

覆盖 PostgresPool 基础行为：借出/归还、max_size 对应 pool_size、configure
（register_vector / row_factory=dict_row）对每条连接生效、close 后拒绝借出。
并发 / 超时 / 耗尽 / aborted recovery 在后续 commit 补。
"""
from __future__ import annotations

import os
import threading
import time

import psycopg
import pytest
from pgvector.psycopg import register_vector
from psycopg import sql as pgsql
from psycopg.rows import dict_row
from psycopg_pool import PoolTimeout, TooManyRequests

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


# ── pool exhaustion 与并发（M4H-3 commit 6）────────────────────────────────


def test_pool_concurrent_borrow_up_to_max_size(pg_url: str) -> None:
    pool = PostgresPool(pg_url, min_size=1, max_size=4, name="t_conc")
    n = 4
    barrier = threading.Barrier(n)
    pids: list[int] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _work() -> None:
        try:
            with pool.connection() as conn:
                # 两道 barrier：确认 n 条连接同时借出、同时持有
                barrier.wait(timeout=10)
                pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
                with lock:
                    pids.append(pid)
                barrier.wait(timeout=10)
        except BaseException as e:  # noqa: BLE001 - 收集到 errors 统一断言
            with lock:
                errors.append(e)

    try:
        pool.wait()
        threads = [threading.Thread(target=_work) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert not errors, errors
        # 并发借出到 max_size：n 条连接同时有效且后端互不相同
        assert len(set(pids)) == n
    finally:
        pool.close()


def test_pool_borrow_timeout_raises(pg_url: str) -> None:
    pool = PostgresPool(
        pg_url, min_size=1, max_size=1, timeout=0.5, name="t_tmo"
    )
    try:
        pool.wait()
        with pool.connection() as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
            # 唯一的连接被占用：并发借出等待超过 timeout → PoolTimeout
            with pytest.raises(PoolTimeout):
                with pool.connection():
                    pass
        # 释放后借出恢复（超时者由 putconn 惰性清出等待队列）
        with pool.connection() as conn:
            assert conn.execute("SELECT 1").fetchone()[0] == 1
    finally:
        pool.close()


def test_pool_max_waiting_cap_raises(pg_url: str) -> None:
    pool = PostgresPool(
        pg_url, min_size=1, max_size=1, max_waiting=1, timeout=5.0, name="t_waitcap"
    )
    try:
        pool.wait()
        release = threading.Event()
        outcomes: list[str] = []

        def _waiter() -> None:
            try:
                with pool.connection() as conn:
                    conn.execute("SELECT 1")
                    release.wait(5)
                    outcomes.append("ok")
            except BaseException as e:  # noqa: BLE001
                outcomes.append(f"err:{type(e).__name__}")

        with pool.connection() as conn:
            # waiter 进入等待队列（psycopg_pool 内部 _waiting）
            t = threading.Thread(target=_waiter)
            t.start()
            deadline = time.monotonic() + 3
            while (
                len(pool._pool._waiting) < 1 and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert len(pool._pool._waiting) == 1
            # 第二个等待者超出 max_waiting=1 → 立即 TooManyRequests
            with pytest.raises(TooManyRequests):
                with pool.connection():
                    pass
        # 释放连接后 waiter 拿到连接并正常完成
        release.set()
        t.join(5)
        assert outcomes == ["ok"]
    finally:
        pool.close()


def test_pool_concurrent_reuse_bounded_by_max_size(pg_url: str) -> None:
    pool = PostgresPool(pg_url, min_size=1, max_size=2, name="t_reuse_conc")
    workers = 6
    lock = threading.Lock()
    pids: list[int] = []
    errors: list[BaseException] = []

    def _work() -> None:
        try:
            with pool.connection() as conn:
                pid = conn.execute("SELECT pg_backend_pid()").fetchone()[0]
                with lock:
                    pids.append(pid)
                time.sleep(0.02)  # 短暂持有，制造等待/复用窗口
        except BaseException as e:  # noqa: BLE001
            with lock:
                errors.append(e)

    try:
        pool.wait()
        threads = [threading.Thread(target=_work) for _ in range(workers)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        assert not errors, errors
        # max_size=2 → 并发下全程后端连接数不超 2 个（归还复用而非新建）
        assert len(set(pids)) <= 2
    finally:
        pool.close()
