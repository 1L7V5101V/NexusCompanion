#!/usr/bin/env python
"""可重复负载工具 CLI（C0 任务 2.3+）。

用法:
    uv run python scripts/load/harness.py --list
    uv run python scripts/load/harness.py --scenario dry_run --dry-run
    uv run python scripts/load/harness.py --scenario <name> --backend postgres --rounds 50

结果 JSON（含输入参数与固定统计口径）默认写到 openspec/evidence/c0/results/。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))

from scripts.load.result import LoadResult, LoadSpec, RunStats
from scripts.load.scenarios import LoadDeps, list_scenarios, run_scenario

DEFAULT_RESULTS_DIR = REPO_ROOT / "openspec" / "evidence" / "c0" / "results"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _git_revision() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="可重复负载工具")
    parser.add_argument("--scenario", choices=list_scenarios(),
                        help="要执行的场景（缺省取唯一场景或 --list）")
    parser.add_argument("--backend", choices=("sqlite", "postgres"), default="sqlite",
                        help="存储后端（与 config storage.backend 同口径，2.5 对齐）")
    parser.add_argument("--tenants", nargs="*", default=["default"],
                        help="租户列表（默认 default）")
    parser.add_argument("--channels", nargs="*", default=["cli"],
                        help="通道列表（默认 cli）")
    parser.add_argument("--rounds", type=int, default=10,
                        help="每 (tenant, channel) 的 turn 轮数")
    parser.add_argument("--concurrency", type=int, default=4,
                        help="并发 turn 数（脚本 reasoner 高并发模式）")
    parser.add_argument("--reasoner", choices=("script", "llm"), default="script",
                        help="reasoner 模式：script=注入式（无真实 LLM 调用），llm=真实冒烟")
    parser.add_argument("--seed", type=int, default=None, help="随机种子（可复现对账）")
    parser.add_argument("--sim-latency-ms", type=float, default=0.0,
                        help="脚本 reasoner 模拟 LLM 延迟（毫秒）")
    parser.add_argument("--sim-fail-rate", type=float, default=0.0,
                        help="脚本 reasoner 模拟失败率（0.0-1.0，seed 确定性）")
    parser.add_argument("--dry-run", action="store_true",
                        help="dry-run：不建真实存储、不执行真实 turn，仅验证骨架与结果写出")
    parser.add_argument("--workspace", default="", help="工作区路径（sqlite 后端落盘位置）")
    parser.add_argument("--results-dir", default=str(DEFAULT_RESULTS_DIR),
                        help="结果 JSON 输出目录")
    parser.add_argument("--no-write", action="store_true", help="只打印结果 JSON，不写文件")
    parser.add_argument("--list", action="store_true", help="列出可用场景并退出")
    return parser


def _write_result(result: LoadResult, results_dir: Path) -> Path:
    results_dir.mkdir(parents=True, exist_ok=True)
    name = f"{result.spec.scenario}-{result.spec.backend}-{result.started_at}.json"
    path = results_dir / name
    path.write_text(
        json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return path


async def _run(args: argparse.Namespace) -> int:
    spec = LoadSpec(
        scenario=args.scenario,
        backend=args.backend,
        tenants=tuple(args.tenants),
        channels=tuple(args.channels),
        rounds=args.rounds,
        concurrency=args.concurrency,
        reasoner=args.reasoner,
        seed=args.seed,
        sim_latency_ms=args.sim_latency_ms,
        sim_fail_rate=args.sim_fail_rate,
        dry_run=args.dry_run,
        workspace=args.workspace,
    )
    deps = LoadDeps(workspace=args.workspace)
    started_at = _now()
    started = datetime.now(timezone.utc)
    runs: dict[str, RunStats] = await run_scenario(args.scenario, spec, deps)
    duration = (datetime.now(timezone.utc) - started).total_seconds()

    result = LoadResult(
        spec=spec,
        runs=runs,
        started_at=started_at,
        finished_at=_now(),
        duration_seconds=duration,
        git_revision=_git_revision(),
    )
    if not runs:
        result.notes.append("场景未产出任何后端统计（runs 为空）")
    if spec.dry_run:
        result.notes.append("dry-run：未执行真实 turn，attempted=0 为预期")

    if args.no_write:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    path = _write_result(result, Path(args.results_dir))
    print(f"scenario={spec.scenario} backend={spec.backend} -> {path}")
    for backend, stats in result.runs.items():
        overall = stats.overall.to_dict()
        print(
            f"  {backend}: attempted={overall['attempted']} "
            f"succeeded={overall['succeeded']} failed={overall['failed']}"
        )
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.list:
        for name in list_scenarios():
            print(name)
        return 0
    if not args.scenario:
        names = list_scenarios()
        if len(names) == 1:
            args.scenario = names[0]
        else:
            parser.error("--scenario 必填（--list 查看可用场景）")
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
