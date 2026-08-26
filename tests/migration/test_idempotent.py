"""2.3 幂等重跑：保留源主键 + ON CONFLICT DO NOTHING，无重复。"""
from __future__ import annotations

from scripts.migrate.importer import run_import

from tests.migration.helpers import expected_counts, pg_counts


def test_rerun_same_source_no_duplicates(make_cfg, mig_pg_url, truncate_all):
    cfg1 = make_cfg("idem-1")
    r1 = run_import(cfg1)
    after_first = pg_counts(mig_pg_url)

    cfg2 = make_cfg("idem-2")  # 新 run_id：无 checkpoint，走 ON CONFLICT
    r2 = run_import(cfg2)
    after_second = pg_counts(mig_pg_url)

    assert after_second == after_first
    expected = expected_counts(cfg1.workspace)
    for table, n in expected.items():
        assert after_first[table] == n
    # 第二次实际插入 0，全部被冲突跳过。
    assert sum(r2["tables"][t]["inserted"] for t in expected) == 0
    assert sum(r2["tables"][t]["skipped_conflict"] for t in expected) == sum(
        expected.values()
    )
    # 主键保留：PG 主键与源一致（抽查 messages id 集合）。
    src_ids = _source_ids(cfg1.workspace)
    conn_ids = _pg_ids(mig_pg_url)
    assert src_ids == conn_ids


def _source_ids(ws):
    import sqlite3

    conn = sqlite3.connect(str(ws / "sessions.db"))
    ids = {r[0] for r in conn.execute("SELECT id FROM messages")}
    conn.close()
    return ids


def _pg_ids(url):
    import psycopg

    conn = psycopg.connect(url)
    ids = {r[0] for r in conn.execute("SELECT id FROM messages")}
    conn.close()
    return ids
