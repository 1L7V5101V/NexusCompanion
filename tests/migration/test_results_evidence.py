"""2.6 import 机器可读结果入库：字段完整 + 可复现。"""
from __future__ import annotations

import json

from scripts.migrate.importer import run_import

from tests.migration.helpers import (
    expected_counts,
    pg_counts,
    truncate_tables,
)


def _result_file(cfg):
    return cfg.results_dir / f"{cfg.run_id}-import.json"


def test_import_writes_results_json(make_cfg, mig_pg_url, truncate_all):
    cfg = make_cfg("results")
    report = run_import(cfg)
    path = _result_file(cfg)
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ok"] is True
    assert data["meta"]["run_id"] == cfg.run_id
    assert "elapsed_sec" in data
    assert set(data["plan"]) >= {
        "source_identities", "mapping", "tenants", "tables",
    }
    assert set(data["tables"]) >= {
        "sessions", "messages", "memory_items", "memory_replacements",
        "tick_log", "scheduled_jobs", "app_configs",
    }
    # 返回的 report 与落盘一致。
    assert data["tables"] == report["tables"]


def test_reimport_reproducible(make_cfg, mig_pg_url, truncate_all):
    """同一源同一 mapping 两次干净导入，逐表 inserted 完全一致（确定性）。"""
    c1 = make_cfg("rep-1")
    r1 = run_import(c1)
    pg_counts(mig_pg_url)
    truncate_tables(mig_pg_url)
    c2 = make_cfg("rep-2")
    r2 = run_import(c2)
    for t in expected_counts(c1.workspace):
        assert r1["tables"][t]["inserted"] == r2["tables"][t]["inserted"], t
