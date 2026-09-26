"""RRF 输入排名只来自 lane final（C13 验收 4，ADR-2/3/6）。

覆盖：raw 值序与 final 序相反时 RRF 序随 final 序；BM25 巨值 vs cosine 低值
不跨 lane 比较；注入排序保持 RRF 返回序（不再按 score 重排覆盖）。

fake store 走真实 BM25 分支（_fts_available=True + keyword_search_bm25），
sparse_final 由 enrich_keyword_hits 按 bm25_raw 归一化与 hotness 融合重算，
保证测试对象是真实分数链而非手填字段。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, cast

from infra.storage.interfaces import TenantContext
from memory2.retriever import Retriever
from memory2.embedder import Embedder
from memory2.store import MemoryStore2

_NOW_ISO = datetime.now(timezone.utc).isoformat()
_OLD_ISO = datetime(2020, 1, 1, tzinfo=timezone.utc).isoformat()


class _StaticEmbedder:
    async def embed(self, text: str) -> list[float]:
        return [1.0, 0.0]


class _LaneStore:
    """可编程双 lane store：dense 走 vector_search_batch，sparse 走 BM25 分支。

    `_fts_available=True` + `keyword_search_bm25` 让 retriever 走真实 BM25
    路径（keyword_score → bm25_raw → enrich 归一化/融合）。
    """

    def __init__(self, vector_hits: list[dict], keyword_hits: list[dict]) -> None:
        self._vector_hits = list(vector_hits)
        self._keyword_hits = list(keyword_hits)
        self._fts_available = True

    def vector_search(self, *_args: object, **_kwargs: object) -> list[dict]:
        return list(self._vector_hits)

    def vector_search_batch(
        self, query_vecs: list[list[float]], **_kwargs: object
    ) -> list[list[dict]]:
        return [list(self._vector_hits) for _ in query_vecs]

    def keyword_search_bm25(self, *_args: object, **_kwargs: object) -> list[dict]:
        return list(self._keyword_hits)

    def keyword_search_summary(self, *_args: object, **_kwargs: object) -> list[dict]:
        return list(self._keyword_hits)


def _retrieve(store: _LaneStore, query: str, **kwargs: Any) -> list[dict]:
    retriever = Retriever(
        cast(MemoryStore2, store),
        cast(Embedder, _StaticEmbedder()),
        top_k=10,
        score_threshold=0.0,
        keyword_rrf_weight=0.5,
    )

    async def _run() -> list[dict]:
        return await retriever.retrieve(
            query,
            top_k=10,
            score_threshold=0.0,
            tenant=TenantContext(tenant_id="test"),
            **kwargs,
        )

    return _run_async(_run())


def _run_async(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)


def _topic(identifier: str, summary: str) -> dict:
    return {
        "id": identifier,
        "memory_type": "event",
        "summary": summary,
    }


# ---------------------------------------------------------------------------
# 验收 4：RRF 排名键 = lane final
# ---------------------------------------------------------------------------

_HOT_DENSE_EXTRA = {
    "scope_channel": "tg",
    "scope_chat_id": "1",
    "_reinforcement": 60,
    "_updated_at": _NOW_ISO,
    "_emotional_weight": 0,
}
_COLD_DENSE_EXTRA = {
    "scope_channel": "tg",
    "scope_chat_id": "1",
    "_reinforcement": 1,
    "_updated_at": _OLD_ISO,
    "_emotional_weight": 0,
}


def test_rrf_rank_follows_dense_final_not_raw_semantic() -> None:
    """dense 侧：raw semantic 序 A>B，但 dense final 序 B>A → RRF 随 final。"""
    store = _LaneStore(
        vector_hits=[
            dict(
                _topic("A", "冷但纯语义高"),
                score=0.5,
                extra_json=_COLD_DENSE_EXTRA,
                _score_debug={"semantic": 1.0, "hotness": 0.0, "final": 0.5},
            ),
            dict(
                _topic("B", "热且语义尚可"),
                score=0.9,
                extra_json=_HOT_DENSE_EXTRA,
                _score_debug={"semantic": 0.8, "hotness": 1.0, "final": 0.9},
            ),
        ],
        keyword_hits=[],
    )
    hits = _retrieve(store, "缓存策略 配置调整")
    by_id = {hit["id"]: hit for hit in hits}
    assert by_id["A"]["_score_debug"]["semantic"] > by_id["B"]["_score_debug"]["semantic"]
    assert by_id["B"]["_score_debug"]["final"] > by_id["A"]["_score_debug"]["final"]
    assert by_id["B"]["_lane_ranks"]["dense"] == 1
    assert by_id["A"]["_lane_ranks"]["dense"] == 2
    assert [hit["id"] for hit in hits if hit["id"] in ("A", "B")] == ["B", "A"]


def test_rrf_rank_follows_sparse_final_not_bm25_raw() -> None:
    """sparse 侧：BM25 raw 序 C>D，但 sparse_final（含 hotness）序 D>C → RRF 随 final。"""
    store = _LaneStore(
        vector_hits=[],
        keyword_hits=[
            # BM25 raw 高但冷：sparse_final 低。
            dict(
                _topic("C", "BM25 raw 高但冷"),
                keyword_score=0.9,
                _reinforcement=1,
                _updated_at=_OLD_ISO,
                _emotional_weight=0,
            ),
            # BM25 raw 低但很热：hotness 把 sparse_final 抬高。
            dict(
                _topic("D", "BM25 raw 低但热"),
                keyword_score=0.5,
                _reinforcement=60,
                _updated_at=_NOW_ISO,
                _emotional_weight=0,
            ),
        ],
    )
    hits = _retrieve(store, "缓存策略 配置调整")
    by_id = {hit["id"]: hit for hit in hits}
    # 真实 enrich 后：bm25_raw 序 C > D，sparse_final 序 D > C。
    assert by_id["C"]["bm25_raw"] > by_id["D"]["bm25_raw"]
    assert by_id["D"]["sparse_final"] > by_id["C"]["sparse_final"]
    assert by_id["D"]["_lane_ranks"]["sparse"] == 1
    assert by_id["C"]["_lane_ranks"]["sparse"] == 2
    assert [hit["id"] for hit in hits if hit["id"] in ("C", "D")] == ["D", "C"]


def test_rrf_output_order_follows_lane_final_across_both_lanes() -> None:
    """双 lane 融合：dense rank 按 final（B>A）、sparse rank 按 final（D>C）。

    RRF 只吃 rank 整数，BM25 巨值/低 cosine 不进入跨 lane 比较；rrf_score 保留
    融合前的原始值。
    """
    store = _LaneStore(
        vector_hits=[
            dict(
                _topic("A", "A"),
                score=0.5,
                extra_json=_COLD_DENSE_EXTRA,
                _score_debug={"semantic": 1.0, "hotness": 0.0, "final": 0.5},
            ),
            dict(
                _topic("B", "B"),
                score=0.9,
                extra_json=_HOT_DENSE_EXTRA,
                _score_debug={"semantic": 0.8, "hotness": 1.0, "final": 0.9},
            ),
        ],
        keyword_hits=[
            dict(
                _topic("C", "C"),
                keyword_score=0.9,
                _reinforcement=1,
                _updated_at=_OLD_ISO,
                _emotional_weight=0,
            ),
            dict(
                _topic("D", "D"),
                keyword_score=0.5,
                _reinforcement=60,
                _updated_at=_NOW_ISO,
                _emotional_weight=0,
            ),
        ],
    )
    hits = _retrieve(store, "缓存策略 配置调整")
    by_id = {hit["id"]: hit for hit in hits}
    order = [hit["id"] for hit in hits]
    # 各 lane 内部 rank 由 final 决定。
    assert by_id["B"]["_lane_ranks"]["dense"] == 1
    assert by_id["A"]["_lane_ranks"]["dense"] == 2
    assert by_id["D"]["_lane_ranks"]["sparse"] == 1
    assert by_id["C"]["_lane_ranks"]["sparse"] == 2
    assert order.index("B") < order.index("A")
    assert order.index("D") < order.index("C")
    # rrf_score 是融合前原始 RRF 值。
    assert by_id["B"]["rrf_score"] == _approx(1.0 / 61.0)
    assert by_id["A"]["rrf_score"] == _approx(1.0 / 62.0)
    assert by_id["D"]["rrf_score"] == _approx(0.5 / 61.0)
    assert by_id["C"]["rrf_score"] == _approx(0.5 / 62.0)


def _approx(value: float) -> Any:
    import pytest

    return pytest.approx(value, abs=1e-9)


# ---------------------------------------------------------------------------
# 验收：注入排序 = RRF 返回序（ADR-6，不重排覆盖）
# ---------------------------------------------------------------------------


def test_injection_order_preserves_rrf_order_not_score_order() -> None:
    """score 序与 RRF 序相反（热度 boost 反超）时，注入仍按 RRF 序重排。"""
    store = _LaneStore(
        vector_hits=[
            # score 更高但冷：post-RRF boost 小 → RRF 靠后。
            dict(
                _topic("hot_dense", "高分但冷"),
                score=0.95,
                extra_json=_COLD_DENSE_EXTRA,
                _score_debug={"semantic": 0.95, "hotness": 0.0, "final": 0.95},
            ),
            # score 较低但很热：boost 反超 → RRF 靠前。
            dict(
                _topic("hot_beta", "低分但热"),
                score=0.6,
                extra_json=_HOT_DENSE_EXTRA,
                _score_debug={"semantic": 0.6, "hotness": 1.0, "final": 0.6},
            ),
        ],
        keyword_hits=[],
    )
    retriever = Retriever(
        cast(MemoryStore2, store),
        cast(Embedder, _StaticEmbedder()),
        top_k=10,
        score_threshold=0.5,
        inject_max_event_profile=4,
    )
    items = _run_async(
        retriever.retrieve(
            "缓存策略",
            top_k=10,
            score_threshold=0.5,
            tenant=TenantContext(tenant_id="test"),
            keyword_enabled=False,
        )
    )
    # RRF（含 post-RRF 热度 boost）序：# 低分热条目在前。
    assert [i["id"] for i in items] == ["hot_beta", "hot_dense"]

    selected, _forced, _norms, events = retriever._select_injection_sections(items)
    # 注入只做阈值过滤与配额，不再按 score 重排覆盖 RRF 序。
    assert [i["id"] for i in selected] == ["hot_beta", "hot_dense"]
    assert [item_id for item_id, _text in events] == ["hot_beta", "hot_dense"]


def test_injection_type_threshold_keeps_keyword_only_items() -> None:
    """keyword-only 命中跳过 cosine 校准阈值门（sparse rank 是相关性证据）。"""
    store = _LaneStore(
        vector_hits=[],
        keyword_hits=[
            dict(
                _topic("kw_only", "关键词命中且 final 低于 cosine 阈值量纲"),
                keyword_score=0.3,
                _reinforcement=1,
                _updated_at=_OLD_ISO,
                _emotional_weight=0,
            )
        ],
    )
    retriever = Retriever(
        cast(MemoryStore2, store),
        cast(Embedder, _StaticEmbedder()),
        top_k=10,
        score_threshold=0.6,
        inject_max_event_profile=4,
    )
    items = _run_async(
        retriever.retrieve(
            "缓存策略 配置调整",
            top_k=10,
            score_threshold=0.6,
            tenant=TenantContext(tenant_id="test"),
        )
    )
    assert [i["id"] for i in items] == ["kw_only"]
    selected, _forced, _norms, events = retriever._select_injection_sections(items)
    assert [i["id"] for i in selected] == ["kw_only"]
    assert len(events) == 1