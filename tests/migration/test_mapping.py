"""2.4 显式 tenant mapping 强制：缺失/未映射预检终止、零写入；租户隔离。"""
from __future__ import annotations

import json

import psycopg
import pytest

from scripts.migrate.importer import run_import
from scripts.migrate.mapping import MappingError

from tests.migration.helpers import pg_counts


def test_missing_mapping_rejected(make_cfg, mig_pg_url, truncate_all):
    cfg = make_cfg("no-mapping", mapping_path=None)
    with pytest.raises(MappingError, match="必须提供 tenant mapping"):
        run_import(cfg)
    counts = pg_counts(mig_pg_url)
    assert all(v == 0 for v in counts.values()), counts


def test_unmapped_identity_rejected(make_cfg, mig_pg_url, truncate_all, sample_ws):
    partial = {
        "telegram": "tenant_a",
        "discord": "tenant_a",
        "whatsapp": "tenant_b",
        "wechat": "tenant_b",
        # slack 未映射
        "memory": "tenant_mem",
    }
    (sample_ws / "mapping.json").write_text(
        json.dumps(partial), encoding="utf-8"
    )
    cfg = make_cfg("unmapped")
    with pytest.raises(MappingError, match="slack"):
        run_import(cfg)
    counts = pg_counts(mig_pg_url)
    assert all(v == 0 for v in counts.values()), counts


def test_tenant_isolation(make_cfg, mig_pg_url, truncate_all):
    cfg = make_cfg("isolation")
    report = run_import(cfg)
    # 各 tenant 行数不为 0，说明 mapping 生效。
    tenants = set()
    for st in report["tables"].values():
        tenants.update(st["tenants"].keys())
    assert {"tenant_a", "tenant_b", "tenant_c", "tenant_mem"} <= tenants

    conn = psycopg.connect(mig_pg_url)
    try:
        # messages 中 tenant_a 的 session_key 只能来自 telegram/discord 通道。
        rows = conn.execute(
            "SELECT DISTINCT session_key FROM messages WHERE tenant_id='tenant_a'"
        ).fetchall()
        assert rows
        for (sk,) in rows:
            assert sk.split(":", 1)[0] in {"telegram", "discord"}, sk
        # memory_items 全部归属 tenant_mem。
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_items WHERE tenant_id != 'tenant_mem'"
        ).fetchone()[0] == 0
    finally:
        conn.close()


def test_dry_run_zero_write(make_cfg, mig_pg_url, truncate_all):
    cfg = make_cfg("dry-run")
    report = run_import(cfg, dry_run=True)
    assert report["result"] == "preflight_ok"
    assert report["plan"]["tenants"] == sorted(
        ["tenant_a", "tenant_b", "tenant_c", "tenant_mem"]
    )
    counts = pg_counts(mig_pg_url)
    assert all(v == 0 for v in counts.values())
