"""A 基线离线评测入口（ADR-7）。

运行默认配置（dense + BM25/hotness + RRF）在合成数据集上的检索指标，
并存档 JSON + markdown 到指定输出目录。

用法：
    python -m eval.memory_retrieval.run_eval --out openspec/evidence/c13-memory-retrieval-bm25
"""

from __future__ import annotations

import argparse
import asyncio
import json
from datetime import datetime, timezone
from pathlib import Path

from eval.memory_retrieval.dataset import build_dataset
from eval.memory_retrieval.run import EvalConfig, run_baseline, run_degrade


def _row(key: str, value: object) -> str:
    return f"| {key} | {value} |\n"


def _markdown(result: object) -> str:
    summary = result["summary"]
    cost = result["cost"]
    degrade = result.get("degrade") or {}
    lines = ["## C13 memory retrieval A 基线\n"]
    lines.append("| 指标 | 值 |")
    lines.append("| --- | --- |")
    for key, value in summary.items():
        lines.append(_row(key, _fmt(value)))
    lines.append(_row("embed_calls", cost.get("embed_calls", 0)))
    lines.append(_row("llm_calls", cost.get("llm_calls", 0)))
    if degrade:
        lines.append("\n## embed 失败退化（keyword-only）\n")
        lines.append("| 指标 | 差额 |")
        lines.append("| --- | --- |")
        for key, value in degrade.items():
            lines.append(_row(key, _fmt(value)))
    return "".join(lines)


def _fmt(value: object) -> str:
    if isinstance(value, float):
        return f"{value:.4f}"
    return str(value)


async def _run(args: argparse.Namespace) -> int:
    items, queries = build_dataset(
        seed=args.seed,
        n_items=args.items,
        n_queries=args.queries,
    )
    config = EvalConfig(
        seed=args.seed,
        n_items=args.items,
        n_queries=args.queries,
        top_k=args.top_k,
        score_threshold=args.threshold,
    )

    baseline = await run_baseline(items=items, queries=queries, config=config, mode="A")
    degraded, delta = await run_degrade(items=items, queries=queries, config=config)
    result = {
        "mode": "A",
        "config": {
            "seed": args.seed,
            "items": args.items,
            "queries": args.queries,
            "top_k": args.top_k,
            "threshold": args.threshold,
        },
        "summary": baseline.summary,
        "cost": baseline.cost,
        "degrade": {"keyword_only": delta},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if args.json_out is not None:
        args.json_out.write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    if args.md_out is not None:
        args.md_out.write_text(_markdown(result), encoding="utf-8")

    print(json.dumps({"summary": baseline.summary, "degrade": delta}, ensure_ascii=False, indent=2))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=202600901)
    parser.add_argument("--items", type=int, default=120)
    parser.add_argument("--queries", type=int, default=40)
    parser.add_argument("--top-k", type=int, default=8)
    parser.add_argument("--threshold", type=float, default=0.3)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument("--md-out", type=Path, default=None)
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()