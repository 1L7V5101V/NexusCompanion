"""批量写入后端：独立 psycopg 连接 + COPY（D1）。

- 迁移持有自己的连接，不经单连接 store 的 RLock 串行。
- ``memory_items`` 是唯一按 tenant 分区的表：导入前对目标 tenant 幂等
  provisioning（复用 ``partition_name_for_tenant``，partition-bound 双检）。
- ``sessions``/``messages``/``memory_replacements`` 等为普通表直接 COPY。
- 幂等：COPY 进临时 staging 表后 ``INSERT ... ON CONFLICT DO NOTHING``，
  重跑不产生重复（D2「保留源主键 + ON CONFLICT」）。
- 事务控制权在调用方（importer）：每批 ``copy_table`` 后由调用方 ``commit()``
  再写 checkpoint，保证「checkpoint 高水位 ≤ 已提交行数」的不变量
  （D2 断点续传正确性的前提）。
"""

from __future__ import annotations

import threading
from typing import Any, Iterable

import psycopg
from psycopg import sql as pgsql

from infra.storage.partitioning import partition_name_for_tenant

# 与 alembic schema 一致（vector(1024)，memory2.store.VEC_DIM）。
VEC_DIM = 1024

# 全局 advisory 锁：序列化跨进程/线程的 memory_items 分区创建。
_PARTITION_LOCK_KEY = 872_001_457


def sanitize_embedding(value: Any) -> str | None:
    """源 embedding 文本（JSON float 列表）→ pgvector 文本字面量。

    维度不符 / 非法 → None（与 alembic 迁移的 try_vector 同语义：损坏 embedding
    不阻断整表导入）。返回 ``[a, b, ...]`` 形式，PG 端 vector 解析。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parts = [float(v) for v in text.strip("[]").split(",") if v.strip()]
    except ValueError:
        return None
    if len(parts) != VEC_DIM:
        return None
    return "[" + ",".join(f"{v:.8g}" for v in parts) + "]"


def _partition_covers(conn: psycopg.Connection[Any], tenant: str) -> bool:
    """内存分区是否已存在覆盖 tenant 的叶子分区（bound 检查，不依赖命名）。

    兼容 alembic 迁移（``memory_items_<sanitize>``）与运行期
    ``partition_name_for_tenant``（``memory_items_<readable>_<md5>``）两套命名：
    只要有一个叶子分区的 bound 含该 tenant 即可。
    """
    row = conn.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM pg_partition_tree('memory_items'::regclass) pt
            JOIN pg_class c ON c.oid = pt.relid
            WHERE pt.isleaf
              AND pg_get_expr(c.relpartbound, c.oid)
                  = 'FOR VALUES IN (' || quote_literal(%s) || ')'
        )
        """,
        (tenant,),
    ).fetchone()
    return bool(row and row[0])


class BulkPgWriter:
    """COPY 批量写入器：单条独立连接。事务提交时机由调用方决定。"""

    def __init__(self, pg_url: str) -> None:
        self._url = pg_url
        self._lock = threading.Lock()
        self._conn = psycopg.connect(pg_url, autocommit=False)
        # staging 表按目标表缓存（创建一次，批间 TRUNCATE 复用）。
        self._staging: dict[str, str] = {}
        self._provisioned: set[str] = set()

    # ── memory_items 分区 provisioning（D1：复用 partition_name_for_tenant + 双检）──

    def provision_partitions(self, tenants: Iterable[str]) -> None:
        """幂等建分区：partition-bound 双检 + advisory 锁串行。

        任一套命名（迁移 / 运行期）下已覆盖的 tenant 一律跳过；只对缺失的
        用 ``partition_name_for_tenant`` 命名新建。调用时机：该表首批 COPY 前，
        本方法结束时自行 commit（释放 advisory 锁），不携带未提交数据。
        """
        pending = [t for t in tenants if t not in self._provisioned]
        if not pending:
            return
        with self._lock:
            self._conn.execute(
                "SELECT pg_advisory_lock(%s)", (_PARTITION_LOCK_KEY,)
            )
            try:
                for tenant in pending:
                    if _partition_covers(self._conn, tenant):
                        self._provisioned.add(tenant)
                        continue
                    name = partition_name_for_tenant(tenant)
                    if not _partition_covers(self._conn, tenant):
                        self._conn.execute(
                            pgsql.SQL(
                                "CREATE TABLE {} PARTITION OF memory_items "
                                "FOR VALUES IN ({})"
                            ).format(
                                pgsql.Identifier(name), pgsql.Literal(tenant)
                            )
                        )
                    self._provisioned.add(tenant)
            finally:
                self._conn.execute(
                    "SELECT pg_advisory_unlock(%s)", (_PARTITION_LOCK_KEY,)
                )
                self._conn.commit()

    # ── COPY + ON CONFLICT（幂等）──

    def copy_table(
        self,
        table: str,
        columns: list[str],
        rows: Iterable[dict[str, Any]],
        *,
        partitioned: bool = False,
    ) -> int:
        """把行批量写入目标表；返回实际插入行数（冲突忽略不计）。

        通过临时 staging 表实现「COPY 速度 + ON CONFLICT DO NOTHING 幂等」：
        COPY 进 staging → ``INSERT INTO target SELECT FROM staging ON CONFLICT``
        → 清空 staging。本方法不提交；调用方决定提交时机（batch 边界）。
        """
        rows_list = list(rows)
        if not rows_list:
            return 0
        with self._lock:
            stage = self._staging_for(table, columns)
            self._conn.execute(f"TRUNCATE TABLE {stage}")
            with self._conn.cursor() as cur:
                with cur.copy(
                    pgsql.SQL("COPY {} ({}) FROM STDIN").format(
                        pgsql.Identifier(stage),
                        pgsql.SQL(", ").join(
                            pgsql.Identifier(c) for c in columns
                        ),
                    )
                ) as copy:
                    for row in rows_list:
                        copy.write_row([row.get(c) for c in columns])
            insert_sql = pgsql.SQL(
                "INSERT INTO {} ({}) SELECT {} FROM {} ON CONFLICT DO NOTHING"
            ).format(
                pgsql.Identifier(table),
                pgsql.SQL(", ").join(pgsql.Identifier(c) for c in columns),
                pgsql.SQL(", ").join(pgsql.Identifier(c) for c in columns),
                pgsql.Identifier(stage),
            )
            cur = self._conn.execute(insert_sql)
            inserted = cur.rowcount if cur.rowcount is not None else 0
        return inserted

    def commit(self) -> None:
        with self._lock:
            self._conn.commit()

    def rollback(self) -> None:
        with self._lock:
            self._conn.rollback()

    def close(self) -> None:
        with self._lock:
            for stage in self._staging.values():
                try:
                    self._conn.execute(f"DROP TABLE IF EXISTS {stage}")
                except psycopg.Error:
                    pass
            self._conn.close()

    # ── 内部 ──

    def _staging_for(self, table: str, columns: list[str]) -> str:
        stage = self._staging.get(table)
        if stage is not None:
            return stage
        stage = f"_mig_stage_{table}"
        self._conn.execute(
            pgsql.SQL(
                "CREATE TEMP TABLE {} (LIKE {} INCLUDING DEFAULTS)"
            ).format(pgsql.Identifier(stage), pgsql.Identifier(table))
        )
        self._staging[table] = stage
        return stage
