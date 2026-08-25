"""1 万行基准：COPY 批量导入 vs 逐行 INSERT 基线（2.1 evidence）。

同一样例 workspace、同一 mapping、同一 transform 管线：
- COPY 路径 = importer.run_import。
- 逐行基线 = psycopg 单行 ``INSERT ... ON CONFLICT DO NOTHING``（批次提交），
  与 COPY 共用 TableSpecs/transform/required 过滤，保证口径一致。

输出：``openspec/evidence/phase1b/results/<run_id>-bench.json``（含耗时、加速比、
逐表行数校验）。

用法:
    uv run python scripts/migrate/bench.py --workspace DIR --mapping FILE [--pg-url URL]
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql as pgsql

from scripts.migrate.config import MigrationConfig, utc_now_iso
from scripts.migrate.importer import TABLE_SPECS, _iter_spec_rows, _preflight, _resolve_tenant, run_import
from scripts.migrate.source import SqliteSource


def _truncate(url: str) -> None:
    conn = psycopg.connect(url, autocommit=True)
    try:
        for spec in TABLE_SPECS:
            conn.execute(f"TRUNCATE TABLE {spec.name}")
    finally:
        conn.close()


def rowwise_import(cfg: MigrationConfig) -> float:
    """逐行 INSERT 基线；返回耗时秒数。"""
    source = SqliteSource(cfg.workspace)
    mapping = __import__("scripts.migrate.mapping", fromlist=["TenantMapping"]).TenantMapping.load(cfg.mapping_path)
    _identities, tick_tenants = _preflight(source, mapping)
    conn = psycopg.connect(cfg.pg_url)
    start = time.monotonic()
    try:
        for spec in TABLE_SPECS:
            placeholders = ", ".join(["%s"] * len(spec.columns))
            stmt = pgsql.SQL(
                "INSERT INTO {} ({}) VALUES ({}) ON CONFLICT DO NOTHING"
            ).format(
                pgsql.Identifier(spec.name),
                pgsql.SQL(", ").join(pgsql.Identifier(c) for c in spec.columns),
                pgsql.SQL(placeholders),
            )
            rows = _iter_spec_rows(source, spec)
            n_in_batch = 0
            for row in rows:
                tid = _resolve_tenant(spec, row, mapping, tick_tenants)
                out = spec.transform(row, tid) if spec.transform else dict(row)
                if any(out.get(c) is None for c in spec.required):
                    continue
                conn.execute(stmt, [out.get(c) for c in spec.columns])
                n_in_batch += 1
                if n_in_batch >= cfg.batch_size:
                    conn.commit()
                    n_in_batch = 0
            conn.commit()
    finally:
        conn.close()
    return time.monotonic() - start


def run_bench(cfg: MigrationConfig) -> dict[str, Any]:
    cfg.ensure_dirs()
    _truncate(cfg.pg_url)
    t0 = time.monotonic()
    report = run_import(cfg)
    t_copy = time.monotonic() - t0

    _truncate(cfg.pg_url)
    t_row = rowwise_import(cfg)

    total_src = sum(r["source_count"] for r in report["tables"].values())
    total_ins = sum(r["inserted"] for r in report["tables"].values())
    speedup = (t_row / t_copy) if t_copy > 0 else None

    out = {
        "meta": {
            "bench": "phase1b-copy-vs-rowwise",
            "run_id": cfg.run_id,
            "workspace": str(cfg.workspace),
            "batch_size": cfg.batch_size,
            "total_source_rows": total_src,
            "total_inserted": total_ins,
        },
        "copy_elapsed_sec": round(t_copy, 3),
        "rowwise_elapsed_sec": round(t_row, 3),
        "speedup_x": round(speedup, 2) if speedup else None,
        "parity": {
            name: {
                "source": st["source_count"],
                "inserted": st["inserted"],
                "conflict": st["skipped_conflict"],
                "invalid": st["skipped_invalid"],
            }
            for name, st in report["tables"].items()
        },
        "ok": all(
            st["source_count"] == st["inserted"] + st["skipped_invalid"]
            for st in report["tables"].values()
        ),
    }
    path = cfg.results_dir / f"{cfg.run_id}-bench.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description="COPY vs 逐行 INSERT 基准")
    parser.add_argument("--workspace", required=True, type=str)
    parser.add_argument("--mapping", required=True, type=str)
    parser.add_argument("--pg-url", default=None, type=str)
    parser.add_argument("--batch-size", default=5000, type=int)
    args = parser.parse_args()
    cfg = MigrationConfig.from_args(
        workspace=args.workspace,
        mapping=args.mapping,
        pg_url=args.pg_url,
        batch_size=args.batch_size,
        run_id="bench-" + utc_now_iso(),
    )
    result = run_bench(cfg)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
