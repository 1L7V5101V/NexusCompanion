"""迁移工具 CLI：样例生成、批量导入、校验、S0-S4 切换（随实现逐步扩展）。

用法:
    uv run python -m scripts.migrate.cli sample  --out DIR [--messages N]
    uv run python -m scripts.migrate.cli import  --workspace DIR --mapping FILE \
        [--dry-run] [--batch-size N] [--run-id ID]
    uv run python -m scripts.migrate.cli verify --workspace DIR --mapping FILE
    uv run python -m scripts.migrate.cli status --workspace DIR
    uv run python -m scripts.migrate.cli promote --workspace DIR --mapping FILE
    uv run python -m scripts.migrate.cli audit --workspace DIR --mapping FILE
    uv run python -m scripts.migrate.cli rollback --workspace DIR
    uv run python -m scripts.migrate.cli pitr-rehearsal --workspace DIR --mapping FILE
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from scripts.migrate.config import MigrationConfig
from scripts.migrate.mapping import MappingError
from scripts.migrate.sample_workspace import generate_workspace
from scripts.migrate.state import (
    StateError,
    record_verify_evidence,
    run_audit,
    run_import_cmd,
    run_pitr_rehearsal,
    run_promote,
    run_rollback,
    run_status,
)
from scripts.migrate.verify import run_verify


def _cmd_sample(args: argparse.Namespace) -> int:
    out = generate_workspace(
        Path(args.out),
        n_messages=args.messages,
        n_memory=args.memory,
        n_ticks=args.ticks,
        seed=args.seed,
    )
    print(f"sample workspace -> {out.resolve()}")
    return 0


def _cmd_import(args: argparse.Namespace) -> int:
    cfg = MigrationConfig.from_args(
        workspace=args.workspace,
        pg_url=args.pg_url,
        mapping=args.mapping,
        batch_size=args.batch_size,
        run_id=args.run_id,
    )
    try:
        report = run_import_cmd(cfg, dry_run=args.dry_run)
    except (MappingError, StateError) as exc:
        print(f"[import] 预检失败: {exc}", file=sys.stderr)
        if isinstance(exc, MappingError) and exc.unmapped:
            print(f"[import] 未映射源身份: {exc.unmapped}", file=sys.stderr)
        if isinstance(exc, StateError) and exc.missing:
            for m in exc.missing:
                print(f"[import] 缺失/前置: {m}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0
    for name, st in report["tables"].items():
        print(
            f"  {name:24s} src={st['source_count']:7d} "
            f"ins={st['inserted']:7d} resume={st['skipped_resume']} "
            f"invalid={st['skipped_invalid']} conflict={st['skipped_conflict']}"
        )
    print(f"[import] ok elapsed={report['elapsed_sec']}s "
          f"run_id={cfg.run_id} state={report.get('state')}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    cfg = MigrationConfig.from_args(
        workspace=args.workspace,
        pg_url=args.pg_url,
        mapping=args.mapping,
        run_id=args.run_id,
    )
    try:
        report = run_verify(cfg)
    except MappingError as exc:
        print(f"[verify] 预检失败: {exc}", file=sys.stderr)
        return 2
    for dim, d in report["dimensions"].items():
        print(f"  {dim:24s} ok={d['ok']}")
    if not report["ok"]:
        for dim, d in report["dimensions"].items():
            if d["ok"]:
                continue
            if dim == "row_count":
                for t, st in d["tables"].items():
                    if not st["match"]:
                        print(f"    row_count {t}: source={st['source']} "
                              f"target={st['target']}", file=sys.stderr)
            elif dim == "field_hash":
                for t, st in d["tables"].items():
                    if not st["match"]:
                        print(f"    field_hash {t}: 不一致", file=sys.stderr)
            elif dim == "referential_integrity":
                for name, st in d["checks"].items():
                    if not st["match"]:
                        print(f"    fk {name}: {st['orphans']} 孤儿", file=sys.stderr)
            elif dim == "semantic_sampling":
                for r in d["vector_topk"]["results"]:
                    if not r["match"]:
                        print(f"    vector_topk query#{r['query_index']}: "
                              f"命中/排序不一致", file=sys.stderr)
                for c in d["keyword_search"]["checks"]:
                    if not c["match"]:
                        print(f"    keyword {c['terms']}: 命中集合不一致",
                              file=sys.stderr)
                if not d["session_next_seq"]["ok"]:
                    print("    session next_seq: 不一致", file=sys.stderr)
    state = record_verify_evidence(cfg, report)
    if state is not None:
        print(f"[verify] ok 已记入状态 S1 退出条件证据（state={state}）")
    print(f"[verify] ok={report['ok']} exit_code={report['exit_code']} "
          f"evidence={report['evidence_path']}")
    return report["exit_code"]


def _cmd_status(args: argparse.Namespace) -> int:
    cfg = MigrationConfig.from_args(workspace=args.workspace, pg_url=args.pg_url)
    try:
        report = run_status(cfg)
    except StateError as exc:
        print(f"[status] {exc}", file=sys.stderr)
        return 1
    print(f"状态文件: {report['state_path']}")
    print(f"当前状态: {report['state']}  (updated_at={report['updated_at']})")
    for st, ph in report["phases"].items():
        print(
            f"  {st}  entered={ph.get('entered_at') or '-'} "
            f"exited={ph.get('exited_at') or '-'}"
        )
        for ev in ph.get("enter_evidence", []):
            print(f"      enter_evidence: {ev}")
        for ev in ph.get("exit_evidence", []):
            print(f"      exit_evidence:  {ev}")
    return 0


def _cmd_promote(args: argparse.Namespace) -> int:
    cfg = MigrationConfig.from_args(
        workspace=args.workspace,
        pg_url=args.pg_url,
        mapping=args.mapping,
        run_id=args.run_id,
    )
    try:
        report = run_promote(cfg)
    except StateError as exc:
        print(f"[promote] 被拒绝: {exc}", file=sys.stderr)
        for m in exc.missing:
            print(f"[promote]   缺失/前置: {m}", file=sys.stderr)
        return 1
    smoke = report["smoke"]
    print(f"[promote] config -> backend=postgres "
          f"({report['config_flip']['path']})")
    print(f"[promote] smoke session={smoke['session_key']} "
          f"msgs={smoke['message_count_readback']} "
          f"next_seq={smoke['next_seq_readback']} turn={smoke['turn_id']}")
    print(f"[promote] dual_store: {report['dual_store']['primary']} primary / "
          f"{report['dual_store']['turn_control']}")
    print(f"[promote] ok={report['ok']} state={report['state']} "
          f"evidence={report['evidence_path']}")
    return 0


def _cmd_audit(args: argparse.Namespace) -> int:
    cfg = MigrationConfig.from_args(
        workspace=args.workspace,
        pg_url=args.pg_url,
        mapping=args.mapping,
        run_id=args.run_id,
    )
    try:
        report = run_audit(cfg)
    except StateError as exc:
        print(f"[audit] 被拒绝: {exc}", file=sys.stderr)
        for m in exc.missing:
            print(f"[audit]   缺失/前置: {m}", file=sys.stderr)
        return 1
    print(f"[audit] cycle={report['cycle']['prefix']} "
          f"n_sessions={report['cycle']['n_sessions']}")
    for r in report["cycle_reconciliation"]:
        print(f"    {r['session_key']}: next_seq={r['next_seq']} "
              f"msgs={r['message_count']} turn={r['turn_readback']}")
    print(f"[audit] shadow.stopped={report['shadow']['stopped']} "
          f"archive={report['shadow']['sqlite_archive_path']}")
    print(f"[audit] ok={report['ok']} state={report['state']} "
          f"evidence={report['evidence_path']}")
    return 0


def _cmd_rollback(args: argparse.Namespace) -> int:
    cfg = MigrationConfig.from_args(workspace=args.workspace, pg_url=args.pg_url)
    try:
        report = run_rollback(cfg)
    except StateError as exc:
        print(f"[rollback] 被拒绝: {exc}", file=sys.stderr)
        return 1
    print(f"[rollback] state={report['state']} "
          f"rolled_back={report['rolled_back']}")
    print(f"[rollback] {report['note']}")
    return 0


def _cmd_pitr(args: argparse.Namespace) -> int:
    cfg = MigrationConfig.from_args(
        workspace=args.workspace,
        pg_url=args.pg_url,
        mapping=args.mapping,
        run_id=args.run_id,
    )
    try:
        report = run_pitr_rehearsal(cfg)
    except StateError as exc:
        print(f"[pitr] 被拒绝: {exc}", file=sys.stderr)
        return 1
    print(f"[pitr] counts_match={report['counts_match']} "
          f"marker_absent={report['post_t0_marker_absent']} ok={report['ok']}")
    print(f"[pitr] runbook={report['runbook_path']}")
    print(f"[pitr] evidence={report['evidence_path']}")
    return 0 if report["ok"] else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="migrate",
                                     description="Phase 1B 数据迁移工具")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sample = sub.add_parser("sample", help="生成多通道多租户样例 workspace")
    p_sample.add_argument("--out", required=True, type=str)
    p_sample.add_argument("--messages", type=int, default=10000)
    p_sample.add_argument("--memory", type=int, default=2000)
    p_sample.add_argument("--ticks", type=int, default=60)
    p_sample.add_argument("--seed", type=int, default=42)
    p_sample.set_defaults(func=_cmd_sample)

    p_import = sub.add_parser("import", help="批量导入（COPY + 断点续传 + 幂等）")
    p_import.add_argument("--workspace", required=True, type=str)
    p_import.add_argument("--mapping", required=True, type=str)
    p_import.add_argument("--pg-url", default=None, type=str,
                          help="默认 NEXUS_TEST_PG_URL 或本地 5433")
    p_import.add_argument("--batch-size", default=None, type=int)
    p_import.add_argument("--run-id", default=None, type=str,
                          help="指定 run-id 以续传已中断的运行")
    p_import.add_argument("--dry-run", action="store_true",
                          help="只做预检（mapping 覆盖 / 孤儿 tick），零写入")
    p_import.set_defaults(func=_cmd_import)

    p_verify = sub.add_parser(
        "verify", help="机器可读校验（行数/hash/引用完整性/语义抽样）")
    p_verify.add_argument("--workspace", required=True, type=str)
    p_verify.add_argument("--mapping", required=True, type=str)
    p_verify.add_argument("--pg-url", default=None, type=str)
    p_verify.add_argument("--run-id", default=None, type=str)
    p_verify.set_defaults(func=_cmd_verify)

    p_status = sub.add_parser("status", help="读取 S0-S4 切换状态")
    p_status.add_argument("--workspace", required=True, type=str)
    p_status.add_argument("--pg-url", default=None, type=str)
    p_status.set_defaults(func=_cmd_status)

    p_promote = sub.add_parser("promote", help="S1 → S2（config 翻转 + smoke + 对账）")
    p_promote.add_argument("--workspace", required=True, type=str)
    p_promote.add_argument("--mapping", required=True, type=str)
    p_promote.add_argument("--pg-url", default=None, type=str)
    p_promote.add_argument("--run-id", default=None, type=str)
    p_promote.set_defaults(func=_cmd_promote)

    p_audit = sub.add_parser("audit", help="S2 → S3（业务周期对账 + 停止 shadow）")
    p_audit.add_argument("--workspace", required=True, type=str)
    p_audit.add_argument("--mapping", required=True, type=str)
    p_audit.add_argument("--pg-url", default=None, type=str)
    p_audit.add_argument("--run-id", default=None, type=str)
    p_audit.set_defaults(func=_cmd_audit)

    p_rollback = sub.add_parser("rollback", help="S1 内回滚 → S0（S2 起拒绝）")
    p_rollback.add_argument("--workspace", required=True, type=str)
    p_rollback.add_argument("--pg-url", default=None, type=str)
    p_rollback.set_defaults(func=_cmd_rollback)

    p_pitr = sub.add_parser("pitr-rehearsal", help="PITR 恢复演练 + runbook")
    p_pitr.add_argument("--workspace", required=True, type=str)
    p_pitr.add_argument("--mapping", required=True, type=str)
    p_pitr.add_argument("--pg-url", default=None, type=str)
    p_pitr.add_argument("--run-id", default=None, type=str)
    p_pitr.set_defaults(func=_cmd_pitr)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
