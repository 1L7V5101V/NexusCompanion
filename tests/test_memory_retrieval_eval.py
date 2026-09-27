"""离线评测冒烟测试（C13 验收 2/6）：数据集确定性、A 基线可复现、降级可用。

用小规模数据集（少量条目/查询）验证评测管线本身：指标产出齐全且确定性
（同 seed 两次运行 recall/mrr/ndcg/injection 一致），embed 失败降级路径
可运行并给出退化幅度。不评测指标绝对水平（P95 只记录观测值）。
"""

from __future__ import annotations

import asyncio

from eval.memory_retrieval.dataset import build_dataset
from eval.memory_retrieval.run import EvalConfig, run_baseline, run_degrade


def _small_config() -> EvalConfig:
    return EvalConfig(n_items=40, n_queries=14, top_k=5, score_threshold=0.3)


def test_dataset_is_deterministic() -> None:
    items_a, queries_a = build_dataset(seed=7, n_items=40, n_queries=14)
    items_b, queries_b = build_dataset(seed=7, n_items=40, n_queries=14)
    assert [i.summary for i in items_a] == [i.summary for i in items_b]
    assert [q.text for q in queries_a] == [q.text for q in queries_b]
    assert all(q.gold for q in queries_a), "每个查询都应有 graded gold"


def test_baseline_summary_is_reproducible() -> None:
    items, queries = build_dataset(seed=7, n_items=40, n_queries=14)
    cfg = _small_config()

    async def _run() -> dict[str, float | int]:
        result = await run_baseline(items=items, queries=queries, config=cfg, mode="A")
        return result.summary

    summary_a = asyncio.run(_run())
    summary_b = asyncio.run(_run())
    for key in (
        "recall@1",
        "recall@3",
        "recall@5",
        "mrr",
        "ndcg@3",
        "injection_hit_rate",
        "n_queries",
        "retrieved_total",
    ):
        assert summary_a[key] == summary_b[key], f"{key} 应同 seed 可复现"
    assert summary_a["n_queries"] == 14
    # 成本计数：每 query 一次 embed（无 aux 时）。
    assert summary_a["retrieved_total"] > 0


def test_degrade_report_quantifies_keyword_fallback() -> None:
    items, queries = build_dataset(seed=7, n_items=40, n_queries=14)
    cfg = _small_config()
    degraded, delta = asyncio.run(
        run_degrade(items=items, queries=queries, config=cfg)
    )
    assert degraded.mode == "degrade"
    # 降级报告结构齐全；keyword-only 退化幅度为非负观测值。
    assert set(delta) == {"recall@1_delta", "recall@5_delta", "mrr_delta"}
    for value in delta.values():
        assert value >= 0.0
