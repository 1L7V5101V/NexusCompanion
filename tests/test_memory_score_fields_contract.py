"""score 字段分离契约测试（C13 验收 1，ADR-1 七字段 fixture 交叉校验）。

对 `tests/fixtures/memory_score_fields.json` 契约断言，并用真实
MemoryStore2 + Retriever 管线交叉校验：七字段出现的 lane、类型与不变式
与 fixture 一致，且异构分数（semantic/bm25_raw/hotness）永不互相覆盖。
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from typing import Any, cast

import pytest

from infra.storage.interfaces import TenantContext
from memory2.retriever import Retriever, _dense_final_score, _rrf_merge
from memory2.sparse_lane import enrich_keyword_hits, fuse_sparse_final, normalize_bm25
from memory2.store import MemoryStore2
from memory2.embedder import Embedder

_FIXTURE = Path(__file__).parent / "fixtures" / "memory_score_fields.json"

_FIELD_NAMES = {
    "semantic",
    "bm25_raw",
    "bm25_normalized",
    "hotness",
    "dense_final",
    "sparse_final",
    "rrf_score",
}


class _QueryEmbedder:
    """固定 query 向量 [1,0,0]；store 内条目用正交向量制造 keyword-only 场景。"""

    async def embed(self, text: str) -> list[float]:
        return [1.0, 0.0, 0.0]


def _load_fixture() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _retrieve(items: list[tuple[str, str, list[float]]], query: str, **kwargs: Any) -> list[dict[str, object]]:
    """建真实 store + retriever，跑一次 retrieve。"""
    import asyncio

    store = MemoryStore2(_tmp_db(), vec_dim=3)
    for mtype, summary, emb in items:
        store.upsert_item(mtype, summary, embedding=emb, extra={})
    retriever = Retriever(
        cast(MemoryStorageT, store),
        cast(Embedder, _QueryEmbedder()),
        top_k=8,
        score_threshold=0.45,
    )

    async def _run() -> list[dict[str, object]]:
        return await retriever.retrieve(
            query,
            top_k=8,
            tenant=TenantContext(tenant_id="test"),
            **kwargs,
        )

    return asyncio.run(_run())


def _tmp_db() -> Path:
    import tempfile

    return Path(tempfile.mkdtemp()) / "contract.db"


MemoryStorageT = Any


# ---------------------------------------------------------------------------
# fixture 结构
# ---------------------------------------------------------------------------


def test_fixture_declares_seven_fields() -> None:
    fixture = _load_fixture()
    fields = fixture["fields"]
    assert {f["field"] for f in fields} == _FIELD_NAMES
    assert len(fields) == 7
    # 每个字段都有 lane 归属与类型、不变式。
    for field in fields:
        assert field["lane"] in {"dense", "sparse", "both"}
        assert field["type"] in {"float", "float-or-null"}
        assert isinstance(field["invariants"], list) and field["invariants"]


def test_fixture_lane_rank_separation_matches_implementation() -> None:
    fixture = _load_fixture()
    rank = fixture["lane_rank_separation"]
    assert rank["dense_rank_key"] == "_score_debug.final"
    assert rank["sparse_rank_key"] == "sparse_final"
    # 实现侧确实存在这些排名键/函数。
    retriever = importlib.import_module("memory2.retriever")
    assert callable(getattr(retriever, "_rrf_merge"))
    assert callable(getattr(retriever, "_dense_final_score"))
    assert callable(fuse_sparse_final)


# ---------------------------------------------------------------------------
# fixture 与代码产物交叉校验
# ---------------------------------------------------------------------------


def test_dense_hit_carries_score_debug_without_sparse_fields() -> None:
    items = [
        ("event", "用户今天处理了支付流水", [1.0, 0.0, 0.0]),
    ]
    hits = _retrieve(items, "支付流水 对账 记录")
    assert hits, "dense lane 至少命中一条"
    for hit in hits:
        debug = hit.get("_score_debug")
        if not isinstance(debug, dict):
            continue
        semantic = debug.get("semantic")
        final = debug.get("final")
        hot = debug.get("hotness")
        assert isinstance(semantic, float) and 0.0 <= semantic <= 1.0
        assert isinstance(final, float) and 0.0 <= final <= 1.0
        assert isinstance(hot, (int, float)) and 0.0 <= float(hot) <= 1.0
        # dense 命中不携带 sparse 原始分。
        assert hit.get("bm25_raw") is None
        assert hit.get("sparse_final") is None


def test_dense_final_matches_fixture_formula() -> None:
    # _dense_final_score 取 _score_debug.final，缺失时回退 score（fixture 声明）。
    item = {"_score_debug": {"final": 0.66}, "score": 0.1}
    assert _dense_final_score(item) == 0.66
    assert _dense_final_score({"score": 0.55}) == 0.55


def test_keyword_hit_enrich_obey_fixture_invariants() -> None:
    # BM25 命中：raw/raw+K。
    bm25_hit = enrich_keyword_hits(
        [{"id": "b", "keyword_score": 8.0, "bm25_raw": 8.0}],
        normalization_k=4.0,
        hotness_alpha=0.2,
        now=cast(Any, None),
        hit_hotness_fn=None,
    )[0]
    assert bm25_hit["bm25_normalized"] == round(normalize_bm25(8.0, 4.0), 4)
    # sparse_final 用未舍入的 bm25_raw 融合后一次性 round-4（存值本身已舍入）。
    assert bm25_hit["sparse_final"] == fuse_sparse_final(8.0 / (8.0 + 4.0), 0.0, 0.2)
    assert bm25_hit["sparse_source"] == "bm25"

    # LIKE 保底：无 bm25_raw 字段、normalized 直接用 keyword_score 比率。
    like_hit = enrich_keyword_hits(
        [{"id": "l", "keyword_score": 0.4}],
        normalization_k=4.0,
        hotness_alpha=0.2,
        now=cast(Any, None),
        hit_hotness_fn=None,
    )[0]
    assert like_hit["bm25_raw"] is None
    assert like_hit["bm25_normalized"] == 0.4
    assert like_hit["sparse_source"] == "like_fallback"


def test_keyword_only_hit_in_real_pipeline_carries_sparse_fields() -> None:
    """构造 3+ CJK 词 query；正交向量的 keyword 命中只进 sparse lane。"""
    items = [
        # 向量正交于 query [1,0,0] → dense 丢弃，但 summary 命中关键词。
        ("event", "用户最近调试过Redis缓存配置", [0.0, 1.0, 0.0]),
        # 高相似条目进 dense lane。
        ("event", "用户处理了支付对账流水", [1.0, 0.0, 0.0]),
    ]
    hits = _retrieve(items, "Redis 缓存配置")
    assert hits, "应至少命中（keyword-only 或 dense）"
    sparse_hits = [h for h in hits if isinstance(h.get("_lane_ranks"), dict) and "dense" not in h["_lane_ranks"]]
    assert sparse_hits, "存在 keyword-only 命中"
    for hit in sparse_hits:
        assert hit.get("bm25_raw") is None or isinstance(hit.get("bm25_raw"), float)
        assert isinstance(hit.get("bm25_normalized"), float)
        assert 0.0 <= float(hit.get("bm25_normalized", 0.0)) < 1.0
        assert isinstance(hit.get("hotness"), (int, float))
        assert 0.0 <= float(hit.get("sparse_final", 0.0)) < 1.0
        assert hit.get("sparse_source") in {"bm25", "like_fallback"}
        # keyword-only 命中不携带 dense 分数。
        assert hit.get("_score_debug") is None
        # score 回填为 sparse_final（取代旧 keyword_score 回填）。
        assert float(hit.get("score", -1.0)) == float(hit.get("sparse_final", -2.0))


def test_fused_items_carry_rrf_score_and_lane_ranks() -> None:
    items = [
        ("event", "用户最近调试过Redis缓存配置", [0.0, 1.0, 0.0]),
        ("event", "用户处理了支付对账流水", [1.0, 0.0, 0.0]),
    ]
    hits = _retrieve(items, "Redis 缓存配置")
    assert hits
    for hit in hits:
        rrf = hit.get("rrf_score")
        assert isinstance(rrf, float) and rrf > 0.0
        ranks = hit.get("_lane_ranks")
        assert isinstance(ranks, dict)
        assert set(ranks) <= {"dense", "sparse"}


def test_rrf_merge_never_compares_raw_scores_across_lanes() -> None:
    """RRF 只接受 rank 整数：dense 侧按 dense_final、sparse 侧按 sparse_final。

    raw 值序（BM25 巨值与 cosine 低值）不进入跨 lane 比较——断言 _rrf_merge
    的排名键取自 lane final 而非原始分。
    """
    vector_items = [
        {"id": "v1", "memory_type": "event", "summary": "a", "_score_debug": {"final": 0.9}, "score": 0.9},
        {"id": "v2", "memory_type": "event", "summary": "b", "_score_debug": {"final": 0.8}, "score": 0.8},
    ]
    keyword_items = [
        # raw 分数巨大（BM25 巨值），但 final 低（仅 0.1，hotness 为 0）。
        {"id": "k1", "memory_type": "event", "summary": "c", "bm25_raw": 1e6, "sparse_final": 0.1},
        {"id": "k2", "memory_type": "event", "summary": "d", "bm25_raw": 1e6, "sparse_final": 0.4},
    ]
    merged = _rrf_merge(vector_items, keyword_items, top_n=10, hotness_beta=0.0)
    # 排序由 lane final 排名决定：sparse 侧 k2(final 0.4) 排 k1(final 0.1) 之前，
    # 与两者相同的 raw 巨值无关。
    order = [item["id"] for item in merged]
    assert order.index("k2") < order.index("k1")
    assert order.index("v1") < order.index("v2")