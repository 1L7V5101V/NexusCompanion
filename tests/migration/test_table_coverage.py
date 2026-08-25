"""2.5 覆盖全部目标表：按 FK 顺序导入，逐表行数与源一致。"""
from __future__ import annotations

from scripts.migrate.importer import TABLE_SPECS, run_import

from tests.migration.helpers import expected_counts, pg_counts


def test_all_tables_parity(make_cfg, mig_pg_url, truncate_all):
    cfg = make_cfg("coverage")
    report = run_import(cfg)
    expected = expected_counts(cfg.workspace)
    actual = pg_counts(mig_pg_url)
    for spec in TABLE_SPECS:
        assert actual[spec.name] == expected[spec.name], (
            f"{spec.name}: PG {actual[spec.name]} != 源 {expected[spec.name]}"
        )
        assert report["tables"][spec.name]["inserted"] == expected[spec.name]


def test_fk_order_sessions_before_messages(make_cfg, mig_pg_url, truncate_all):
    """FK 顺序：messages.session_key 必须命中 sessions（无孤儿）。"""
    cfg = make_cfg("fk-order")
    run_import(cfg)
    conn = __import__("psycopg").connect(mig_pg_url)
    try:
        orphans = conn.execute(
            "SELECT COUNT(*) FROM messages m"
            " LEFT JOIN sessions s ON s.key = m.session_key"
            " AND s.tenant_id = m.tenant_id"
            " WHERE s.key IS NULL"
        ).fetchone()[0]
        assert orphans == 0
        # memory_replacements 外键命中 memory_items（跨表）。
        bad = conn.execute(
            "SELECT COUNT(*) FROM memory_replacements r"
            " LEFT JOIN memory_items mi ON mi.id = r.old_item_id"
            " AND mi.tenant_id = r.tenant_id"
            " WHERE mi.id IS NULL"
        ).fetchone()[0]
        assert bad == 0
    finally:
        conn.close()
