"""SQLite 源读取：只读打开 workspace 各 DB，按行迭代 + 计数。

时间戳统一规范化为 UTC ISO 8601（naive 视为 UTC），保证 COPY 写入确定性、
校验时源/目标逐值可比（见 verify.canon_ts）。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

# workspace 内各源 DB 的相对路径。
SESSIONS_DB = "sessions.db"
MEMORY_DB = "memory/memory2.db"
PROACTIVE_DB = "proactive.db"


def channel_of(session_key: str) -> str:
    """从 session key 派生源通道身份（``channel:chat_id`` 形式取前缀）。"""
    key = session_key or ""
    return key.split(":", 1)[0] if ":" in key else key


def canon_ts(value: Any) -> str | None:
    """把源时间值规范化为 UTC ISO 字符串；naive 视为 UTC。

    None / 空 / 无法解析 → None（PG 可空列）。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


class SqliteSource:
    """只读访问一个 workspace 的 SQLite 源。连接按需打开、用完即关。"""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace)

    def _connect(self, db: str) -> sqlite3.Connection:
        path = self.workspace / db
        if not path.exists():
            raise FileNotFoundError(f"源 DB 不存在: {path}")
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn

    def table_exists(self, db: str, table: str) -> bool:
        if not (self.workspace / db).exists():
            return False
        conn = self._connect(db)
        try:
            row = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            return row is not None
        finally:
            conn.close()

    def count(self, db: str, table: str) -> int:
        conn = self._connect(db)
        try:
            row = conn.execute(f'SELECT COUNT(*) AS c FROM "{table}"').fetchone()
            return int(row["c"])
        finally:
            conn.close()

    def iter_rows(
        self, db: str, table: str, *, order_by: list[str] | None = None
    ) -> Iterator[dict[str, Any]]:
        """按序迭代源表行（每行为 dict）。order_by 支持逗号分隔列名。"""
        conn = self._connect(db)
        try:
            order = f" ORDER BY {', '.join(order_by)}" if order_by else ""
            cur = conn.execute(f'SELECT * FROM "{table}"{order}')
            for row in cur:
                yield {k: row[k] for k in row.keys()}
        finally:
            conn.close()

    def query(self, db: str, sql: str, params: list[Any] | None = None
              ) -> list[dict[str, Any]]:
        """只读执行一条 SQL，返回行 dict 列表（校验/抽样用）。"""
        conn = self._connect(db)
        try:
            cur = conn.execute(sql, params or [])
            return [{k: row[k] for k in row.keys()} for row in cur]
        finally:
            conn.close()
