"""SQLite 连接管理与 schema 初始化。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

# schema 与 schema/observe.sql 保持同步，在代码里内嵌一份避免运行时文件依赖
_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;

CREATE TABLE IF NOT EXISTS turns (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT    NOT NULL,
    source      TEXT    NOT NULL,
    session_key TEXT    NOT NULL,
    user_msg    TEXT,
    llm_output  TEXT    NOT NULL DEFAULT '',
    tool_calls  TEXT,
    error       TEXT
);
CREATE INDEX IF NOT EXISTS ix_turns_sk_ts  ON turns (session_key, ts);
CREATE INDEX IF NOT EXISTS ix_turns_source ON turns (source, ts);

CREATE TABLE IF NOT EXISTS rag_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  TEXT    NOT NULL,
    source              TEXT    NOT NULL,
    session_key         TEXT    NOT NULL,
    original_query      TEXT    NOT NULL,
    query               TEXT    NOT NULL,
    route_decision      TEXT,
    route_latency_ms    INTEGER,
    hyde_hypothesis     TEXT,
    history_scope_mode  TEXT,
    history_gate_reason TEXT,
    injected_block      TEXT,
    preference_block    TEXT,
    preference_query    TEXT,
    fallback_reason     TEXT,
    error               TEXT
);
CREATE INDEX IF NOT EXISTS ix_re_sk_ts  ON rag_events (session_key, ts);
CREATE INDEX IF NOT EXISTS ix_re_source ON rag_events (source, ts);

CREATE TABLE IF NOT EXISTS rag_items (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    rag_event_id    INTEGER NOT NULL REFERENCES rag_events (id),
    item_id         TEXT    NOT NULL,
    memory_type     TEXT    NOT NULL,
    score           REAL    NOT NULL,
    summary         TEXT    NOT NULL,
    happened_at     TEXT,
    extra_json      TEXT,
    retrieval_path  TEXT    NOT NULL,
    injected        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS ix_ri_event ON rag_items (rag_event_id);
CREATE INDEX IF NOT EXISTS ix_ri_item  ON rag_items (item_id);
"""


def open_db(db_path: Path) -> sqlite3.Connection:
    """打开（或新建）observe.db，初始化 schema，返回连接。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.executescript(_SCHEMA_SQL)
    conn.commit()
    return conn
