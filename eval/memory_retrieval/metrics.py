"""检索指标（ADR-7）：Recall@k / MRR / nDCG@k / 注入命中率 / P95 / 退化幅度。

`summarize(results, gold)` 输入一个 query 的检索输出，输出确定性指标；
纯函数、无阈值断言（P95 只记录观测值；SLO 红线不进代码，对齐 C12）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import mean


@dataclass(frozen=True)
class QueryEval:
    query_id: str
    hit_ids: list[str]  # 检索返回序（RRF 序）
    injected_ids: list[str]  # 注入筛选后的 item id 序
    gold: dict[str, int]  # graded relevance 0-3
    latency_s: float


def _recall_at(query: QueryEval, k: int) -> float:
    if not query.gold:
        return 0.0
    hits = set(query.hit_ids[:k])
    recalled = sum(1 for gid in query.gold if gid in hits)
    return recalled / len(query.gold)


def _mrr(query: QueryEval) -> float:
    for rank, item_id in enumerate(query.hit_ids, start=1):
        if item_id in query.gold:
            return 1.0 / rank
    return 0.0


def _ndcg_at(query: QueryEval, k: int) -> float:
    if not query.gold:
        return 0.0
    dcg = 0.0
    for rank, item_id in enumerate(query.hit_ids[:k], start=1):
        rel = query.gold.get(item_id, 0)
        dcg += (2**rel - 1) / math.log2(rank + 1)
    ideal = sorted(query.gold.values(), reverse=True)[:k]
    ideal_dcg = sum((2**rel - 1) / math.log2(rank + 1) for rank, rel in enumerate(ideal, start=1))
    return dcg / ideal_dcg if ideal_dcg > 0 else 0.0


def _injection_hit_rate(query: QueryEval) -> float:
    injected = set(query.injected_ids)
    return float(any(gid in injected for gid in query.gold if query.gold[gid] >= 1))


def _p95(values: list[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, math.ceil(0.95 * len(ordered)) - 1)]


def summarize(queries: list[QueryEval]) -> dict[str, float | int]:
    """聚合一批 QueryEval 为宏观指标摘要（确定性、无阈值断言）。"""
    ks = (1, 3, 5)
    recall_at = {f"recall@{k}": mean(_recall_at(q, k) for q in queries) for k in ks}
    injection = mean(_injection_hit_rate(q) for q in queries)
    latencies = [q.latency_s for q in queries]
    retrieved_total = sum(len(q.hit_ids) for q in queries)
    injected_total = sum(len(q.injected_ids) for q in queries)
    return {
        **recall_at,
        "mrr": mean(_mrr(q) for q in queries),
        **{"ndcg@" + str(k): mean(_ndcg_at(q, k) for q in queries) for k in ks},
        "injection_hit_rate": injection,
        "p95_latency_s": _p95(latencies),
        "avg_latency_s": mean(latencies),
        "n_queries": len(queries),
        "retrieved_total": retrieved_total,
        "injected_total": injected_total,
        "gold_items_total": sum(len(q.gold) for q in queries),
    }


def degrade_report(
    baseline: list[QueryEval],
    degraded: list[QueryEval],
) -> dict[str, float]:
    """注入 embed 异常后的 keyword-only 退化幅度（recall@k 差额）。"""
    base = summarize(baseline)
    fail = summarize(degraded)
    return {
        "recall@1_delta": float(base["recall@1"] - fail["recall@1"]),
        "recall@5_delta": float(base["recall@5"] - fail["recall@5"]),
        "mrr_delta": float(base["mrr"] - fail["mrr"]),
    }