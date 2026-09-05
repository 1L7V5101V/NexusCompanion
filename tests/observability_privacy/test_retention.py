"""C12 retention sweep 契约测试（ADR-5，task-12 验收第 3 条）。

三档 TTL 生效、幂等重跑零删除、dry-run 零副作用、目录缺失降级、失败逐条记录。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from core.telemetry.retention import (
    RetentionCategory,
    RetentionPolicy,
    prune_empty_dirs,
    sweep_roots,
)

NOW = 1_700_000_000.0
DAY = 86400.0


def _touch(path: Path, mtime: float) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("payload", encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def _sample_roots(tmp_path: Path) -> dict[str, list[Path]]:
    op_root = tmp_path / "operational"
    audit_root = tmp_path / "audit"
    debug_root = tmp_path / "debug"
    _touch(op_root / "old.log", NOW - 40 * DAY)
    _touch(op_root / "new.log", NOW - 1 * DAY)
    _touch(audit_root / "recent.jsonl", NOW - 100 * DAY)
    _touch(audit_root / "ancient.jsonl", NOW - 200 * DAY)
    _touch(debug_root / "capture.bin", NOW - 8 * DAY)
    return {
        RetentionCategory.OPERATIONAL: [op_root],
        RetentionCategory.AUDIT: [audit_root],
        RetentionCategory.DEBUG_CONTENT: [debug_root],
    }


def test_three_tier_policy_defaults():
    policy = RetentionPolicy()
    assert policy.operational_days == 30
    assert policy.audit_days == 180
    assert policy.debug_content_days == 7
    assert policy.days_for(RetentionCategory.DEBUG_CONTENT) == 7.0


def test_expired_deleted_unexpired_kept(tmp_path: Path):
    roots = _sample_roots(tmp_path)
    reports = sweep_roots(roots, RetentionPolicy(), now=NOW)
    assert not (tmp_path / "operational" / "old.log").exists()
    assert (tmp_path / "operational" / "new.log").exists()
    assert (tmp_path / "audit" / "recent.jsonl").exists()
    assert not (tmp_path / "audit" / "ancient.jsonl").exists()
    assert not (tmp_path / "debug" / "capture.bin").exists()

    by_category = {r.category: r for r in reports}
    assert by_category[RetentionCategory.OPERATIONAL].deleted == 1
    assert by_category[RetentionCategory.OPERATIONAL].kept == 1
    assert by_category[RetentionCategory.AUDIT].deleted == 1
    assert by_category[RetentionCategory.AUDIT].kept == 1
    assert by_category[RetentionCategory.DEBUG_CONTENT].deleted == 1
    assert all(r.errors == [] for r in reports)


def test_sweep_is_idempotent(tmp_path: Path):
    roots = _sample_roots(tmp_path)
    sweep_roots(roots, RetentionPolicy(), now=NOW)
    second = sweep_roots(roots, RetentionPolicy(), now=NOW)
    assert all(r.deleted == 0 for r in second)
    assert all(r.errors == [] for r in second)


def test_dry_run_reports_without_deleting(tmp_path: Path):
    roots = _sample_roots(tmp_path)
    reports = sweep_roots(roots, RetentionPolicy(), now=NOW, dry_run=True)
    assert (tmp_path / "operational" / "old.log").exists()
    assert (tmp_path / "debug" / "capture.bin").exists()
    by_category = {r.category: r for r in reports}
    assert by_category[RetentionCategory.OPERATIONAL].deleted == 1
    assert by_category[RetentionCategory.OPERATIONAL].dry_run is True
    assert by_category[RetentionCategory.OPERATIONAL].bytes_freed > 0


def test_missing_root_downgrades_to_empty_scan(tmp_path: Path):
    reports = sweep_roots(
        {RetentionCategory.OPERATIONAL: [tmp_path / "not_exist"]},
        RetentionPolicy(),
        now=NOW,
    )
    assert len(reports) == 1
    assert reports[0].scanned == 0
    assert reports[0].deleted == 0
    assert reports[0].errors == []


def test_unlink_failure_recorded_not_fatal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    root = tmp_path / "operational"
    _touch(root / "stuck.log", NOW - 40 * DAY)

    def boom(self: Path) -> None:
        raise OSError("locked")

    monkeypatch.setattr(Path, "unlink", boom)
    reports = sweep_roots(
        {RetentionCategory.OPERATIONAL: [root]}, RetentionPolicy(), now=NOW
    )
    assert reports[0].deleted == 0
    assert reports[0].kept == 0
    assert len(reports[0].errors) == 1
    assert "locked" in reports[0].errors[0]
    assert (root / "stuck.log").exists()


def test_prune_empty_dirs_after_sweep(tmp_path: Path):
    root = tmp_path / "operational"
    _touch(root / "nested" / "deep" / "old.log", NOW - 40 * DAY)
    sweep_roots({RetentionCategory.OPERATIONAL: [root]}, RetentionPolicy(), now=NOW)
    removed = prune_empty_dirs(root)
    assert removed >= 2
    assert root.is_dir()
    assert not (root / "nested").exists()
