"""进程级 sync PG 连接池封装（M4H-3 commit 2）：让 pool_size 真实生效。

基于 psycopg 官方 psycopg_pool.ConnectionPool（sync 后端）：

- ``max_size`` = ``StorageConfig.pool_size``（DB 连接预算）。
- ``configure`` 回调对每条借出的连接初始化（memory 侧 ``register_vector``、
  session 侧 ``row_factory=dict_row``）。
- 借出超时或等待队列超上限抛 ``PoolTimeout``；归还时 pool 自动 rollback
  未结束事务（``psycopg_pool`` 的 reset 逻辑，aborted transaction recovery）。
- 生命周期只归 ``StorageRuntime.close()``；borrower（view / 调用方）不创建、
  不拥有、不关闭连接。

sqlite 路径不经过本模块。
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool, PoolTimeout

__all__ = ["PostgresPool", "PoolTimeout"]

# psycopg_pool.configure 回调签名；返回类型放宽为 Any 以兼容 register_vector
Configure = Callable[[psycopg.Connection[Any]], Any]


def _normalize_url(url: str) -> str:
    # SQLAlchemy 风格 scheme 与 psycopg 连接串兼容
    if url.startswith("postgresql+psycopg://"):
        return url.replace("postgresql+psycopg://", "postgresql://", 1)
    return url


class PostgresPool:
    """进程级 sync PG 连接池（psycopg_pool.ConnectionPool 封装）。"""

    def __init__(
        self,
        url: str,
        *,
        min_size: int,
        max_size: int,
        timeout: float = 5.0,
        max_waiting: int = 20,
        configure: Configure | None = None,
        kwargs: dict[str, Any] | None = None,
        name: str = "postgres",
    ) -> None:
        self._url = _normalize_url(url)
        self._closed = False
        self._close_lock = threading.Lock()
        self._pool = ConnectionPool(
            conninfo=self._url,
            min_size=min_size,
            max_size=max_size,
            timeout=timeout,
            max_waiting=max_waiting,
            configure=configure,
            kwargs=kwargs,
            name=name,
            open=True,
        )

    @property
    def size(self) -> int:
        """池最大连接数（= StorageConfig.pool_size）。"""
        return self._pool.max_size

    def _check_open(self) -> None:
        if self._closed:
            raise RuntimeError("PostgresPool is closed")

    @contextmanager
    def connection(self) -> Iterator[psycopg.Connection[Any]]:
        """借出一条连接执行事务，退出时归还。

        成功路径由调用方决定事务边界（view 方法显式 commit / 读操作保留
        INTRANS，归还时 psycopg_pool 自动 rollback）；异常路径**先显式
        rollback 再归还**，避免把 aborted / 半提交状态留给下一位借用人。
        rollback 再失败（连接已断）由 psycopg_pool 归还时丢弃并重建。
        """
        self._check_open()
        with self._pool.connection() as conn:
            try:
                yield conn
            except BaseException:
                try:
                    conn.rollback()
                except psycopg.Error:
                    pass
                raise

    def check(self) -> None:
        """健康检查：移除并替换池中的坏连接。"""
        self._check_open()
        self._pool.check()

    def wait(self, timeout: float = 30.0) -> None:
        """等待池初始化完成（连接数达到 min_size）。

        psycopg_pool 以 `open=True` 异步建 min_size 连接；启动后立即借出时
        可能在池未就绪时新建临时连接。需要「同一连接复用」语义的路径先 wait。
        """
        self._check_open()
        self._pool.wait(timeout=timeout)

    def close(self) -> None:
        if self._closed:
            return
        with self._close_lock:
            if self._closed:
                return
            try:
                self._pool.close()
            finally:
                self._closed = True
