"""
PostgresSessionStore：PostgreSQL 版的 SessionStore 后端（M3）

与 SQLite 版（session/store.py）保持接口与返回类型一致：
- 所有操作按 tenant_id 作用域（单库多租户，决策 C）；sessions/messages 主键
  为 (tenant_id, key) / (tenant_id, id)（迁移 b6e9d2c4a8f1）
- 时间列（created_at / updated_at / ts / last_*_at）在 PG 为 TIMESTAMPTZ，
  对外仍返回 ISO 字符串，与 SQLite 返回类型一致
- next_seq 用 UPDATE ... RETURNING 原子自增（契约 3 节：PG 用 RETURNING）
- search_messages 用 pg_trgm（GIN 索引加速 ILIKE '%x%'），对标 SQLite FTS5
  trigram；bm25 排序由「命中词数 DESC + seq DESC」近似（中文分词语义见
  docs/tasks/phase1-storage/phase1-storage.md M3）
"""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Any, LiteralString, cast

import psycopg
from psycopg.rows import dict_row

from infra.storage.pool import PostgresPool
from memory2.store import _now_iso


def _to_iso(value: object) -> str | None:
    """TIMESTAMPTZ -> ISO 字符串（保持与 SQLite 返回 str|None 一致）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _parse_iso(value: str | None) -> datetime | None:
    """调用方传入的 ISO 字符串 -> datetime（供 timestamptz 列比较）。"""
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _q(text: str) -> LiteralString:
    """标记运行期拼接的 SQL 文本为可信 LiteralString。

    仅用于占位符数量/allowlist 片段拼接的查询文本；不把用户输入拼进 SQL 文本。
    """
    return cast(LiteralString, text)


class PostgresSessionBackend:
    """resource-owner：持有连接池 + RLock（M4H-2 C / M4H-3）。

    跨 tenant 共享；tenant-bound view（PostgresSessionStore(backend, tenant_id)）
    只委托 pool 借出的连接与锁，不拥有 backend。底层生命周期只归
    StorageRuntime.close()。
    """

    def __init__(
        self,
        postgres_url: str,
        *,
        pool_size: int = 1,
        timeout: float = 5.0,
        max_waiting: int = 20,
    ) -> None:
        if postgres_url.startswith("postgresql+psycopg://"):
            postgres_url = postgres_url.replace(
                "postgresql+psycopg://", "postgresql://", 1
            )
        self._url = postgres_url
        self._lock = threading.RLock()
        self._closed = False
        # pool 是连接唯一所有者；dict_row 让 fetchone 返回 dict
        self._pool = PostgresPool(
            postgres_url,
            min_size=min(1, pool_size),
            max_size=pool_size,
            timeout=timeout,
            max_waiting=max_waiting,
            kwargs={"row_factory": dict_row},
            name="session",
        )
        # 当前线程借出的连接（view 方法体经 connection() 借出/归还）
        self._local = threading.local()

    @property
    def conn(self) -> psycopg.Connection[dict[str, Any]]:
        """当前线程借出的连接；未借出时访问属编程错误。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            raise RuntimeError(
                "PostgresSessionBackend: no connection borrowed "
                "(missing `with self._backend.connection()`)"
            )
        return cast(psycopg.Connection[dict[str, Any]], conn)

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
                "PostgresSessionBackend: pool is closed"
            )

    def close(self) -> None:
        if self._closed:
            return
        try:
            self._pool.close()
        finally:
            self._closed = True


class PostgresSessionStore:
    def __init__(
        self,
        postgres_url: str | PostgresSessionBackend,
        tenant_id: str = "default",
    ) -> None:
        # 传入 PostgresSessionBackend = tenant-bound view：不建连接、不拥有 backend；
        # 传入 URL = owned 构造（factory/tests 直连，构造即开一条连接）。
        if isinstance(postgres_url, PostgresSessionBackend):
            self._backend = postgres_url
            self._owns_backend = False
        else:
            self._backend = PostgresSessionBackend(postgres_url)
            self._owns_backend = True
        self._tenant_id = tenant_id

    @property
    def _conn(self) -> psycopg.Connection[dict[str, Any]]:
        return self._backend.conn

    @property
    def _lock(self) -> threading.RLock:
        return self._backend._lock

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
    # 会话生命周期
    # ------------------------------------------------------------------

    def session_exists(self, key: str) -> bool:
        with self._lock, self._backend.connection():
            self._check_open()
            row = self._conn.execute(
                "SELECT 1 FROM sessions WHERE tenant_id = %s AND key = %s",
                (self._tenant_id, key),
            ).fetchone()
        return row is not None

    def upsert_session(
        self,
        key: str,
        *,
        created_at: str,
        updated_at: str,
        last_consolidated: int,
        metadata: dict[str, Any],
    ) -> None:
        payload = json.dumps(metadata or {}, ensure_ascii=False)
        with self._lock, self._backend.connection():
            self._check_open()
            self._conn.execute(
                """
                INSERT INTO sessions (
                    tenant_id, key, channel, chat_id, created_at, updated_at,
                    last_consolidated, metadata_json
                )
                VALUES (%s, %s, '', '', %s, %s, %s, %s)
                ON CONFLICT (tenant_id, key) DO UPDATE SET
                    updated_at = EXCLUDED.updated_at,
                    last_consolidated = EXCLUDED.last_consolidated,
                    metadata_json = EXCLUDED.metadata_json
                """,
                (self._tenant_id, key, created_at, updated_at, int(last_consolidated), payload),
            )
            self._conn.commit()

    def update_last_consolidated(self, key: str, last_consolidated: int) -> None:
        with self._lock, self._backend.connection():
            self._check_open()
            self._conn.execute(
                """
                UPDATE sessions
                SET last_consolidated = %s, updated_at = %s
                WHERE tenant_id = %s AND key = %s
                """,
                (int(last_consolidated), _now_iso(), self._tenant_id, key),
            )
            self._conn.commit()

    def get_session_meta(self, key: str) -> dict[str, Any] | None:
        with self._lock, self._backend.connection():
            self._check_open()
            row = self._conn.execute(
                """
                SELECT key, created_at, updated_at, last_consolidated, metadata_json,
                       last_user_at, last_proactive_at
                FROM sessions
                WHERE tenant_id = %s AND key = %s
                """,
                (self._tenant_id, key),
            ).fetchone()
        if row is None:
            return None
        return {
            "key": row["key"],
            "created_at": _to_iso(row["created_at"]),
            "updated_at": _to_iso(row["updated_at"]),
            "last_consolidated": int(row["last_consolidated"] or 0),
            "metadata": json.loads(row["metadata_json"] or "{}"),
            "last_user_at": _to_iso(row["last_user_at"]),
            "last_proactive_at": _to_iso(row["last_proactive_at"]),
        }

    def list_sessions(self) -> list[dict[str, Any]]:
        with self._lock, self._backend.connection():
            self._check_open()
            rows = self._conn.execute(
                """
                SELECT key, created_at, updated_at, last_user_at, last_proactive_at
                FROM sessions
                WHERE tenant_id = %s
                ORDER BY updated_at DESC
                """,
                (self._tenant_id,),
            ).fetchall()
        return [
            {
                "key": str(row["key"]),
                "created_at": _to_iso(row["created_at"]),
                "updated_at": _to_iso(row["updated_at"]),
                "last_user_at": _to_iso(row["last_user_at"]),
                "last_proactive_at": _to_iso(row["last_proactive_at"]),
            }
            for row in rows
        ]

    def list_sessions_for_dashboard(
        self,
        *,
        q: str = "",
        channel: str = "",
        updated_from: str = "",
        updated_to: str = "",
        has_proactive: bool | None = None,
        page: int = 1,
        page_size: int = 50,
        sort_by: str = "updated_at",
        sort_order: str = "desc",
    ) -> tuple[list[dict[str, Any]], int]:
        safe_page = max(1, int(page))
        safe_page_size = max(1, min(int(page_size), 200))
        offset = (safe_page - 1) * safe_page_size
        safe_sort_by = (
            sort_by
            if sort_by
            in {
                "updated_at",
                "created_at",
                "last_user_at",
                "last_proactive_at",
            }
            else "updated_at"
        )
        safe_sort_order = "ASC" if str(sort_order).lower() == "asc" else "DESC"

        params: list[Any] = [self._tenant_id]
        where_parts: list[str] = ["s.tenant_id = %s"]
        query = (q or "").strip()
        if query:
            where_parts.append(
                "(s.key LIKE %s OR COALESCE(s.metadata_json, '') LIKE %s)"
            )
            like = f"%{query}%"
            params.extend([like, like])
        if channel:
            where_parts.append("s.key LIKE %s")
            params.append(f"{channel}:%")
        if updated_from:
            where_parts.append("s.updated_at >= %s")
            params.append(_parse_iso(updated_from))
        if updated_to:
            where_parts.append("s.updated_at <= %s")
            params.append(_parse_iso(updated_to))
        if has_proactive is True:
            where_parts.append("s.last_proactive_at IS NOT NULL")
        if has_proactive is False:
            where_parts.append("s.last_proactive_at IS NULL")

        where_sql = "WHERE " + " AND ".join(where_parts)
        count_sql = f"SELECT COUNT(1) AS c FROM sessions s {where_sql}"
        data_sql = f"""
            SELECT
                s.key,
                s.created_at,
                s.updated_at,
                s.last_consolidated,
                s.metadata_json,
                s.last_user_at,
                s.last_proactive_at,
                COALESCE(msg.message_count, 0) AS message_count
            FROM sessions s
            LEFT JOIN (
                SELECT session_key, COUNT(1) AS message_count
                FROM messages
                GROUP BY session_key
            ) msg ON msg.session_key = s.key
            {where_sql}
            ORDER BY s.{safe_sort_by} {safe_sort_order}, s.key ASC
            LIMIT %s OFFSET %s
        """
        with self._lock, self._backend.connection():
            self._check_open()
            count_row = self._conn.execute(_q(count_sql), tuple(params)).fetchone()
            rows = self._conn.execute(
                _q(data_sql),
                tuple([*params, safe_page_size, offset]),
            ).fetchall()
        total = int((count_row["c"] if count_row else 0) or 0)
        return [
            {
                "key": str(row["key"]),
                "created_at": _to_iso(row["created_at"]),
                "updated_at": _to_iso(row["updated_at"]),
                "last_consolidated": int(row["last_consolidated"] or 0),
                "metadata": json.loads(row["metadata_json"] or "{}"),
                "last_user_at": _to_iso(row["last_user_at"]),
                "last_proactive_at": _to_iso(row["last_proactive_at"]),
                "message_count": int(row["message_count"] or 0),
            }
            for row in rows
        ], total

    def create_session(
        self,
        *,
        key: str,
        metadata: dict[str, Any] | None = None,
        last_consolidated: int = 0,
        last_user_at: str | None = None,
        last_proactive_at: str | None = None,
    ) -> dict[str, Any]:
        now = _now_iso()
        payload = json.dumps(metadata or {}, ensure_ascii=False)
        with self._lock, self._backend.connection():
            self._check_open()
            self._conn.execute(
                """
                INSERT INTO sessions (
                    tenant_id, key, channel, chat_id, created_at, updated_at,
                    last_consolidated, metadata_json, last_user_at, last_proactive_at
                )
                VALUES (%s, %s, '', '', %s, %s, %s, %s, %s, %s)
                """,
                (
                    self._tenant_id,
                    key,
                    now,
                    now,
                    int(last_consolidated),
                    payload,
                    last_user_at,
                    last_proactive_at,
                ),
            )
            self._conn.commit()
        meta = self.get_session_meta(key)
        if meta is None:
            raise ValueError(f"session 创建失败: {key}")
        return meta

    def update_session(
        self,
        key: str,
        *,
        metadata: dict[str, Any] | None = None,
        last_consolidated: int | None = None,
        last_user_at: str | None = None,
        last_proactive_at: str | None = None,
    ) -> dict[str, Any] | None:
        set_parts = ["updated_at = %s"]
        params: list[Any] = [_now_iso()]
        if metadata is not None:
            set_parts.append("metadata_json = %s")
            params.append(json.dumps(metadata, ensure_ascii=False))
        if last_consolidated is not None:
            set_parts.append("last_consolidated = %s")
            params.append(int(last_consolidated))
        if last_user_at is not None:
            set_parts.append("last_user_at = %s")
            params.append(last_user_at)
        if last_proactive_at is not None:
            set_parts.append("last_proactive_at = %s")
            params.append(last_proactive_at)
        params.extend([self._tenant_id, key])
        with self._lock, self._backend.connection():
            self._check_open()
            cur = self._conn.execute(
                _q(
                    f"UPDATE sessions SET {', '.join(set_parts)} "
                    "WHERE tenant_id = %s AND key = %s"
                ),
                tuple(params),
            )
            self._conn.commit()
        if cur.rowcount <= 0:
            return None
        return self.get_session_meta(key)

    def delete_session(self, key: str, *, cascade: bool = False) -> bool:
        with self._lock, self._backend.connection():
            self._check_open()
            if not cascade:
                row = self._conn.execute(
                    "SELECT COUNT(1) AS c FROM messages "
                    "WHERE tenant_id = %s AND session_key = %s",
                    (self._tenant_id, key),
                ).fetchone()
                count = int((row["c"] if row else 0) or 0)
                if count > 0:
                    raise ValueError("session 下仍有 messages，需使用 cascade 删除")
            else:
                self._conn.execute(
                    "DELETE FROM messages WHERE tenant_id = %s AND session_key = %s",
                    (self._tenant_id, key),
                )
            cur = self._conn.execute(
                "DELETE FROM sessions WHERE tenant_id = %s AND key = %s",
                (self._tenant_id, key),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def delete_sessions_batch(self, keys: list[str], *, cascade: bool = False) -> int:
        clean_keys = [str(key).strip() for key in keys if str(key).strip()]
        if not clean_keys:
            return 0
        placeholders = ",".join("%s" for _ in clean_keys)
        with self._lock, self._backend.connection():
            self._check_open()
            if not cascade:
                row = self._conn.execute(
                    f"SELECT COUNT(1) AS c FROM messages "
                    f"WHERE tenant_id = %s AND session_key IN ({placeholders})",
                    tuple([self._tenant_id, *clean_keys]),
                ).fetchone()
                count = int((row["c"] if row else 0) or 0)
                if count > 0:
                    raise ValueError(
                        "选中的 session 中仍有 messages，需使用 cascade 删除"
                    )
            else:
                self._conn.execute(
                    f"DELETE FROM messages "
                    f"WHERE tenant_id = %s AND session_key IN ({placeholders})",
                    tuple([self._tenant_id, *clean_keys]),
                )
            cur = self._conn.execute(
                f"DELETE FROM sessions "
                f"WHERE tenant_id = %s AND key IN ({placeholders})",
                tuple([self._tenant_id, *clean_keys]),
            )
            self._conn.commit()
        return int(cur.rowcount or 0)

    # ------------------------------------------------------------------
    # 会话状态（presence / channel）
    # ------------------------------------------------------------------

    def update_presence(
        self,
        key: str,
        *,
        last_user_at: str | None = None,
        last_proactive_at: str | None = None,
    ) -> None:
        now = _now_iso()
        with self._lock, self._backend.connection():
            self._check_open()
            self._conn.execute(
                """
                INSERT INTO sessions (
                    tenant_id, key, channel, chat_id, created_at, updated_at,
                    last_consolidated, metadata_json, last_user_at, last_proactive_at
                )
                VALUES (%s, %s, '', '', %s, %s, 0, '{}', %s, %s)
                ON CONFLICT (tenant_id, key) DO UPDATE SET
                    updated_at = EXCLUDED.updated_at,
                    last_user_at = COALESCE(
                        EXCLUDED.last_user_at, sessions.last_user_at
                    ),
                    last_proactive_at = COALESCE(
                        EXCLUDED.last_proactive_at, sessions.last_proactive_at
                    )
                """,
                (self._tenant_id, key, now, now, last_user_at, last_proactive_at),
            )
            self._conn.commit()

    def get_presence(self, key: str) -> dict[str, str | None] | None:
        with self._lock, self._backend.connection():
            self._check_open()
            row = self._conn.execute(
                """
                SELECT last_user_at, last_proactive_at
                FROM sessions
                WHERE tenant_id = %s AND key = %s
                """,
                (self._tenant_id, key),
            ).fetchone()
        if row is None:
            return None
        return {
            "last_user_at": _to_iso(row["last_user_at"]),
            "last_proactive_at": _to_iso(row["last_proactive_at"]),
        }

    def list_presence(self) -> dict[str, dict[str, str | None]]:
        with self._lock, self._backend.connection():
            self._check_open()
            rows = self._conn.execute(
                """
                SELECT key, last_user_at, last_proactive_at
                FROM sessions
                WHERE tenant_id = %s
                  AND (last_user_at IS NOT NULL OR last_proactive_at IS NOT NULL)
                """,
                (self._tenant_id,),
            ).fetchall()
        return {
            str(row["key"]): {
                "last_user_at": _to_iso(row["last_user_at"]),
                "last_proactive_at": _to_iso(row["last_proactive_at"]),
            }
            for row in rows
        }

    def most_recent_user_at(self) -> str | None:
        with self._lock, self._backend.connection():
            self._check_open()
            row = self._conn.execute(
                """
                SELECT MAX(last_user_at) AS last_user_at
                FROM sessions
                WHERE tenant_id = %s AND last_user_at IS NOT NULL
                """,
                (self._tenant_id,),
            ).fetchone()
        if row is None:
            return None
        return _to_iso(row["last_user_at"])

    def get_channel_metadata(self, channel: str) -> list[dict[str, Any]]:
        like_key = f"{channel}:%"
        with self._lock, self._backend.connection():
            self._check_open()
            rows = self._conn.execute(
                "SELECT key, metadata_json FROM sessions "
                "WHERE tenant_id = %s AND key LIKE %s",
                (self._tenant_id, like_key),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            key = str(row["key"])
            chat_id = key.split(":", 1)[-1] if ":" in key else key
            results.append(
                {
                    "key": key,
                    "chat_id": chat_id,
                    "metadata": json.loads(row["metadata_json"] or "{}"),
                }
            )
        return results

    # ------------------------------------------------------------------
    # 消息
    # ------------------------------------------------------------------

    def count_messages(self, session_key: str) -> int:
        with self._lock, self._backend.connection():
            self._check_open()
            row = self._conn.execute(
                "SELECT COUNT(1) AS c FROM messages "
                "WHERE tenant_id = %s AND session_key = %s",
                (self._tenant_id, session_key),
            ).fetchone()
        return int((row["c"] if row else 0) or 0)

    def next_seq(self, session_key: str) -> int:
        """取下一 seq（非消费）：与 SQLite 基线一致，返回 max(stored, max(seq)+1)。

        不采用 SEQUENCE 消费式自增：peek_next_message_id 会无副作用地预测
        下一条消息 id，消费式会把 seq 烧掉造成空洞；唯一性由 insert_message
        的原子 max 自增（UPDATE ... CASE WHEN）+ UNIQUE(tenant_id, session_key,
        seq) 保证，单连接 + RLock 下不存在竞态。
        """
        with self._lock, self._backend.connection():
            self._check_open()
            meta = self._conn.execute(
                "SELECT next_seq FROM sessions "
                "WHERE tenant_id = %s AND key = %s",
                (self._tenant_id, session_key),
            ).fetchone()
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq) + 1, 0) AS seq FROM messages "
                "WHERE tenant_id = %s AND session_key = %s",
                (self._tenant_id, session_key),
            ).fetchone()
        from_messages = int(row["seq"])
        if meta is None:
            return from_messages
        return max(int(meta["next_seq"] or 0), from_messages)

    def insert_message(
        self,
        session_key: str,
        *,
        role: str,
        content: str,
        ts: str,
        seq: int,
        tool_chain: Any | None = None,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        message_id = f"{session_key}:{seq}"
        tool_chain_payload = (
            json.dumps(tool_chain, ensure_ascii=False)
            if tool_chain is not None
            else None
        )
        extra_payload = json.dumps(extra or {}, ensure_ascii=False)
        with self._lock, self._backend.connection():
            self._check_open()
            self._conn.execute(
                """
                INSERT INTO messages (
                    tenant_id, id, session_key, seq, role, content, tool_chain,
                    extra, ts
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    self._tenant_id,
                    message_id,
                    session_key,
                    seq,
                    role,
                    content,
                    tool_chain_payload,
                    extra_payload,
                    ts,
                ),
            )
            self._conn.execute(
                """
                UPDATE sessions
                SET next_seq = CASE WHEN next_seq < %s THEN %s ELSE next_seq END,
                    updated_at = %s
                WHERE tenant_id = %s AND key = %s
                """,
                (int(seq) + 1, int(seq) + 1, _now_iso(), self._tenant_id, session_key),
            )
            self._conn.commit()
        row = {
            "id": message_id,
            "session_key": session_key,
            "seq": seq,
            "role": role,
            "content": content,
            "timestamp": ts,
        }
        if tool_chain is not None:
            row["tool_chain"] = tool_chain
        if extra:
            row.update(extra)
        return row

    def fetch_session_messages(self, session_key: str) -> list[dict[str, Any]]:
        with self._lock, self._backend.connection():
            self._check_open()
            rows = self._conn.execute(
                """
                SELECT id, session_key, seq, role, content, tool_chain, extra, ts
                FROM messages
                WHERE tenant_id = %s AND session_key = %s
                ORDER BY seq ASC
                """,
                (self._tenant_id, session_key),
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def list_messages_for_dashboard(
        self,
        *,
        session_key: str | None = None,
        q: str = "",
        role: str = "",
        page: int = 1,
        page_size: int = 25,
        sort_by: str = "ts",
        sort_order: str = "desc",
    ) -> tuple[list[dict[str, Any]], int]:
        safe_page = max(1, int(page))
        safe_page_size = max(1, min(int(page_size), 200))
        offset = (safe_page - 1) * safe_page_size
        safe_sort = "ASC" if str(sort_order).lower() == "asc" else "DESC"
        safe_sort_by = (
            sort_by if sort_by in {"ts", "seq", "role", "session_key"} else "ts"
        )

        params: list[Any] = [self._tenant_id]
        where_parts: list[str] = ["tenant_id = %s"]
        if session_key:
            where_parts.append("session_key = %s")
            params.append(session_key)
        term = (q or "").strip()
        if term:
            where_parts.append("content LIKE %s")
            params.append(f"%{term}%")
        if role:
            where_parts.append("role = %s")
            params.append(role)
        where_sql = "WHERE " + " AND ".join(where_parts)

        count_sql = f"SELECT COUNT(1) AS c FROM messages {where_sql}"
        data_sql = f"""
            SELECT id, session_key, seq, role, content, tool_chain, extra, ts
            FROM messages
            {where_sql}
            ORDER BY {safe_sort_by} {safe_sort}, seq {safe_sort}, id ASC
            LIMIT %s OFFSET %s
        """
        with self._lock, self._backend.connection():
            self._check_open()
            count_row = self._conn.execute(_q(count_sql), tuple(params)).fetchone()
            rows = self._conn.execute(
                _q(data_sql),
                tuple([*params, safe_page_size, offset]),
            ).fetchall()
        total = int((count_row["c"] if count_row else 0) or 0)
        return [self._row_to_message(row) for row in rows], total

    def get_message(self, message_id: str) -> dict[str, Any] | None:
        with self._lock, self._backend.connection():
            self._check_open()
            row = self._conn.execute(
                """
                SELECT id, session_key, seq, role, content, tool_chain, extra, ts
                FROM messages
                WHERE tenant_id = %s AND id = %s
                """,
                (self._tenant_id, message_id),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_message(row)

    def update_message(
        self,
        message_id: str,
        *,
        role: str | None = None,
        content: str | None = None,
        tool_chain: Any | None = None,
        extra: dict[str, Any] | None = None,
        ts: str | None = None,
    ) -> dict[str, Any] | None:
        set_parts: list[str] = []
        params: list[Any] = []
        if role is not None:
            set_parts.append("role = %s")
            params.append(role)
        if content is not None:
            set_parts.append("content = %s")
            params.append(content)
        if tool_chain is not None:
            set_parts.append("tool_chain = %s")
            params.append(json.dumps(tool_chain, ensure_ascii=False))
        if extra is not None:
            set_parts.append("extra = %s")
            params.append(json.dumps(extra, ensure_ascii=False))
        if ts is not None:
            set_parts.append("ts = %s")
            params.append(ts)
        if not set_parts:
            return self.get_message(message_id)

        with self._lock, self._backend.connection():
            self._check_open()
            row = self._conn.execute(
                "SELECT session_key FROM messages "
                "WHERE tenant_id = %s AND id = %s",
                (self._tenant_id, message_id),
            ).fetchone()
            if row is None:
                return None
            session_key = str(row["session_key"])
            params.extend([self._tenant_id, message_id])
            cur = self._conn.execute(
                _q(
                    f"UPDATE messages SET {', '.join(set_parts)} "
                    "WHERE tenant_id = %s AND id = %s"
                ),
                tuple(params),
            )
            self._conn.execute(
                "UPDATE sessions SET updated_at = %s "
                "WHERE tenant_id = %s AND key = %s",
                (_now_iso(), self._tenant_id, session_key),
            )
            self._conn.commit()
        if cur.rowcount <= 0:
            return None
        return self.get_message(message_id)

    def delete_message(self, message_id: str) -> bool:
        with self._lock, self._backend.connection():
            self._check_open()
            row = self._conn.execute(
                "SELECT session_key FROM messages "
                "WHERE tenant_id = %s AND id = %s",
                (self._tenant_id, message_id),
            ).fetchone()
            if row is None:
                return False
            session_key = str(row["session_key"])
            cur = self._conn.execute(
                "DELETE FROM messages WHERE tenant_id = %s AND id = %s",
                (self._tenant_id, message_id),
            )
            self._conn.execute(
                "UPDATE sessions SET updated_at = %s "
                "WHERE tenant_id = %s AND key = %s",
                (_now_iso(), self._tenant_id, session_key),
            )
            self._conn.commit()
        return cur.rowcount > 0

    def delete_messages_batch(self, ids: list[str]) -> int:
        clean_ids = [
            str(message_id).strip() for message_id in ids if str(message_id).strip()
        ]
        if not clean_ids:
            return 0
        placeholders = ",".join("%s" for _ in clean_ids)
        now = _now_iso()
        with self._lock, self._backend.connection():
            self._check_open()
            rows = self._conn.execute(
                f"SELECT DISTINCT session_key FROM messages "
                f"WHERE tenant_id = %s AND id IN ({placeholders})",
                tuple([self._tenant_id, *clean_ids]),
            ).fetchall()
            cur = self._conn.execute(
                f"DELETE FROM messages WHERE tenant_id = %s AND id IN ({placeholders})",
                tuple([self._tenant_id, *clean_ids]),
            )
            for row in rows:
                self._conn.execute(
                    "UPDATE sessions SET updated_at = %s "
                    "WHERE tenant_id = %s AND key = %s",
                    (now, self._tenant_id, str(row["session_key"])),
                )
            self._conn.commit()
        return int(cur.rowcount or 0)

    def delete_session_messages_and_update_cursor(
        self,
        session_key: str,
        *,
        ids: list[str],
        last_consolidated: int,
    ) -> int:
        clean_ids = [
            str(message_id).strip() for message_id in ids if str(message_id).strip()
        ]
        if not clean_ids:
            return 0
        placeholders = ",".join("%s" for _ in clean_ids)
        now = _now_iso()
        with self._lock, self._backend.connection():
            self._check_open()
            try:
                seq_rows = self._conn.execute(
                    f"""
                    SELECT seq
                    FROM messages
                    WHERE tenant_id = %s AND session_key = %s
                      AND id IN ({placeholders})
                    """,
                    tuple([self._tenant_id, session_key, *clean_ids]),
                ).fetchall()
                next_seq = (
                    max(int(row["seq"]) for row in seq_rows) + 1 if seq_rows else 0
                )
                cur = self._conn.execute(
                    f"""
                    DELETE FROM messages
                    WHERE tenant_id = %s AND session_key = %s
                      AND id IN ({placeholders})
                    """,
                    tuple([self._tenant_id, session_key, *clean_ids]),
                )
                self._conn.execute(
                    """
                    UPDATE sessions
                    SET last_consolidated = %s,
                        updated_at = %s,
                        next_seq = CASE WHEN next_seq < %s THEN %s ELSE next_seq END
                    WHERE tenant_id = %s AND key = %s
                    """,
                    (
                        int(last_consolidated),
                        now,
                        next_seq,
                        next_seq,
                        self._tenant_id,
                        session_key,
                    ),
                )
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        return int(cur.rowcount or 0)

    def fetch_by_ids_with_context(
        self, ids: list[str], context: int
    ) -> list[dict[str, Any]]:
        """Fetch messages by ID, expanding each hit by ±context rows in its session."""
        if not ids:
            return []
        if context == 0:
            result = self.fetch_by_ids(ids)
            for m in result:
                m["in_source_ref"] = True
            return result

        id_set = set(ids)
        session_seqs: dict[str, set[int]] = {}
        for msg_id in ids:
            parts = msg_id.rsplit(":", 1)
            if len(parts) != 2:
                continue
            sk, seq_str = parts
            try:
                seq = int(seq_str)
            except ValueError:
                continue
            if sk not in session_seqs:
                session_seqs[sk] = set()
            session_seqs[sk].add(seq)

        if not session_seqs:
            return []

        results: list[dict[str, Any]] = []
        with self._lock, self._backend.connection():
            self._check_open()
            for sk, seqs in session_seqs.items():
                expanded: set[int] = set()
                for seq in seqs:
                    for s in range(max(0, seq - context), seq + context + 1):
                        expanded.add(s)
                placeholders = ",".join("%s" for _ in expanded)
                rows = self._conn.execute(
                    f"SELECT id, session_key, seq, role, content, tool_chain, extra, ts "
                    f"FROM messages WHERE tenant_id = %s AND session_key = %s "
                    f"AND seq IN ({placeholders}) ORDER BY seq",
                    [self._tenant_id, sk, *expanded],
                ).fetchall()
                for row in rows:
                    msg = self._row_to_message(row)
                    msg["in_source_ref"] = msg["id"] in id_set
                    results.append(msg)
        return results

    def fetch_by_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        if not ids:
            return []
        placeholders = ",".join("%s" for _ in ids)
        order_expr = " ".join(f"WHEN %s THEN {i}" for i in range(len(ids)))
        sql = (
            "SELECT id, session_key, seq, role, content, tool_chain, extra, ts "
            f"FROM messages WHERE tenant_id = %s AND id IN ({placeholders}) "
            f"ORDER BY CASE id {order_expr} END"
        )
        with self._lock, self._backend.connection():
            self._check_open()
            rows = self._conn.execute(
                _q(sql), tuple([self._tenant_id, *ids, *ids])
            ).fetchall()
        return [self._row_to_message(row) for row in rows]

    def search_messages(
        self,
        query: str,
        *,
        session_key: str | None = None,
        role: str | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        limit = max(1, min(int(limit), 100))
        offset = max(0, int(offset))
        params: list[Any] = [self._tenant_id]
        where_parts: list[str] = ["m.tenant_id = %s"]
        if session_key:
            where_parts.append("m.session_key = %s")
            params.append(session_key)
        if role:
            where_parts.append("m.role = %s")
            params.append(role)
        where_sql = "WHERE " + " AND ".join(where_parts)

        terms = [t for t in query.split() if t]
        if not terms:
            terms = [query]
        # 与 SQLite 基线一致：不做长度过滤，短词同样走子串匹配

        term_conditions_or = " OR ".join("m.content ILIKE %s" for _ in terms)
        score_expr = " + ".join(
            f"(CASE WHEN m.content ILIKE %s THEN 1 ELSE 0 END)" for _ in terms
        )
        # ILIKE 子串条件各绑定一个 %term%，score_expr（SELECT 列，先出现）
        # 各绑定一个 %term%；按 SQL 文本位置顺序排列参数
        count_params = [*params, *(f"%{t}%" for t in terms)]
        data_params = [
            *(f"%{t}%" for t in terms),
            *params,
            *(f"%{t}%" for t in terms),
            limit,
            offset,
        ]
        count_sql = (
            f"SELECT COUNT(1) AS c FROM messages m "
            f"{where_sql} AND ({term_conditions_or})"
        )
        data_sql = (
            f"SELECT m.id, m.session_key, m.seq, m.role, m.content, "
            f"m.tool_chain, m.extra, m.ts, ({score_expr}) AS match_score "
            f"FROM messages m "
            f"{where_sql} AND ({term_conditions_or}) "
            f"ORDER BY match_score DESC, m.seq DESC LIMIT %s OFFSET %s"
        )
        with self._lock, self._backend.connection():
            self._check_open()
            count_row = self._conn.execute(_q(count_sql), tuple(count_params)).fetchone()
            rows = self._conn.execute(_q(data_sql), tuple(data_params)).fetchall()
        total = int((count_row["c"] if count_row else 0) or 0)
        return [self._row_to_message(row) for row in rows], total

    def _row_to_message(self, row: dict[str, Any]) -> dict[str, Any]:
        message: dict[str, Any] = {
            "id": row["id"],
            "session_key": row["session_key"],
            "seq": int(row["seq"]),
            "role": row["role"],
            "content": row["content"] or "",
            "timestamp": _to_iso(row["ts"]),
        }
        tool_chain = row["tool_chain"]
        if tool_chain:
            message["tool_chain"] = json.loads(tool_chain)
        extra = json.loads(row["extra"] or "{}")
        if extra:
            message.update(extra)
        return message
