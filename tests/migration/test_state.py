"""4.1-4.7 S0-S4 状态机：持久化、gate、promote、dual-store、audit、回滚、PITR。"""
from __future__ import annotations

import json
import re
from pathlib import Path

import psycopg
import pytest

from scripts.migrate.config import MigrationConfig
from scripts.migrate.importer import run_import
from scripts.migrate.source import SESSIONS_DB, SqliteSource
from scripts.migrate.state import (
    CutoverState,
    S0,
    S1,
    S2,
    S3,
    StateError,
    load_state,
    promote_gate,
    record_verify_evidence,
    run_audit,
    run_import_cmd,
    run_pitr_rehearsal,
    run_promote,
    run_rollback,
    run_status,
    save_state,
    transition,
)
from scripts.migrate.verify import run_verify


# ── 4.1 状态持久化 + status ─────────────────────────────────────────────────

def test_status_initial_s0(make_cfg):
    report = run_status(make_cfg("st-initial"))
    assert report["state"] == S0
    assert not report["phases"]


def test_state_atomic_save_and_reload(make_cfg):
    cfg = make_cfg("st-save")
    state = CutoverState(current=S1)
    state.phase(S1)["entered_at"] = "2026-08-25T00:00:00Z"
    save_state(cfg.state_path, state)
    # 原子写：临时文件已替换，目录里不留 .tmp。
    assert not list(cfg.state_path.parent.glob(".state-*.tmp"))
    reloaded = load_state(cfg.state_path)
    assert reloaded.current == S1
    assert reloaded.phase(S1)["entered_at"] == "2026-08-25T00:00:00Z"


def test_state_transition_progression(make_cfg):
    cfg = make_cfg("st-trans")
    state = CutoverState()
    transition(state, S1, evidence=["/tmp/imp.json"], note="import")
    assert state.current == S1
    assert state.phase(S1)["enter_evidence"] == ["/tmp/imp.json"]
    transition(state, S2, evidence=["/tmp/pro.json"])
    assert state.current == S2
    assert state.phase(S1)["exited_at"] is not None
    # S0 → S2 非法。
    bad = CutoverState(current=S0)
    with pytest.raises(StateError):
        transition(bad, S2)


def test_import_enters_s1_and_persists(make_cfg, mig_pg_url, truncate_all):
    cfg = make_cfg("st-imp1")
    report = run_import_cmd(cfg)
    assert report["state"] == S1
    state = load_state(cfg.state_path)
    assert state.current == S1
    assert any(
        Path(e).exists() for e in state.phase(S1)["enter_evidence"]
    )


# ── 4.2 S1 进入/退出条件 gate ────────────────────────────────────────────────

def _import_to_s1(make_cfg):
    cfg = make_cfg("st-imp")
    run_import_cmd(cfg)
    return cfg


def test_promote_rejected_without_verify(make_cfg, mig_pg_url, truncate_all):
    cfg = _import_to_s1(make_cfg)
    with pytest.raises(StateError) as excinfo:
        run_promote(make_cfg("st-prom"))
    msg = str(excinfo.value)
    assert "校验" in msg
    assert any("校验" in m for m in excinfo.value.missing)
    # 状态未被推进。
    assert load_state(cfg.state_path).current == S1


def test_promote_rejected_when_verify_failed(make_cfg, mig_pg_url, truncate_all):
    cfg = _import_to_s1(make_cfg)
    conn = psycopg.connect(mig_pg_url)
    conn.execute("DELETE FROM messages WHERE id = "
                 "(SELECT id FROM messages ORDER BY id LIMIT 1)")
    conn.commit()
    conn.close()
    report = run_verify(make_cfg("st-ver-bad"))
    assert report["ok"] is False
    # 非全绿不记录退出条件证据。
    assert record_verify_evidence(make_cfg("st-ver-bad"), report) is None
    with pytest.raises(StateError) as excinfo:
        run_promote(make_cfg("st-prom-bad"))
    assert any("校验" in m for m in excinfo.value.missing)


def test_promote_passes_after_verify(make_cfg, mig_pg_url, truncate_all):
    cfg = _import_to_s1(make_cfg)
    vcfg = make_cfg("st-ver")
    vreport = run_verify(vcfg)
    assert vreport["ok"] is True
    assert record_verify_evidence(vcfg, vreport) == S1
    report = run_promote(make_cfg("st-prom"))
    assert report["ok"] is True
    assert report["state"] == S2
    assert load_state(cfg.state_path).current == S2


# ── 4.3 promote（config 翻转 + smoke + 对账）────────────────────────────────

def _import_verify_promote(make_cfg):
    cfg = _import_to_s1(make_cfg)
    vcfg = make_cfg("st-ver")
    record_verify_evidence(vcfg, run_verify(vcfg))
    pcfg = make_cfg("st-prom")
    report = run_promote(pcfg)
    return cfg, pcfg, report


def test_promote_config_flip_and_pg_writes(make_cfg, mig_pg_url, sample_ws,
                                           truncate_all):
    cfg, pcfg, report = _import_verify_promote(make_cfg)
    assert report["ok"] is True
    assert report["state"] == S2

    # config 翻转产物：backend=postgres。
    config_path = Path(report["config_flip"]["path"])
    assert config_path.exists()
    text = config_path.read_text(encoding="utf-8")
    assert 'backend = "postgres"' in text
    assert "postgres_url" in text

    # 新写入以 PG 为准：smoke session/messages 落到 PG。
    conn = psycopg.connect(mig_pg_url)
    sess = conn.execute("SELECT next_seq FROM sessions "
                        "WHERE key='smoke:1000'").fetchone()
    assert sess is not None and sess[0] == 2
    msgs = conn.execute("SELECT COUNT(*) FROM messages "
                        "WHERE session_key='smoke:1000'").fetchone()[0]
    assert msgs == 2
    mem = conn.execute("SELECT COUNT(*) FROM memory_items "
                       "WHERE source_ref='smoke:smoke:smoke'").fetchone()[0]
    assert mem == 1
    conn.close()

    # SQLite 源不被写入（PG primary，SQLite 仅审计副本）。
    src = SqliteSource(sample_ws)
    rows = src.query(SESSIONS_DB, "SELECT key FROM sessions WHERE key=?",
                     ["smoke:1000"])
    assert rows == []

    # 对账报告入库。
    ev = Path(report["evidence_path"])
    assert ev.exists()
    data = json.loads(ev.read_text(encoding="utf-8"))
    assert data["state"] == S2
    assert data["smoke"]["next_seq_readback"] == 2


def test_promote_turn_lands_in_sqlite_audit_copy(make_cfg, mig_pg_url,
                                                 sample_ws, truncate_all):
    cfg, pcfg, report = _import_verify_promote(make_cfg)
    turn = report["smoke"]
    assert turn["turn_readback"] is True
    from session.store import SessionStore

    audit = SessionStore(sample_ws / "turn_audit.db")
    try:
        record = audit.read_turn(turn["turn_id"])
        assert record is not None
        assert record.thread_id == turn["session_key"]
    finally:
        audit.close()


# ── 4.4 turn control dual-store 边界声明 ────────────────────────────────────

def test_promote_dual_store_declaration(make_cfg, mig_pg_url, truncate_all):
    cfg, pcfg, report = _import_verify_promote(make_cfg)
    dd = report["dual_store"]
    assert dd["primary"] == "postgres"
    assert dd["turn_control"] == "sqlite-audit-copy"
    assert "审计副本" in dd["declaration"]
    assert "S1" in dd["recovery_rollback"]


# ── 4.5 S2 → S3 audit ───────────────────────────────────────────────────────

def test_audit_to_s3(make_cfg, mig_pg_url, truncate_all):
    cfg, pcfg, report = _import_verify_promote(make_cfg)
    areport = run_audit(make_cfg("st-aud"))
    assert areport["state"] == S3
    assert areport["ok"] is True
    assert all(
        r["next_seq"] == 2 and r["message_count"] == 2 and r["turn_readback"]
        for r in areport["cycle_reconciliation"]
    )
    # shadow 停止 + SQLite 归档只读标记。
    assert areport["shadow"]["stopped"] is True
    archive = Path(areport["shadow"]["sqlite_archive_path"])
    assert archive.exists()
    # S3 证据入库。
    assert Path(areport["evidence_path"]).exists()
    state = load_state(pcfg.state_path)
    assert state.current == S3
    assert state.phase(S3)["entered_at"] is not None


def test_audit_requires_s2(make_cfg, mig_pg_url, truncate_all):
    cfg = _import_to_s1(make_cfg)
    with pytest.raises(StateError) as excinfo:
        run_audit(make_cfg("st-aud-x"))
    assert "S2" in str(excinfo.value)


# ── 4.6 回滚（S1 内回 SQLite；S2 起禁止）────────────────────────────────────

def test_rollback_s1_to_s0(make_cfg, mig_pg_url, truncate_all):
    cfg = _import_to_s1(make_cfg)
    r = run_rollback(make_cfg("st-rb"))
    assert r["rolled_back"] is True
    assert r["state"] == S0
    state = load_state(cfg.state_path)
    assert state.current == S0
    # S1 阶段记录被重置，可重新 import。
    assert "S1" not in state.phases
    report = run_import_cmd(make_cfg("st-reimp"))
    assert report["state"] == S1


def test_rollback_identity_at_s0(make_cfg, mig_pg_url, truncate_all):
    r = run_rollback(make_cfg("st-rb0"))
    assert r["rolled_back"] is False
    assert r["state"] == S0


def test_rollback_forbidden_after_s2(make_cfg, mig_pg_url, truncate_all):
    cfg, pcfg, report = _import_verify_promote(make_cfg)
    with pytest.raises(StateError) as excinfo:
        run_rollback(make_cfg("st-rb2"))
    msg = str(excinfo.value)
    assert "禁止" in msg and "PG" in msg
    assert "不可无损回切" in msg


# ── 4.7 PITR 恢复演练 + runbook ─────────────────────────────────────────────

def test_pitr_rehearsal_restores_point_in_time(make_cfg, mig_pg_url,
                                               truncate_all):
    cfg = _import_to_s1(make_cfg)
    report = run_pitr_rehearsal(make_cfg("st-pitr"))
    assert report["ok"] is True
    assert report["counts_match"] is True
    assert report["post_t0_marker_absent"] is True
    assert report["baseline_counts"] == report["restored_counts"]
    assert report["slo"]["rpo_max_seconds"] <= 300
    assert report["slo"]["rto_max_seconds"] <= 1800
    # runbook 存在并含真实恢复/回滚步骤与 SLO。
    rb = Path(report["runbook_path"])
    assert rb.exists()
    text = rb.read_text(encoding="utf-8")
    assert "pg_basebackup" in text
    assert "recovery_target_time" in text
    assert "RPO" in text and "RTO" in text
    # 证据入库。
    assert Path(report["evidence_path"]).exists()


def test_pitr_requires_import(make_cfg, mig_pg_url, truncate_all):
    with pytest.raises(StateError) as excinfo:
        run_pitr_rehearsal(make_cfg("st-pitr0"))
    assert "import" in str(excinfo.value)
