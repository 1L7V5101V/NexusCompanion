"""
PostgresMemoryStore：PostgreSQL + pgvector 版的 MemoryStore2 后端（M2）

与 SQLite 版（memory2/store.py）保持接口与返回类型一致：
- 所有操作按 tenant_id 作用域（单库多租户，决策 C）
- embedding 存原生 vector(1024) 列；查询走每分区 HNSW（决策 B）
- 时间列（happened_at / created_at / updated_at）在 PG 为 TIMESTAMPTZ，
  对外仍返回 ISO 字符串，与 SQLite 返回类型一致
- 复用 memory2.store 的 module-level 纯函数与 _score_embedding_rows，
  避免语义漂移（parity 由 M7 回归测试兜底）
"""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, LiteralString, cast

import psycopg
from pgvector.psycopg import register_vector

from infra.storage.partitioning import PartitionNotReady, partition_name_for_tenant
from infra.storage.pool import PostgresPool
from memory2.store import (
    MemoryStore2 as _SQLiteMemoryStore,
    _TIME_FILTER_KEYWORD_CANDIDATE_LIMIT,
    _TIME_FILTER_MARGIN,
    _content_hash,
    _coerce_emotional_weight,
    _coerce_float,
    _coerce_int,
    _cosine_similarity,
    _hotness_score,
    _is_memory_time_in_range,
    _now_iso,
    _parse_memory_time,
    _result_score,
    _source_ref_message_ids,
)

logger = logging.getLogger(__name__)

VEC_DIM = 1024
# extra_json 是 TEXT，->> 需要显式 cast 为 jsonb（写入侧始终 json.dumps，保证合法）
_SCOPE_CHANNEL_SQL = "COALESCE(TRIM(extra_json::jsonb->>'scope_channel'), '')"
_SCOPE_CHAT_SQL = "COALESCE(TRIM(extra_json::jsonb->>'scope_chat_id'), '')"

_MemoryHit = dict[str, object]
_EmbeddingRow = tuple[
    str,
    str,
    str,
    list[float] | None,
    dict[str, object],
    str | None,
    str | None,
]

# SQLite 版的 _score_embedding_rows 不引用 self（纯 numpy 打分），
# 用 Callable[..., ...] 收窄隐藏 self 形参，直接复用避免语义漂移。
_score_embedding_rows: Callable[..., list[dict[str, object]]] = cast(
    Callable[..., list[dict[str, object]]],
    _SQLiteMemoryStore._score_embedding_rows,
)


def _q(text: str) -> LiteralString:
    """标记运行期拼接的 SQL 文本为可信 LiteralString。

    仅用于占位符数量/allowlist 片段拼接的查询文本；标识符与字面量一律走
    sql.Identifier / sql.Literal，绝不把用户输入拼进 SQL 文本。
    """
    return cast(LiteralString, text)


def _to_iso(value: object) -> str | None:
    """TIMESTAMPTZ -> ISO 字符串（保持与 SQLite 返回 str|None 一致）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _pg_time_prefilter_clauses(
    column: str,
    time_start: datetime | None,
    time_end: datetime | None,
) -> tuple[list[str], list[object]]:
    """PG 版时间预过滤：TIMESTAMPTZ 直接与 datetime 比较（SQLite 用字符串）。"""
    clauses = [f"{column} IS NOT NULL"]
    params: list[object] = []
    if time_start is not None:
        clauses.append(f"{column} >= %s")
        params.append(time_start - _TIME_FILTER_MARGIN)
    if time_end is not None:
        clauses.append(f"{column} < %s")
        params.append(time_end + _TIME_FILTER_MARGIN)
    return clauses, params


def _coerce_embedding(value: object) -> list[float] | None:
    """pgvector 列读出的 embedding：Vector/list -> list[float]，NULL -> None。"""
    if value is None:
        return None
    if isinstance(value, list):
        return [float(v) for v in value]
    to_list = getattr(value, "to_list", None)
    if callable(to_list):
        out = to_list()
        if isinstance(out, list):
            return [float(v) for v in out]
    # 兜底：非 list 的 iterable（如 numpy array）
    return [float(v) for v in cast(Iterable[Any], value)]


class PostgresMemoryBackend:
    """resource-owner：持有连接池 + RLock + 分区存在性缓存（M4H-2 C / M4H-3）。

    跨 tenant 共享；tenant-bound view（PostgresMemoryStore(backend, tenant_id)）
    只委托 pool 借出的连接与锁，不拥有 backend。底层生命周期只归
    StorageRuntime.close()。
    """

    def __init__(
        self,
        postgres_url: str,
        *,
        vec_dim: int = VEC_DIM,
        pool_size: int = 1,
        timeout: float = 5.0,
        max_waiting: int = 20,
    ) -> None:
        # SQLAlchemy 风格 scheme 与 psycopg 连接串兼容
        if postgres_url.startswith("postgresql+psycopg://"):
            postgres_url = postgres_url.replace(
                "postgresql+psycopg://", "postgresql://", 1
            )
        self._url = postgres_url
        self._vec_dim = vec_dim
        self._lock = threading.RLock()
        self._closed = False
        # PG 无 FTS5；镜像 SQLite 的 _fts_available 让 retriever 守卫短路到
        # keyword_search_summary（PG store 已实现，语义等价于 SQLite 基线 OR-LIKE）
        self._fts_available = False
        # 分区存在性是 catalog 级，天然跨 tenant 共享，放 backend
        self._partitions_known: set[str] = set()
        # pool 是连接唯一所有者；每条借出连接经 configure 注册 vector 适配器
        self._pool = PostgresPool(
            postgres_url,
            min_size=min(1, pool_size),
            max_size=pool_size,
            timeout=timeout,
            max_waiting=max_waiting,
            configure=register_vector,
            name="memory",
        )
        # 当前线程借出的连接（view 方法体经 connection() 借出/归还）
        self._local = threading.local()

    @property
    def conn(self) -> psycopg.Connection[Any]:
        """当前线程借出的连接；未借出时访问属编程错误。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            raise RuntimeError(
                "PostgresMemoryBackend: no connection borrowed "
                "(missing `with self._backend.connection()`)"
            )
        return conn

    @contextmanager
    def connection(self) -> Iterator[None]:
        """借出一条 pool 连接供方法体使用，退出时归还。

        嵌套调用复用当前连接（RLock 同线程语义），仅最外层归还。
        """
        self._check_open()
        if getattr(self._local, "conn", None) is not None:
            self._local.depth += 1
            try:
                yield
            finally:
                self._local.depth -= 1
            return
        with self._pool.connection() as conn:
            self._local.conn = conn
            self._local.depth = 1
            try:
                yield
            finally:
                self._local.depth = 0
                self._local.conn = None

    def _check_open(self) -> None:
        if self._closed:
            raise psycopg.ProgrammingError(
                "PostgresMemoryBackend: pool is closed"
            )

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._pool.close()
        finally:
            self._closed = True


class PostgresMemoryStore:
    def __init__(
        self,
        postgres_url: str | PostgresMemoryBackend,
        tenant_id: str = "default",
        vec_dim: int = VEC_DIM,
    ) -> None:
        # 传入 PostgresMemoryBackend = tenant-bound view：不建连接、不拥有 backend；
        # 传入 URL = owned 构造（factory/tests 直连，构造即开一条连接）。
        if isinstance(postgres_url, PostgresMemoryBackend):
            self._backend = postgres_url
            self._owns_backend = False
        else:
            self._backend = PostgresMemoryBackend(postgres_url, vec_dim=vec_dim)
            self._owns_backend = True
        self._tenant_id = tenant_id

    @property
    def _conn(self) -> psycopg.Connection[Any]:
        return self._backend.conn

    @property
    def _lock(self) -> threading.RLock:
        return self._backend._lock

    @property
    def _vec_dim(self) -> int:
        return self._backend._vec_dim

    @property
    def _fts_available(self) -> bool:
        return self._backend._fts_available

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def _check_open(self) -> None:
        self._backend._check_open()

    def close(self) -> None:
        """owned 时关闭 backend；view 是 no-op（不关共享连接）。"""
        if self._owns_backend:
            self._backend.close()

    def __del__(self) -> None:
        self.close()

    # ------------------------------------------------------------------
    # 分区管理
    # ------------------------------------------------------------------

    def _assert_partition_ready(self) -> None:
        """只读 readiness 检查：分区必须已由 provisioning control path 建好。

        写路径不执行 DDL；分区缺失直接抛 ``PartitionNotReady``（fail-fast），
        绝不在此懒建分区（M4H-4：DDL 只归独立 control worker）。只读
        ``to_regclass`` 探测 + 缓存填充，无 advisory 锁、无 commit。
        """
        name = partition_name_for_tenant(self._tenant_id)
        if name in self._backend._partitions_known:
            return
        with self._lock, self._backend.connection():
            self._check_open()
            if name in self._backend._partitions_known:
                return
            row = self._conn.execute(
                "SELECT to_regclass(%s)", (name,)
            ).fetchone()
            if row is None or row[0] is None:
                raise PartitionNotReady(
                    f"tenant {self._tenant_id} partition {name} not provisioned"
                )
            self._backend._partitions_known.add(name)

    # ------------------------------------------------------------------
    # 写操作
    # ------------------------------------------------------------------

    def upsert_item(
        self,
        memory_type: str,
        summary: str,
        embedding: list[float] | None,
        source_ref: str | None = None,
        extra: dict[str, object] | None = None,
        happened_at: str | None = None,
        emotional_weight: int = 0,
    ) -> str:
        """写入或强化一条记忆。返回 'new:id' 或 'reinforced:id'"""
        chash = _content_hash(summary, memory_type)
        emotional_weight = _coerce_emotional_weight(emotional_weight)
        # 维度不符时存 NULL（对齐 SQLite 的 sqlite-vec 跳过，不落向量）
        emb_val = (
            embedding
            if embedding is not None and len(embedding) == self._vec_dim
            else None
        )
        with self._lock, self._backend.connection():
            self._check_open()
            self._assert_partition_ready()
            existing = self._conn.execute(
                "SELECT id, status FROM memory_items "
                "WHERE tenant_id=%s AND content_hash=%s AND memory_type=%s",
                (self._tenant_id, chash, memory_type),
            ).fetchone()
            if existing:
                row_id, status = existing
                if status == "superseded":
                    self._conn.execute(
                        "UPDATE memory_items SET status='active', "
                        "reinforcement=reinforcement+1, updated_at=%s, "
                        "emotional_weight=GREATEST(emotional_weight, %s) "
                        "WHERE tenant_id=%s AND id=%s",
                        (_now_iso(), emotional_weight, self._tenant_id, row_id),
                    )
                else:
                    self._conn.execute(
                        "UPDATE memory_items SET reinforcement=reinforcement+1, "
                        "updated_at=%s, "
                        "emotional_weight=GREATEST(emotional_weight, %s) "
                        "WHERE tenant_id=%s AND id=%s",
                        (_now_iso(), emotional_weight, self._tenant_id, row_id),
                    )
                self._conn.commit()
                return f"reinforced:{row_id}"

            item_id = hashlib.md5(f"{chash}{time.time()}".encode()).hexdigest()[:12]
            self._conn.execute(
                """INSERT INTO memory_items
                   (tenant_id, id, memory_type, summary, content_hash, embedding,
                    emotional_weight, extra_json, source_ref, happened_at,
                    created_at, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                (
                    self._tenant_id,
                    item_id,
                    memory_type,
                    summary,
                    chash,
                    emb_val,
                    emotional_weight,
                    json.dumps(extra, ensure_ascii=False) if extra else None,
                    source_ref,
                    happened_at,
                    _now_iso(),
                    _now_iso(),
                ),
            )
            self._conn.commit()
            return f"new:{item_id}"

    def upsert_consolidation_event(
        self,
        *,
        source_ref: str,
        summary: str,
        embedding: list[float] | None,
        extra: dict[str, object] | None = None,
        happened_at: str | None = None,
        emotional_weight: int = 0,
    ) -> str:
        """原子写入 consolidation event：同一 (tenant, source_ref) 最多写一次。"""
        src = (source_ref or "").strip()
        text = (summary or "").strip()
        if not src or not text:
            return "skipped:empty"
        emotional_weight = _coerce_emotional_weight(emotional_weight)
        emb_val = (
            embedding
            if embedding is not None and len(embedding) == self._vec_dim
            else None
        )
        with self._lock, self._backend.connection():
            self._check_open()
            self._assert_partition_ready()
            try:
                already = self._conn.execute(
                    "SELECT item_id FROM consolidation_events "
                    "WHERE tenant_id=%s AND source_ref=%s",
                    (self._tenant_id, src),
                ).fetchone()
                if already is not None:
                    self._conn.commit()
                    existing_id = already[0] or ""
                    return f"skipped:{existing_id or src}"

                chash = _content_hash(text, "event")
                existing = self._conn.execute(
                    "SELECT id, status FROM memory_items "
                    "WHERE tenant_id=%s AND content_hash=%s AND memory_type=%s",
                    (self._tenant_id, chash, "event"),
                ).fetchone()

                if existing:
                    row_id, status = existing
                    if status == "superseded":
                        self._conn.execute(
                            "UPDATE memory_items SET status='active', "
                            "reinforcement=reinforcement+1, updated_at=%s, "
                            "emotional_weight=GREATEST(emotional_weight, %s), "
                            "happened_at=COALESCE(happened_at, %s) "
                            "WHERE tenant_id=%s AND id=%s",
                            (
                                _now_iso(),
                                emotional_weight,
                                happened_at,
                                self._tenant_id,
                                row_id,
                            ),
                        )
                    else:
                        self._conn.execute(
                            "UPDATE memory_items SET reinforcement=reinforcement+1, "
                            "updated_at=%s, "
                            "emotional_weight=GREATEST(emotional_weight, %s), "
                            "happened_at=COALESCE(happened_at, %s) "
                            "WHERE tenant_id=%s AND id=%s",
                            (
                                _now_iso(),
                                emotional_weight,
                                happened_at,
                                self._tenant_id,
                                row_id,
                            ),
                        )
                    item_id = row_id
                    result = f"reinforced:{row_id}"
                else:
                    item_id = hashlib.md5(f"{chash}{time.time()}".encode()).hexdigest()[:12]
                    self._conn.execute(
                        """INSERT INTO memory_items
                           (tenant_id, id, memory_type, summary, content_hash, embedding,
                            emotional_weight, extra_json, source_ref, happened_at,
                            created_at, updated_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                        (
                            self._tenant_id,
                            item_id,
                            "event",
                            text,
                            chash,
                            emb_val,
                            emotional_weight,
                            json.dumps(extra, ensure_ascii=False) if extra else None,
                            src,
                            happened_at,
                            _now_iso(),
                            _now_iso(),
                        ),
                    )
                    result = f"new:{item_id}"

                self._conn.execute(
                    "INSERT INTO consolidation_events(tenant_id, source_ref, item_id, created_at) "
                    "VALUES (%s, %s, %s, %s)",
                    (self._tenant_id, src, item_id, _now_iso()),
                )
                self._conn.commit()
                return result
            except Exception:
                self._conn.rollback()
                raise

    def merge_item_raw(
        self,
        item_id: str,
        new_summary: str,
        new_hash: str,
        new_embedding: list[float],
        new_extra: dict[str, object] | None = None,
    ) -> None:
        """原子更新 merge 目标：summary + content_hash + embedding + reinforcement。

        content_hash 冲突（极低概率）时 supersede 旧条目并由 upsert_item
        走强化路径，与 SQLite 语义一致。
        """
        emb_val = (
            new_embedding
            if new_embedding is not None and len(new_embedding) == self._vec_dim
            else None
        )
        with self._lock, self._backend.connection():
            self._check_open()
            self._assert_partition_ready()
            try:
                if new_extra is not None:
                    self._conn.execute(
                        """UPDATE memory_items
                           SET summary=%s, content_hash=%s, embedding=%s,
                               extra_json=%s, reinforcement=reinforcement+1, updated_at=%s
                           WHERE tenant_id=%s AND id=%s""",
                        (
                            new_summary,
                            new_hash,
                            emb_val,
                            json.dumps(new_extra, ensure_ascii=False),
                            _now_iso(),
                            self._tenant_id,
                            item_id,
                        ),
                    )
                else:
                    self._conn.execute(
                        """UPDATE memory_items
                           SET summary=%s, content_hash=%s, embedding=%s,
                               reinforcement=reinforcement+1, updated_at=%s
                           WHERE tenant_id=%s AND id=%s""",
                        (
                            new_summary,
                            new_hash,
                            emb_val,
                            _now_iso(),
                            self._tenant_id,
                            item_id,
                        ),
                    )
                self._conn.commit()
            except psycopg.errors.UniqueViolation:
                self._conn.rollback()
                logger.warning(
                    "merge_item_raw: content_hash collision for item %s, "
                    "superseding and falling back to upsert",
                    item_id,
                )
                row = self._conn.execute(
                    "SELECT memory_type FROM memory_items "
                    "WHERE tenant_id=%s AND id=%s",
                    (self._tenant_id, item_id),
                ).fetchone()
                if row:
                    self.mark_superseded(item_id)
                    self.upsert_item(
                        memory_type=row[0],
                        summary=new_summary,
                        embedding=new_embedding,
                    )

    def mark_superseded(self, item_id: str) -> None:
        with self._lock, self._backend.connection():
            self._check_open()
            self._conn.execute(
                "UPDATE memory_items SET status='superseded', updated_at=%s "
                "WHERE tenant_id=%s AND id=%s",
                (_now_iso(), self._tenant_id, item_id),
            )
            self._conn.commit()

    def mark_superseded_batch(self, ids: list[str]) -> None:
        if not ids:
            return
        now = _now_iso()
        with self._lock, self._backend.connection():
            self._check_open()
            with self._conn.cursor() as cur:
                cur.executemany(
                    "UPDATE memory_items SET status='superseded', updated_at=%s "
                    "WHERE tenant_id=%s AND id=%s",
                    [(now, self._tenant_id, item_id) for item_id in ids],
                )
            self._conn.commit()

    def undo_by_message_sources(
        self,
        message_ids: list[str],
        *,
        dry_run: bool = False,
    ) -> dict[str, object]:
        """按消息 source 撤销记忆（引擎 undo_by_message_sources 委托此处）。

        与 SQLite 版语义一致：全扫带 source_ref 的条目 → 命中则 supersede →
        恢复其取代的旧条目（仅当旧条目无其他 active 取代）。dry_run 只探测。
        单事务：整段在锁内，末尾一次 commit。
        """
        clean_ids = [str(item).strip() for item in message_ids if str(item).strip()]
        if not clean_ids:
            return {"affected_ids": [], "restored_ids": [], "rollback_source_ids": []}
        target_ids = set(clean_ids)
        with self._lock, self._backend.connection():
            self._check_open()
            rows = self._conn.execute(
                """
                SELECT id, source_ref
                FROM memory_items
                WHERE tenant_id = %s AND COALESCE(source_ref, '') != ''
                """,
                (self._tenant_id,),
            ).fetchall()
            affected_ids: set[str] = set()
            rollback_source_ids: set[str] = set()
            for item_id, source_ref in rows:
                source = str(source_ref or "").strip()
                base_ids = _source_ref_message_ids(source)
                if source in target_ids:
                    affected_ids.add(str(item_id))
                    rollback_source_ids.add(source)
                    continue
                if base_ids and target_ids.intersection(base_ids):
                    affected_ids.add(str(item_id))
                    rollback_source_ids.update(base_ids)

            if affected_ids and not dry_run:
                now = _now_iso()
                with self._conn.cursor() as cur:
                    cur.executemany(
                        "UPDATE memory_items SET status='superseded', updated_at=%s "
                        "WHERE tenant_id=%s AND id=%s",
                        [(now, self._tenant_id, item_id) for item_id in sorted(affected_ids)],
                    )
            restored_ids = self._restore_replacements_for_undo(
                affected_ids,
                dry_run=dry_run,
            )
            if not dry_run:
                self._conn.commit()
        return {
            "affected_ids": sorted(affected_ids),
            "restored_ids": sorted(restored_ids),
            "rollback_source_ids": sorted(rollback_source_ids),
        }

    def _restore_replacements_for_undo(
        self,
        affected_ids: set[str],
        *,
        dry_run: bool = False,
    ) -> set[str]:
        if not affected_ids:
            return set()
        sorted_affected = sorted(affected_ids)
        placeholders = ",".join("%s" for _ in sorted_affected)
        rows = self._conn.execute(
            f"""
            SELECT DISTINCT old_item_id
            FROM memory_replacements
            WHERE tenant_id = %s AND new_item_id IN ({placeholders})
            """,
            (self._tenant_id, *sorted_affected),
        ).fetchall()
        old_ids = {str(row[0]) for row in rows if str(row[0]).strip()}
        restored: set[str] = set()
        now = _now_iso()
        for old_id in sorted(old_ids):
            active_replacement = self._conn.execute(
                f"""
                SELECT 1
                FROM memory_replacements r
                JOIN memory_items m ON m.id = r.new_item_id AND m.tenant_id = r.tenant_id
                WHERE r.tenant_id = %s
                  AND r.old_item_id = %s
                  AND r.new_item_id NOT IN ({placeholders})
                  AND m.status = 'active'
                LIMIT 1
                """,
                (self._tenant_id, old_id, *sorted_affected),
            ).fetchone()
            if active_replacement is not None:
                continue
            if dry_run:
                old_row = self._conn.execute(
                    "SELECT 1 FROM memory_items WHERE tenant_id=%s AND id=%s AND status='superseded'",
                    (self._tenant_id, old_id),
                ).fetchone()
                if old_row is not None:
                    restored.add(old_id)
                continue
            cur = self._conn.execute(
                "UPDATE memory_items SET status='active', updated_at=%s "
                "WHERE tenant_id=%s AND id=%s AND status='superseded'",
                (now, self._tenant_id, old_id),
            )
            if cur.rowcount:
                restored.add(old_id)
        return restored

    def record_replacements(
        self,
        *,
        old_items: list[dict[str, object]],
        new_item: dict[str, object],
        source_ref: str | None = None,
        relation_type: str = "supersede",
    ) -> int:
        if not old_items or not new_item or not new_item.get("id"):
            return 0
        now = _now_iso()
        rows = []
        for old_item in old_items:
            if not old_item or not old_item.get("id"):
                continue
            rows.append(
                (
                    self._tenant_id,
                    str(old_item.get("id")),
                    str(old_item.get("memory_type") or ""),
                    str(old_item.get("summary") or ""),
                    old_item.get("source_ref"),
                    old_item.get("happened_at"),
                    json.dumps(old_item.get("extra_json") or {}, ensure_ascii=False),
                    str(new_item.get("id")),
                    str(new_item.get("memory_type") or ""),
                    str(new_item.get("summary") or ""),
                    new_item.get("source_ref"),
                    new_item.get("happened_at"),
                    json.dumps(new_item.get("extra_json") or {}, ensure_ascii=False),
                    relation_type,
                    source_ref or new_item.get("source_ref"),
                    now,
                )
            )
        if not rows:
            return 0
        with self._lock, self._backend.connection():
            self._check_open()
            with self._conn.cursor() as cur:
                cur.executemany(
                    """INSERT INTO memory_replacements
                       (tenant_id, old_item_id, old_memory_type, old_summary,
                        old_source_ref, old_happened_at, old_extra_json,
                        new_item_id, new_memory_type, new_summary, new_source_ref,
                        new_happened_at, new_extra_json, relation_type, source_ref,
                        created_at)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                    rows,
                )
            self._conn.commit()
            return len(rows)

    def list_replacements(self) -> list[dict[str, object]]:
        with self._lock, self._backend.connection():
            self._check_open()
            rows = self._conn.execute(
                "SELECT old_item_id, old_memory_type, old_summary, old_source_ref, "
                "old_happened_at, old_extra_json, new_item_id, new_memory_type, "
                "new_summary, new_source_ref, new_happened_at, new_extra_json, "
                "relation_type, source_ref, created_at "
                "FROM memory_replacements WHERE tenant_id=%s ORDER BY id ASC",
                (self._tenant_id,),
            ).fetchall()
        result = []
        for row in rows:
            result.append(
                {
                    "old_item_id": row[0],
                    "old_memory_type": row[1],
                    "old_summary": row[2],
                    "old_source_ref": row[3],
                    "old_happened_at": _to_iso(row[4]),
                    "old_extra_json": json.loads(row[5]) if row[5] else {},
                    "new_item_id": row[6],
                    "new_memory_type": row[7],
                    "new_summary": row[8],
                    "new_source_ref": row[9],
                    "new_happened_at": _to_iso(row[10]),
                    "new_extra_json": json.loads(row[11]) if row[11] else {},
                    "relation_type": row[12],
                    "source_ref": row[13],
                    "created_at": _to_iso(row[14]),
                }
            )
        return result

    def reinforce_items_batch(self, ids: list[str], emotional_weight: int = 0) -> None:
        if not ids:
            return
        now = _now_iso()
        emotional_weight = _coerce_emotional_weight(emotional_weight)
        with self._lock, self._backend.connection():
            self._check_open()
            with self._conn.cursor() as cur:
                cur.executemany(
                    "UPDATE memory_items SET reinforcement=reinforcement+1, "
                    "updated_at=%s, "
                    "emotional_weight=GREATEST(emotional_weight, %s) "
                    "WHERE tenant_id=%s AND id=%s",
                    [(now, emotional_weight, self._tenant_id, item_id) for item_id in ids],
                )
            self._conn.commit()

    # ------------------------------------------------------------------
    # 读操作
    # ------------------------------------------------------------------

    def get_items_by_ids(self, ids: list[str]) -> list[dict[str, object]]:
        if not ids:
            return []
        placeholders = ",".join(["%s"] * len(ids))
        with self._lock, self._backend.connection():
            self._check_open()
            rows = self._conn.execute(
                _q(
                    "SELECT id, memory_type, summary, extra_json, source_ref, happened_at, "
                    "status, created_at, updated_at, emotional_weight "
                    f"FROM memory_items WHERE tenant_id=%s AND id IN ({placeholders})"
                ),
                (self._tenant_id, *ids),
            ).fetchall()
        by_id: dict[str, dict[str, object]] = {}
        for (
            row_id,
            memory_type,
            summary,
            extra_json,
            source_ref,
            happened_at,
            status,
            created_at,
            updated_at,
            emotional_weight,
        ) in rows:
            by_id[str(row_id)] = {
                "id": row_id,
                "memory_type": memory_type,
                "summary": summary,
                "extra_json": json.loads(extra_json) if extra_json else {},
                "source_ref": source_ref,
                "happened_at": _to_iso(happened_at),
                "status": status,
                "created_at": _to_iso(created_at),
                "updated_at": _to_iso(updated_at),
                "emotional_weight": emotional_weight,
            }
        return [by_id[item_id] for item_id in ids if item_id in by_id]

    def list_by_type(self, memory_type: str) -> list[dict[str, object]]:
        with self._lock, self._backend.connection():
            self._check_open()
            rows = self._conn.execute(
                "SELECT id, memory_type, summary, extra_json, happened_at, "
                "reinforcement, emotional_weight "
                "FROM memory_items WHERE tenant_id=%s AND memory_type=%s",
                (self._tenant_id, memory_type),
            ).fetchall()
        result = []
        for row_id, mtype, summary, extra_json, happened_at, reinforcement, emotional_weight in rows:
            result.append(
                {
                    "id": row_id,
                    "memory_type": mtype,
                    "summary": summary,
                    "extra_json": json.loads(extra_json) if extra_json else {},
                    "happened_at": _to_iso(happened_at),
                    "reinforcement": reinforcement,
                    "emotional_weight": emotional_weight,
                }
            )
        return result

    def list_events_by_time_range(
        self,
        time_start: datetime,
        time_end: datetime,
        limit: int = 200,
    ) -> list[dict[str, object]]:
        time_clauses, time_params = _pg_time_prefilter_clauses(
            "happened_at", time_start, time_end
        )
        with self._lock, self._backend.connection():
            self._check_open()
            rows = self._conn.execute(
                _q(
                    "SELECT id, memory_type, summary, source_ref, happened_at "
                    "FROM memory_items "
                    "WHERE tenant_id=%s AND memory_type='event' AND status='active' "
                    f"AND {' AND '.join(time_clauses)}"
                ),
                (self._tenant_id, *time_params),
            ).fetchall()

        hits: list[tuple[datetime, dict[str, object]]] = []
        for row_id, memory_type, summary, source_ref, happened_at in rows:
            parsed_time = _parse_memory_time(_to_iso(happened_at))
            if parsed_time is None:
                continue
            if parsed_time < time_start or parsed_time >= time_end:
                continue
            hits.append(
                (
                    parsed_time,
                    {
                        "id": row_id,
                        "memory_type": str(memory_type),
                        "summary": str(summary),
                        "source_ref": str(source_ref) if source_ref else "",
                        "happened_at": _to_iso(happened_at) or "",
                        "score": 1.0,
                    },
                )
            )

        max_items = max(1, min(limit, 200))
        hits.sort(key=lambda item: item[0], reverse=True)
        selected = hits[:max_items]
        selected.sort(key=lambda item: item[0])
        return [item for _, item in selected]

    def has_consolidation_source_ref(self, source_ref: str) -> bool:
        with self._lock, self._backend.connection():
            self._check_open()
            row = self._conn.execute(
                "SELECT 1 FROM consolidation_events "
                "WHERE tenant_id=%s AND source_ref=%s LIMIT 1",
                (self._tenant_id, (source_ref or "").strip()),
            ).fetchone()
        return row is not None

    def has_item_by_source_ref(
        self,
        source_ref: str,
        memory_type: str | None = None,
    ) -> bool:
        with self._lock, self._backend.connection():
            self._check_open()
            if memory_type:
                row = self._conn.execute(
                    "SELECT 1 FROM memory_items "
                    "WHERE tenant_id=%s AND source_ref=%s AND memory_type=%s LIMIT 1",
                    (self._tenant_id, source_ref, memory_type),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT 1 FROM memory_items "
                    "WHERE tenant_id=%s AND source_ref=%s LIMIT 1",
                    (self._tenant_id, source_ref),
                ).fetchone()
        return row is not None

    def get_all_with_embedding(
        self, include_superseded: bool = False
    ) -> list[_EmbeddingRow]:
        """返回 [(id, memory_type, summary, embedding_list, extra_json_dict, happened_at, source_ref)]

        extra_json_dict 中注入 _reinforcement / _updated_at / _emotional_weight
        （_ 前缀，不污染用户字段）。embedding 为 pgvector 列直读的 list[float]。
        """
        where = "" if include_superseded else "AND status='active'"
        with self._lock, self._backend.connection():
            self._check_open()
            rows = self._conn.execute(
                "SELECT id, memory_type, summary, embedding, extra_json, happened_at, "
                "reinforcement, updated_at, source_ref, emotional_weight "
                f"FROM memory_items WHERE tenant_id=%s AND embedding IS NOT NULL {where}",
                (self._tenant_id,),
            ).fetchall()
        result: list[_EmbeddingRow] = []
        for row in rows:
            (
                row_id,
                mtype,
                summary,
                emb,
                extra_json,
                happened_at,
                reinforcement,
                updated_at,
                source_ref,
                emotional_weight,
            ) = row
            extra = json.loads(extra_json) if extra_json else {}
            extra["_reinforcement"] = _coerce_int(reinforcement, 1)
            extra["_updated_at"] = _to_iso(updated_at) or ""
            extra["_emotional_weight"] = _coerce_emotional_weight(emotional_weight)
            result.append(
                (
                    str(row_id),
                    str(mtype),
                    str(summary),
                    _coerce_embedding(emb),
                    extra,
                    _to_iso(happened_at),
                    source_ref,
                )
            )
        return result

    def _get_embedding_rows_by_time_filter(
        self,
        *,
        memory_types: list[str] | None,
        include_superseded: bool,
        scope_channel: str | None,
        scope_chat_id: str | None,
        require_scope_match: bool,
        time_start: datetime | None,
        time_end: datetime | None,
    ) -> list[_EmbeddingRow]:
        where_parts = ["embedding IS NOT NULL"]
        params: list[object] = [self._tenant_id]
        if not include_superseded:
            where_parts.append("status='active'")
        if memory_types:
            placeholders = ",".join(["%s"] * len(memory_types))
            where_parts.append(f"memory_type IN ({placeholders})")
            params.extend(memory_types)
        if require_scope_match:
            where_parts.append(f"{_SCOPE_CHANNEL_SQL} = %s")
            where_parts.append(f"{_SCOPE_CHAT_SQL} = %s")
            params.extend([(scope_channel or "").strip(), (scope_chat_id or "").strip()])
        time_clauses, time_params = _pg_time_prefilter_clauses(
            "happened_at", time_start, time_end
        )
        where_parts.extend(time_clauses)
        params.extend(time_params)

        with self._lock, self._backend.connection():
            self._check_open()
            rows = self._conn.execute(
                _q(
                    "SELECT id, memory_type, summary, embedding, extra_json, happened_at, "
                    "reinforcement, updated_at, source_ref, emotional_weight "
                    f"FROM memory_items WHERE tenant_id=%s AND {' AND '.join(where_parts)}"
                ),
                tuple(params),
            ).fetchall()
        result: list[_EmbeddingRow] = []
        for row in rows:
            (
                row_id,
                mtype,
                summary,
                emb,
                extra_json,
                happened_at,
                reinforcement,
                updated_at,
                source_ref,
                emotional_weight,
            ) = row
            happened_at_iso = _to_iso(happened_at)
            if not _is_memory_time_in_range(happened_at_iso, time_start, time_end):
                continue
            extra = json.loads(extra_json) if extra_json else {}
            extra["_reinforcement"] = _coerce_int(reinforcement, 1)
            extra["_updated_at"] = _to_iso(updated_at) or ""
            extra["_emotional_weight"] = _coerce_emotional_weight(emotional_weight)
            result.append(
                (
                    str(row_id),
                    str(mtype),
                    str(summary),
                    _coerce_embedding(emb),
                    extra,
                    happened_at_iso,
                    source_ref,
                )
            )
        return result

    # ------------------------------------------------------------------
    # 检索
    # ------------------------------------------------------------------

    def vector_search(
        self,
        query_vec: list[float],
        top_k: int = 8,
        memory_types: list[str] | None = None,
        score_threshold: float = 0.0,
        include_superseded: bool = False,
        scope_channel: str | None = None,
        scope_chat_id: str | None = None,
        require_scope_match: bool = False,
        hotness_alpha: float = 0.0,
        hotness_half_life_days: float = 14.0,
        time_start: datetime | None = None,
        time_end: datetime | None = None,
    ) -> list[dict[str, object]]:
        if time_start is not None or time_end is not None:
            return self._vector_search_fullscan(
                query_vec,
                top_k=top_k,
                memory_types=memory_types,
                score_threshold=score_threshold,
                include_superseded=include_superseded,
                scope_channel=scope_channel,
                scope_chat_id=scope_chat_id,
                require_scope_match=require_scope_match,
                hotness_alpha=hotness_alpha,
                hotness_half_life_days=hotness_half_life_days,
                time_start=time_start,
                time_end=time_end,
            )
        return self._vector_search_vec(
            query_vec,
            top_k=top_k,
            memory_types=memory_types,
            score_threshold=score_threshold,
            include_superseded=include_superseded,
            scope_channel=scope_channel,
            scope_chat_id=scope_chat_id,
            require_scope_match=require_scope_match,
            hotness_alpha=hotness_alpha,
            hotness_half_life_days=hotness_half_life_days,
        )

    def vector_search_batch(
        self,
        query_vecs: list[list[float]],
        top_k: int = 8,
        memory_types: list[str] | None = None,
        score_threshold: float = 0.0,
        include_superseded: bool = False,
        scope_channel: str | None = None,
        scope_chat_id: str | None = None,
        require_scope_match: bool = False,
        hotness_alpha: float = 0.0,
        hotness_half_life_days: float = 14.0,
        time_start: datetime | None = None,
        time_end: datetime | None = None,
    ) -> list[list[dict[str, object]]]:
        if not query_vecs:
            return []
        if time_start is None and time_end is None:
            return [
                self.vector_search(
                    query_vec,
                    top_k=top_k,
                    memory_types=memory_types,
                    score_threshold=score_threshold,
                    include_superseded=include_superseded,
                    scope_channel=scope_channel,
                    scope_chat_id=scope_chat_id,
                    require_scope_match=require_scope_match,
                    hotness_alpha=hotness_alpha,
                    hotness_half_life_days=hotness_half_life_days,
                )
                for query_vec in query_vecs
            ]

        rows = self._get_embedding_rows_by_time_filter(
            memory_types=memory_types,
            include_superseded=include_superseded,
            scope_channel=scope_channel,
            scope_chat_id=scope_chat_id,
            require_scope_match=require_scope_match,
            time_start=time_start,
            time_end=time_end,
        )
        return [
            _score_embedding_rows(
                query_vec,
                rows,
                top_k=top_k,
                score_threshold=score_threshold,
                hotness_alpha=hotness_alpha,
                hotness_half_life_days=hotness_half_life_days,
            )
            for query_vec in query_vecs
        ]

    def _vector_search_vec(
        self,
        query_vec: list[float],
        top_k: int = 8,
        memory_types: list[str] | None = None,
        score_threshold: float = 0.0,
        include_superseded: bool = False,
        scope_channel: str | None = None,
        scope_chat_id: str | None = None,
        require_scope_match: bool = False,
        hotness_alpha: float = 0.0,
        hotness_half_life_days: float = 14.0,
    ) -> list[_MemoryHit]:
        """pgvector HNSW 检索路径（每分区索引，决策 B）。

        维度不符时回退全表扫描，与 SQLite 行为一致。
        """
        if len(query_vec) != self._vec_dim:
            logger.debug(
                "query dim %d ≠ vec_dim %d，回退全表扫描", len(query_vec), self._vec_dim
            )
            return self._vector_search_fullscan(
                query_vec,
                top_k=top_k,
                memory_types=memory_types,
                score_threshold=score_threshold,
                include_superseded=include_superseded,
                scope_channel=scope_channel,
                scope_chat_id=scope_chat_id,
                require_scope_match=require_scope_match,
                hotness_alpha=hotness_alpha,
                hotness_half_life_days=hotness_half_life_days,
            )

        # 多取候选以补偿 score_threshold 截断（同 SQLite fetch_k 逻辑）
        fetch_k = max(top_k * 2, 20)

        params: list[object] = [query_vec, self._tenant_id]

        status_filter = "" if include_superseded else "AND m.status = 'active'"

        if memory_types:
            placeholders = ",".join(["%s"] * len(memory_types))
            type_filter = f"AND m.memory_type IN ({placeholders})"
            params.extend(memory_types)
        else:
            type_filter = ""

        if require_scope_match:
            s_channel = (scope_channel or "").strip()
            s_chat = (scope_chat_id or "").strip()
            scope_filter = f"AND {_SCOPE_CHANNEL_SQL} = %s AND {_SCOPE_CHAT_SQL} = %s"
            params.extend([s_channel, s_chat])
        else:
            scope_filter = ""

        params.append(fetch_k)

        with self._lock, self._backend.connection():
            self._check_open()
            rows = self._conn.execute(
                _q(
                    f"""
                    SELECT m.id, m.memory_type, m.summary, m.extra_json, m.happened_at,
                           m.reinforcement, m.updated_at, m.source_ref, m.emotional_weight,
                           (m.embedding <=> %s::vector) AS distance
                    FROM memory_items m
                    WHERE m.tenant_id = %s AND m.embedding IS NOT NULL
                          {status_filter} {type_filter} {scope_filter}
                    ORDER BY distance ASC
                    LIMIT %s
                    """
                ),
                tuple(params),
            ).fetchall()

        now = datetime.now(timezone.utc)
        scored: list[_MemoryHit] = []
        for row in rows:
            (
                row_id,
                mtype,
                summary,
                extra_json,
                happened_at,
                reinforcement,
                updated_at_raw,
                source_ref,
                emotional_weight,
                distance,
            ) = row
            # pgvector <=> 返回 cosine distance：similarity = 1 - distance
            # （注意：与 sqlite-vec 的 L2 distance 转换不同）
            similarity = 1.0 - _coerce_float(distance)
            if similarity < score_threshold:
                continue

            extra = json.loads(extra_json) if extra_json else {}
            reinforcement_int = _coerce_int(reinforcement, 1)
            updated_at_str = _to_iso(updated_at_raw) or ""
            emotional_weight_int = _coerce_emotional_weight(emotional_weight)
            extra["_reinforcement"] = reinforcement_int
            extra["_updated_at"] = updated_at_str
            extra["_emotional_weight"] = emotional_weight_int

            hotness = 0.0
            if hotness_alpha > 0 and updated_at_str:
                try:
                    updated_at = datetime.fromisoformat(updated_at_str)
                    hotness = _hotness_score(
                        reinforcement_int,
                        updated_at,
                        now,
                        hotness_half_life_days,
                        emotional_weight=emotional_weight_int,
                    )
                except (ValueError, TypeError):
                    pass

            final = (1.0 - hotness_alpha) * similarity + hotness_alpha * hotness
            scored.append(
                {
                    "id": str(row_id),
                    "memory_type": str(mtype),
                    "summary": str(summary),
                    "extra_json": extra,
                    "happened_at": _to_iso(happened_at) or "",
                    "source_ref": str(source_ref) if source_ref else "",
                    "score": round(final, 4),
                    "_score_debug": {
                        "semantic": round(similarity, 4),
                        "hotness": round(hotness, 4),
                        "final": round(final, 4),
                    },
                }
            )

        scored.sort(key=_result_score, reverse=True)
        return scored[:top_k]

    def _vector_search_fullscan(
        self,
        query_vec: list[float],
        top_k: int = 8,
        memory_types: list[str] | None = None,
        score_threshold: float = 0.0,
        include_superseded: bool = False,
        scope_channel: str | None = None,
        scope_chat_id: str | None = None,
        require_scope_match: bool = False,
        hotness_alpha: float = 0.0,
        hotness_half_life_days: float = 14.0,
        time_start: datetime | None = None,
        time_end: datetime | None = None,
    ) -> list[_MemoryHit]:
        """全表扫描回退路径（时间过滤 / HNSW 不可用时使用）。"""
        has_time_filter = time_start is not None or time_end is not None
        if has_time_filter:
            rows = self._get_embedding_rows_by_time_filter(
                memory_types=memory_types,
                include_superseded=include_superseded,
                scope_channel=scope_channel,
                scope_chat_id=scope_chat_id,
                require_scope_match=require_scope_match,
                time_start=time_start,
                time_end=time_end,
            )
        else:
            rows = self.get_all_with_embedding(include_superseded=include_superseded)
        if not rows:
            return []

        if memory_types and not has_time_filter:
            rows = [r for r in rows if r[1] in memory_types]

        if require_scope_match and not has_time_filter:
            s_channel = (scope_channel or "").strip()
            s_chat = (scope_chat_id or "").strip()
            rows = [
                r
                for r in rows
                if str((r[4] or {}).get("scope_channel", "")).strip() == s_channel
                and str((r[4] or {}).get("scope_chat_id", "")).strip() == s_chat
            ]

        return _score_embedding_rows(
            query_vec,
            rows,
            top_k=top_k,
            score_threshold=score_threshold,
            hotness_alpha=hotness_alpha,
            hotness_half_life_days=hotness_half_life_days,
        )

    def find_similar_recent_events(
        self,
        embedding: list[float],
        *,
        days_back: int = 7,
        threshold: float = 0.92,
        top_k: int = 3,
    ) -> list[str]:
        cutoff = (
            datetime.now(timezone.utc) - timedelta(days=max(1, int(days_back)))
        )
        with self._lock, self._backend.connection():
            self._check_open()
            rows = self._conn.execute(
                "SELECT id, embedding FROM memory_items "
                "WHERE tenant_id=%s AND memory_type='event' AND status='active' "
                "AND embedding IS NOT NULL AND created_at >= %s",
                (self._tenant_id, cutoff),
            ).fetchall()
        scored: list[tuple[str, float]] = []
        for row_id, emb in rows:
            emb_list = _coerce_embedding(emb)
            if not emb_list:
                continue
            score = _cosine_similarity(embedding, emb_list)
            if score >= float(threshold):
                scored.append((row_id, score))
        scored.sort(key=lambda item: item[1], reverse=True)
        return [row_id for row_id, _score in scored[: max(1, int(top_k))]]

    def delete_by_source_ref(self, source_ref: str) -> int:
        with self._lock, self._backend.connection():
            self._check_open()
            cur = self._conn.execute(
                "DELETE FROM memory_items WHERE tenant_id=%s AND source_ref=%s",
                (self._tenant_id, source_ref),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def delete_item(self, item_id: str) -> bool:
        with self._lock, self._backend.connection():
            self._check_open()
            cur = self._conn.execute(
                "DELETE FROM memory_items WHERE tenant_id=%s AND id=%s",
                (self._tenant_id, item_id),
            )
            self._conn.commit()
            return cur.rowcount > 0

    def delete_items_batch(self, ids: list[str]) -> int:
        if not ids:
            return 0
        placeholders = ",".join(["%s"] * len(ids))
        with self._lock, self._backend.connection():
            self._check_open()
            cur = self._conn.execute(
                _q(
                    f"DELETE FROM memory_items WHERE tenant_id=%s AND id IN ({placeholders})"
                ),
                (self._tenant_id, *ids),
            )
            self._conn.commit()
            return int(cur.rowcount or 0)

    def keyword_match_procedures(
        self, action_tokens: list[str]
    ) -> list[dict[str, object]]:
        if not action_tokens:
            return []

        token_set = {t.lower() for t in action_tokens if t}
        action_text = " ".join(action_tokens).lower()

        with self._lock, self._backend.connection():
            self._check_open()
            rows = self._conn.execute(
                "SELECT id, summary, extra_json FROM memory_items "
                "WHERE tenant_id=%s AND memory_type='procedure' AND status='active' "
                "AND extra_json IS NOT NULL",
                (self._tenant_id,),
            ).fetchall()

        matched: list[dict[str, object]] = []
        for row_id, summary, extra_json_str in rows:
            try:
                extra = json.loads(extra_json_str) if extra_json_str else {}
            except Exception:
                continue
            tags = extra.get("trigger_tags") or {}
            if tags.get("scope") != "tool_triggered":
                continue

            keywords = [k for k in (tags.get("keywords") or []) if k and len(k) >= 3]

            if keywords:
                hit = any(kw.lower() in action_text for kw in keywords)
            else:
                proc_tools = tags.get("tools") or []
                proc_skills = tags.get("skills") or []
                if len(proc_tools) > 4:
                    continue
                tag_token_set = {t.lower() for t in proc_tools}
                tag_token_set |= {s.lower() for s in proc_skills}
                hit = bool(token_set & tag_token_set)

            if hit:
                matched.append(
                    {
                        "id": row_id,
                        "memory_type": "procedure",
                        "summary": summary,
                        "extra_json": extra,
                        "intercept": bool(tags.get("intercept", False)),
                        "score": 1.0,
                    }
                )

        return matched

    def keyword_search_summary(
        self,
        terms: list[str],
        memory_types: list[str] | None = None,
        limit: int = 20,
        time_start: datetime | None = None,
        time_end: datetime | None = None,
        scope_channel: str | None = None,
        scope_chat_id: str | None = None,
        require_scope_match: bool = False,
    ) -> list[dict[str, object]]:
        terms = [t for t in terms if t and len(t) >= 2]
        if not terms:
            return []

        type_filter = ""
        type_params: list[str] = []
        if memory_types:
            placeholders = ",".join(["%s"] * len(memory_types))
            type_filter = f" AND memory_type IN ({placeholders})"
            type_params = list(memory_types)

        scope_filter = ""
        scope_params: list[str] = []
        if require_scope_match:
            scope_filter = (
                f" AND {_SCOPE_CHANNEL_SQL} = %s"
                f" AND {_SCOPE_CHAT_SQL} = %s"
            )
            scope_params = [(scope_channel or "").strip(), (scope_chat_id or "").strip()]

        or_conditions = " OR ".join("summary ILIKE %s" for _ in terms)
        score_expr = " + ".join(
            "(CASE WHEN summary ILIKE %s THEN 1 ELSE 0 END)" for _ in terms
        )
        like_vals = [f"%{t}%" for t in terms]

        has_time_filter = time_start is not None or time_end is not None
        time_filter = ""
        time_params: list[object] = []
        if has_time_filter:
            time_clauses, time_params = _pg_time_prefilter_clauses(
                "happened_at", time_start, time_end
            )
            time_filter = " AND " + " AND ".join(time_clauses)
        batch_size = (
            max(limit, _TIME_FILTER_KEYWORD_CANDIDATE_LIMIT)
            if has_time_filter
            else limit
        )
        query_text = (
            "SELECT id, memory_type, summary, source_ref, happened_at, created_at, "
            f"reinforcement, ({score_expr}) AS kw_score "
            "FROM memory_items "
            f"WHERE tenant_id=%s AND status='active' AND ({or_conditions})"
            f"{type_filter}{scope_filter}{time_filter} "
            "ORDER BY kw_score DESC, reinforcement DESC, id ASC "
            "LIMIT %s OFFSET %s"
        )
        results: list[_MemoryHit] = []
        offset = 0
        while True:
            params: tuple[object, ...] = (
                tuple(like_vals)
                + (self._tenant_id,)
                + tuple(like_vals)
                + tuple(type_params)
                + tuple(scope_params)
                + tuple(time_params)
                + (batch_size, offset)
            )
            with self._lock, self._backend.connection():
                self._check_open()
                rows = self._conn.execute(_q(query_text), params).fetchall()
            if not rows:
                break
            for row in rows:
                (
                    row_id,
                    mtype,
                    summary,
                    source_ref,
                    happened_at,
                    created_at,
                    _reinforcement,
                    kw_score,
                ) = row
                if has_time_filter and not _is_memory_time_in_range(
                    _to_iso(happened_at), time_start, time_end
                ):
                    continue
                results.append(
                    {
                        "id": str(row_id),
                        "memory_type": str(mtype),
                        "summary": str(summary),
                        "source_ref": str(source_ref) if source_ref else "",
                        "happened_at": _to_iso(happened_at) or _to_iso(created_at) or "",
                        "keyword_score": _coerce_float(kw_score) / len(terms),
                    }
                )
                if len(results) >= limit:
                    return results
            if not has_time_filter or len(rows) < batch_size:
                break
            offset += batch_size
        return results

    def list_items_for_dashboard(
        self,
        *,
        q: str = "",
        memory_type: str = "",
        status: str = "",
        source_ref: str = "",
        scope_channel: str = "",
        scope_chat_id: str = "",
        has_embedding: bool | None = None,
        page: int = 1,
        page_size: int = 50,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[dict[str, object]], int]:
        with self._lock, self._backend.connection():
            self._check_open()
            safe_sort_by = (
                sort_by
                if sort_by
                in {
                    "updated_at",
                    "created_at",
                    "happened_at",
                    "reinforcement",
                    "emotional_weight",
                    "memory_type",
                }
                else "created_at"
            )
            safe_sort_order = "asc" if sort_order == "asc" else "desc"
            safe_page = max(1, page)
            safe_page_size = max(1, min(page_size, 200))
            offset = (safe_page - 1) * safe_page_size

            where_parts = ["1=1"]
            params: list[object] = [self._tenant_id]

            if q:
                where_parts.append(
                    "(id ILIKE %s OR summary ILIKE %s OR COALESCE(source_ref, '') ILIKE %s)"
                )
                like = f"%{q}%"
                params.extend([like, like, like])
            if memory_type:
                where_parts.append("memory_type = %s")
                params.append(memory_type)
            if status:
                where_parts.append("status = %s")
                params.append(status)
            if source_ref:
                where_parts.append("COALESCE(source_ref, '') ILIKE %s")
                params.append(f"%{source_ref}%")
            if scope_channel:
                where_parts.append(f"{_SCOPE_CHANNEL_SQL} = %s")
                params.append(scope_channel.strip())
            if scope_chat_id:
                where_parts.append(f"{_SCOPE_CHAT_SQL} = %s")
                params.append(scope_chat_id.strip())
            if has_embedding is True:
                where_parts.append("embedding IS NOT NULL")
            elif has_embedding is False:
                where_parts.append("embedding IS NULL")

            where_sql = " AND ".join(where_parts)
            total = int(
                self._conn.execute(
                    _q(
                        "SELECT COUNT(*) FROM memory_items "
                        f"WHERE tenant_id=%s AND {where_sql}"
                    ),
                    tuple(params),
                ).fetchone()[0]
            )
            rows = self._conn.execute(
                _q(
                    f"""
                    SELECT id, memory_type, summary, source_ref, happened_at, status,
                           created_at, updated_at, reinforcement, emotional_weight,
                           extra_json, embedding IS NOT NULL
                    FROM memory_items
                    WHERE tenant_id=%s AND {where_sql}
                    ORDER BY {safe_sort_by} {safe_sort_order}, id ASC
                    LIMIT %s OFFSET %s
                    """
                ),
                tuple([*params, safe_page_size, offset]),
            ).fetchall()
            items: list[dict[str, object]] = []
            for row in rows:
                (
                    row_id,
                    row_memory_type,
                    summary,
                    row_source_ref,
                    happened_at,
                    row_status,
                    created_at,
                    updated_at,
                    reinforcement,
                    emotional_weight,
                    extra_json,
                    row_has_embedding,
                ) = row
                extra = json.loads(extra_json) if extra_json else {}
                items.append(
                    {
                        "id": str(row_id),
                        "memory_type": row_memory_type,
                        "summary": summary,
                        "source_ref": row_source_ref,
                        "happened_at": _to_iso(happened_at),
                        "status": row_status,
                        "created_at": _to_iso(created_at),
                        "updated_at": _to_iso(updated_at),
                        "reinforcement": reinforcement,
                        "emotional_weight": emotional_weight,
                        "has_embedding": bool(row_has_embedding),
                        "scope_channel": extra.get("scope_channel", ""),
                        "scope_chat_id": extra.get("scope_chat_id", ""),
                    }
                )
            return items, total

    def get_item_for_dashboard(
        self,
        item_id: str,
        *,
        include_embedding: bool = False,
    ) -> dict[str, object] | None:
        with self._lock, self._backend.connection():
            self._check_open()
            row = self._conn.execute(
                "SELECT id, memory_type, summary, content_hash, embedding, "
                "reinforcement, emotional_weight, extra_json, source_ref, "
                "happened_at, status, created_at, updated_at "
                "FROM memory_items WHERE tenant_id=%s AND id=%s",
                (self._tenant_id, item_id),
            ).fetchone()
        if row is None:
            return None
        (
            row_id,
            memory_type,
            summary,
            content_hash,
            embedding,
            reinforcement,
            emotional_weight,
            extra_json,
            source_ref,
            happened_at,
            status,
            created_at,
            updated_at,
        ) = row
        emb = _coerce_embedding(embedding)
        return {
            "id": row_id,
            "memory_type": memory_type,
            "summary": summary,
            "content_hash": content_hash,
            "reinforcement": reinforcement,
            "emotional_weight": emotional_weight,
            "extra_json": json.loads(extra_json) if extra_json else {},
            "source_ref": source_ref,
            "happened_at": _to_iso(happened_at),
            "status": status,
            "created_at": _to_iso(created_at),
            "updated_at": _to_iso(updated_at),
            "has_embedding": emb is not None,
            "embedding_dim": len(emb) if emb is not None else 0,
            "embedding": emb if include_embedding else None,
        }

    def update_item_for_dashboard(
        self,
        item_id: str,
        *,
        status: str | None = None,
        extra_json: dict[str, object] | None = None,
        source_ref: str | None = None,
        happened_at: str | None = None,
        emotional_weight: int | None = None,
    ) -> dict[str, object] | None:
        with self._lock, self._backend.connection():
            self._check_open()
            updates: list[str] = []
            params: list[object] = []

            if status is not None:
                safe_status = status.strip()
                if safe_status not in {"active", "superseded"}:
                    raise ValueError("status 仅支持 active 或 superseded")
                updates.append("status=%s")
                params.append(safe_status)
            if extra_json is not None:
                updates.append("extra_json=%s")
                params.append(json.dumps(extra_json, ensure_ascii=False))
            if source_ref is not None:
                updates.append("source_ref=%s")
                params.append(source_ref)
            if happened_at is not None:
                updates.append("happened_at=%s")
                params.append(happened_at)
            if emotional_weight is not None:
                updates.append("emotional_weight=%s")
                params.append(_coerce_emotional_weight(emotional_weight))
            if not updates:
                return self.get_item_for_dashboard(item_id)

            updates.append("updated_at=%s")
            params.append(_now_iso())
            params.append(item_id)
            params.append(self._tenant_id)
            cur = self._conn.execute(
                _q(
                    "UPDATE memory_items SET "
                    + ", ".join(updates)
                    + " WHERE id=%s AND tenant_id=%s"
                ),
                params,
            )
            self._conn.commit()
            if cur.rowcount <= 0:
                return None
        return self.get_item_for_dashboard(item_id)

    def find_similar_items_for_dashboard(
        self,
        item_id: str,
        *,
        top_k: int = 8,
        memory_type: str = "",
        score_threshold: float = 0.0,
        include_superseded: bool = False,
    ) -> list[dict[str, object]]:
        base = self.get_item_for_dashboard(item_id, include_embedding=True)
        if base is None:
            raise KeyError(item_id)
        embedding = base.get("embedding")
        if not isinstance(embedding, list) or not embedding:
            raise ValueError("memory 没有 embedding")

        results = self.vector_search(
            query_vec=embedding,
            top_k=max(1, top_k) + 1,
            memory_types=[memory_type] if memory_type else None,
            score_threshold=score_threshold,
            include_superseded=include_superseded,
        )
        filtered = [item for item in results if item.get("id") != item_id]
        return filtered[: max(1, top_k)]
