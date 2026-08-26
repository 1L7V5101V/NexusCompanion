"""migration 测试共享工具：源/目标计数与预期行数。"""
from __future__ import annotations

from pathlib import Path

import psycopg
from psycopg.sql import Identifier, SQL

from scripts.migrate.importer import TABLE_SPECS, _iter_spec_rows
from scripts.migrate.source import SqliteSource

MIGRATION_TABLES = [
    "tick_step_log", "tick_log", "context_only_timestamps", "session_state",
    "deliveries", "memory_replacements", "consolidation_events",
    "memory_items", "messages", "sessions", "scheduled_jobs", "app_configs",
]


def truncate_tables(url: str) -> None:
    conn = psycopg.connect(url, autocommit=True)
    try:
        for table in MIGRATION_TABLES:
            conn.execute(SQL("TRUNCATE TABLE {}").format(Identifier(table)))
    finally:
        conn.close()


def pg_counts(url: str) -> dict[str, int]:
    conn = psycopg.connect(url)
    try:
        return {
            t: int(conn.execute(
                SQL("SELECT COUNT(*) FROM {}").format(Identifier(t))
            ).fetchone()[0])
            for t in MIGRATION_TABLES
        }
    finally:
        conn.close()


def source_counts(ws: Path) -> dict[str, int]:
    source = SqliteSource(ws)
    return {spec.name: len(_iter_spec_rows(source, spec)) for spec in TABLE_SPECS}


def expected_counts(ws: Path) -> dict[str, int]:
    """预期入库行数：源行数减去 required 列缺失被跳过的行（与 importer 同逻辑）。"""
    source = SqliteSource(ws)
    out: dict[str, int] = {}
    for spec in TABLE_SPECS:
        n = 0
        for row in _iter_spec_rows(source, spec):
            transformed = (
                spec.transform(row, "dummy") if spec.transform else dict(row)
            )
            if any(transformed.get(c) is None for c in spec.required):
                continue
            n += 1
        out[spec.name] = n
    return out
