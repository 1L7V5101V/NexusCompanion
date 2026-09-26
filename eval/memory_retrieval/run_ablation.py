"""消融矩阵入口（ADR-7）：A/B/C/D，全部保留双 lane hotness。

- A：默认（dense + BM25/hotness + RRF）
- B：A + HyDE（stub 假设文本经 aux_queries 注入向量 lane）
- C：A + query rewrite（stub 改写 query）
- D：A + reranker（确定性 stub：RRF 截断后按 gold 相关性重排）

stub 使用 gold 提示词：本评测衡量管线与开关接通性，不评价 stub 本身质量。

用法：
    python -m eval.memory_retrieval.run_ablation --out openspec/evidence/c13-memory-retrieval-bm25
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from eval.memory_retrieval.dataset import RetrievalQuery, build_dataset
from eval.memory_retrieval.run import EvalConfig, RunResult, run_baseline

_MODE_NAMES = {"A": "默认", "B": "A+HyDE", "C": "A+rewrite", "D": "A+reranker"}


def _gold_hints(query: RetrievalQuery, *, rewrite: bool) -> list[str]:
    domain = query.domain
    hints = [domain]
    for phrase in query.text.replace("如何", "").replace("？", "").split():
        if phrase and phrase not in hints:
            hints.append(phrase)
    prefix = "基于用户近期情况，" if not rewrite else "结合用户维护的资料，"
    return [hint for hint in hints[:2]]


def _aux_queries(query: RetrievalQuery) -> list[str]:
    return [f"我最近处理过 {query.text} 的相关情况"]


def _rewrite(query: RetrievalQuery) -> str:
    return f"{query.text} 结合 {query.domain} 用户维护的资料"


def _reranker_for(query: RetrievalQuery):
    """每个 query 生成一个确定性 reranker（协议形状：async rerank）。"""
    score_by_id = {item_id: grade for item_id, grade in query.gold.items() if grade >= 1}

    class _GoldReranker:
        async def rerank(self, _query: str, items: list[dict]) -> list[dict]:
            return sorted(
                items,
                key=lambda item: -score_by_id.get(str(item.get("id")), 0),
            )

    return _GoldReranker()


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

    # dataclasses.replace 只覆盖注入点字段，其余配置与 A 模式共享。
    modes: dict[str, EvalConfig] = {
        "A": config,
        "B": replace(config, aux_queries_for=_aux_queries),
        "C": replace(config, rewrite_query=_rewrite),
        "D": replace(config, reranker_for=_reranker_for),
    }

    results: dict[str, RunResult] = {}
    for mode, mode_cfg in modes.items():
        results[mode] = await run_baseline(
            items=items,
            queries=queries,
            config=mode_cfg,
            mode=mode,
        )

    table_rows = []
    for mode in ("A", "B", "C", "D"):
        res = results[mode]
        s = res.summary
        table_rows.append(
            {
                "mode": mode,
                "name": _MODE_NAMES[mode],
                "recall@1": round(float(s["recall@1"]), 4),
                "recall@3": round(float(s["recall@3"]), 4),
                "recall@5": round(float(s["recall@5"]), 4),
                "mrr": round(float(s["mrr"]), 4),
                "ndcg@3": round(float(s["ndcg@3"]), 4),
                "injection_hit_rate": round(float(s["injection_hit_rate"]), 4),
                "p95_latency_s": round(float(s["p95_latency_s"]), 6),
            }
        )

    payload = {
        "config": {
            "seed": args.seed,
            "items": args.items,
            "queries": args.queries,
            "top_k": args.top_k,
            "threshold": args.threshold,
        },
        "rows": table_rows,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if args.json_out is not None:
        args.json_out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    md = ["## C13 消融矩阵（双 lane hotness 全保留）\n"]
    md.append("| 模式 | recall@1 | recall@3 | recall@5 | mrr | ndcg@3 | 注入命中率 | p95 |")
    md.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in table_rows:
        md.append(
            f"| {row['name']} | {row['recall@1']:.4f} | {row['recall@3']:.4f} | "
            f"{row['recall@5']:.4f} | {row['mrr']:.4f} | {row['ndcg@3']:.4f} | "
            f"{row['injection_hit_rate']:.4f} | {row['p95_latency_s']:.6f} |"
        )
    if args.md_out is not None:
        args.md_out.write_text("\n".join(md), encoding="utf-8")

    print(json.dumps({"rows": table_rows}, ensure_ascii=False, indent=2))
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