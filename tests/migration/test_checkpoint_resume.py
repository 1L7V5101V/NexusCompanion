"""2.2 checkpoint 断点续传：中断 → 高水位记录 → 续跑对齐。"""
from __future__ import annotations

import json

import pytest

from scripts.migrate.bulk import BulkPgWriter
from scripts.migrate.checkpoint import load_checkpoint
from scripts.migrate.importer import run_import

from tests.migration.helpers import expected_counts, pg_counts


def test_interrupt_then_resume(make_cfg, mig_pg_url, truncate_all, monkeypatch):
    """中途异常后 checkpoint 记录边界；续跑后逐表行数与源一致。"""
    cfg = make_cfg("interrupt", batch_size=100)

    real = BulkPgWriter.copy_table
    calls = {"n": 0}

    def flaky(self, table, columns, rows, *, partitioned=False):
        calls["n"] += 1
        if calls["n"] == 3:  # sessions 一批 + messages 首批后中断
            raise RuntimeError("simulated interrupt")
        return real(self, table, columns, rows, partitioned=partitioned)

    monkeypatch.setattr(BulkPgWriter, "copy_table", flaky)
    with pytest.raises(RuntimeError, match="simulated interrupt"):
        run_import(cfg)
    monkeypatch.undo()

    # 已提交批次保留、checkpoint 记录到边界。
    mid = pg_counts(mig_pg_url)
    assert mid["sessions"] == expected_counts(cfg.workspace)["sessions"]
    assert 0 < mid["messages"] < expected_counts(cfg.workspace)["messages"]
    ckpt = load_checkpoint(cfg.checkpoint_path)
    assert ckpt is not None
    assert "sessions" in ckpt.tables
    assert "messages" in ckpt.tables
    # checkpoint 是合法 JSON 原子文件。
    json.loads(cfg.checkpoint_path.read_text(encoding="utf-8"))

    # 续跑：同 run_id，跳过已完成行，最终对齐。
    report = run_import(cfg)
    assert report["tables"]["messages"]["skipped_resume"] == mid["messages"]
    final = pg_counts(mig_pg_url)
    expected = expected_counts(cfg.workspace)
    for table, n in expected.items():
        assert final[table] == n, f"{table}: {final[table]} != {n}"
