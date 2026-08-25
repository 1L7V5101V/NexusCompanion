"""3.1-3.4 机器可读校验：对齐通过 / 四维差异均被检出并报非零退出码。"""
from __future__ import annotations

import json
from pathlib import Path

import psycopg

from scripts.migrate.importer import run_import
from scripts.migrate.verify import run_verify


def _import_and_verify(make_cfg, mig_pg_url):
    cfg = make_cfg("v-imp")
    run_import(cfg)
    vcfg = make_cfg("v-run")
    return run_verify(vcfg)


def test_verify_passes_on_aligned(make_cfg, mig_pg_url, truncate_all):
    report = _import_and_verify(make_cfg, mig_pg_url)
    assert report["ok"] is True
    assert report["exit_code"] == 0
    for dim, d in report["dimensions"].items():
        assert d["ok"] is True, dim
    # 证据入库且字段完整。
    path = Path(report["evidence_path"])
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["ok"] is True
    assert set(data["dimensions"]) >= {
        "row_count", "field_hash", "referential_integrity", "semantic_sampling",
    }
    assert data["dimensions"]["field_hash"]["tables"]["messages"]["match"] is True
    assert data["dimensions"]["semantic_sampling"]["vector_topk"]["ok"] is True
    assert data["dimensions"]["semantic_sampling"]["session_next_seq"]["ok"] is True


def test_row_count_diff_detected(make_cfg, mig_pg_url, truncate_all):
    cfg = make_cfg("v-imp")
    run_import(cfg)
    conn = psycopg.connect(mig_pg_url)
    conn.execute("DELETE FROM messages WHERE id = "
                 "(SELECT id FROM messages ORDER BY id LIMIT 1)")
    conn.commit()
    conn.close()
    report = run_verify(make_cfg("v-run"))
    assert report["ok"] is False
    assert report["exit_code"] == 1
    rc = report["dimensions"]["row_count"]
    assert rc["ok"] is False
    assert rc["tables"]["messages"]["match"] is False


def test_field_hash_diff_detected(make_cfg, mig_pg_url, truncate_all):
    cfg = make_cfg("v-imp")
    run_import(cfg)
    conn = psycopg.connect(mig_pg_url)
    conn.execute("UPDATE messages SET content = 'tampered' WHERE id = "
                 "(SELECT id FROM messages ORDER BY id LIMIT 1)")
    conn.commit()
    conn.close()
    report = run_verify(make_cfg("v-run"))
    assert report["ok"] is False
    assert report["exit_code"] == 1
    fh = report["dimensions"]["field_hash"]
    assert fh["ok"] is False
    assert fh["tables"]["messages"]["match"] is False
    assert fh["tables"]["sessions"]["match"] is True


def test_orphan_reference_detected(make_cfg, mig_pg_url, truncate_all):
    cfg = make_cfg("v-imp")
    run_import(cfg)
    conn = psycopg.connect(mig_pg_url)
    # 删除一条有消息的 session → messages.session_key 变孤儿引用。
    conn.execute("DELETE FROM sessions WHERE key = "
                 "(SELECT session_key FROM messages "
                 " WHERE session_key IS NOT NULL LIMIT 1)")
    conn.commit()
    conn.close()
    report = run_verify(make_cfg("v-run"))
    assert report["ok"] is False
    assert report["exit_code"] == 1
    ref = report["dimensions"]["referential_integrity"]
    assert ref["ok"] is False
    assert ref["checks"]["messages.session_key"]["match"] is False
    assert ref["checks"]["messages.session_key"]["orphans"] >= 1


def test_semantic_next_seq_diff_detected(make_cfg, mig_pg_url, truncate_all):
    cfg = make_cfg("v-imp")
    run_import(cfg)
    conn = psycopg.connect(mig_pg_url)
    conn.execute("UPDATE sessions SET next_seq = next_seq + 100 WHERE key = "
                 "(SELECT key FROM sessions ORDER BY key LIMIT 1)")
    conn.commit()
    conn.close()
    report = run_verify(make_cfg("v-run"))
    assert report["ok"] is False
    assert report["exit_code"] == 1
    sem = report["dimensions"]["semantic_sampling"]
    assert sem["ok"] is False
    assert sem["session_next_seq"]["ok"] is False
    assert len(sem["session_next_seq"]["mismatches"]) == 1


def test_vector_topk_diff_detected(make_cfg, mig_pg_url, truncate_all):
    cfg = make_cfg("v-imp")
    run_import(cfg)
    conn = psycopg.connect(mig_pg_url)
    far = "[" + ",".join(["1.0"] * 1024) + "]"
    conn.execute(
        "UPDATE memory_items SET embedding = %s::vector "
        "WHERE tenant_id='tenant_mem' AND id='mem00000'",
        (far,),
    )
    conn.commit()
    conn.close()
    report = run_verify(make_cfg("v-run"))
    assert report["ok"] is False
    assert report["exit_code"] == 1
    sem = report["dimensions"]["semantic_sampling"]
    assert sem["vector_topk"]["ok"] is False
    assert sem["vector_topk"]["results"][0]["match"] is False
