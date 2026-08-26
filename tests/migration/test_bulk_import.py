"""2.1 批量写入后端：COPY 路由 + provisioning 幂等 + 行级容错。"""
from __future__ import annotations

import psycopg
import pytest

from scripts.migrate.bulk import BulkPgWriter
from scripts.migrate.importer import run_import

from tests.migration.helpers import expected_counts, pg_counts, source_counts


def test_copy_routes_partitioned_and_plain(make_cfg, mig_pg_url, truncate_all):
    """memory_items COPY 落入 tenant_mem 分区；sessions/messages 进普通表。"""
    cfg = make_cfg("bulk-route")
    report = run_import(cfg)
    assert report["tables"]["memory_items"]["inserted"] == expected_counts(
        cfg.workspace
    )["memory_items"]
    conn = psycopg.connect(mig_pg_url)
    try:
        # memory_items 全部落在 tenant_mem 分区（唯一 memory 归属）。
        row = conn.execute(
            """
            SELECT c.relname, count(*)
            FROM memory_items mi
            JOIN pg_class c ON c.oid = mi.tableoid
            WHERE mi.tenant_id = 'tenant_mem'
            GROUP BY c.relname
            """
        ).fetchone()
        assert row is not None
        assert row[1] == expected_counts(cfg.workspace)["memory_items"]
        assert row[0].startswith("memory_items_tenant_mem_")
        # 非分区表直接计数。
        assert conn.execute(
            "SELECT COUNT(*) FROM sessions"
        ).fetchone()[0] == expected_counts(cfg.workspace)["sessions"]
        assert conn.execute(
            "SELECT COUNT(*) FROM messages"
        ).fetchone()[0] == expected_counts(cfg.workspace)["messages"]
    finally:
        conn.close()


def test_provision_partitions_idempotent(make_cfg, mig_pg_url, truncate_all):
    """同一 tenant 重复 provisioning 不产生重复分区。"""
    writer = BulkPgWriter(mig_pg_url)
    try:
        writer.provision_partitions(["tenant_mem"])
        writer.provision_partitions(["tenant_mem"])
    finally:
        writer.close()
    conn = psycopg.connect(mig_pg_url)
    try:
        n = conn.execute(
            """
            SELECT count(*) FROM pg_partition_tree('memory_items'::regclass) pt
            JOIN pg_class c ON c.oid = pt.relid
            WHERE pt.isleaf
              AND pg_get_expr(c.relpartbound, c.oid)
                  = 'FOR VALUES IN (' || quote_literal('tenant_mem') || ')'
            """
        ).fetchone()[0]
        assert n == 1
    finally:
        conn.close()


def test_required_invalid_row_skipped(make_cfg, mig_pg_url, truncate_all, sample_ws):
    """ts 无法解析的消息行被记 skipped_invalid，不中断整表导入。"""
    import sqlite3

    conn = sqlite3.connect(str(sample_ws / "sessions.db"))
    conn.execute(
        "INSERT INTO messages (id, session_key, seq, role, content, ts)"
        " VALUES ('m-bad-ts', ?, 9999, 'user', 'x', 'not-a-timestamp')",
        ("telegram:1000",),
    )
    conn.commit()
    conn.close()

    cfg = make_cfg("bulk-invalid")
    report = run_import(cfg)
    msgs = report["tables"]["messages"]
    assert msgs["skipped_invalid"] == 1
    assert pg_counts(mig_pg_url)["messages"] == source_counts(cfg.workspace)[
        "messages"
    ] - 1
