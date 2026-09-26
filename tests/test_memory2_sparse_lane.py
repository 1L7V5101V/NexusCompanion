"""sparse lane 分数链单测：归一化 / 融合 / 字段补齐（C13 验收 1，ADR-1）。

覆盖：normalize_bm25 公式与单调性、fuse_sparse_final 公式、enrich_keyword_hits
补齐五字段（bm25_raw/bm25_normalized/hotness/sparse_final/sparse_source）、
LIKE 保底分支，以及 keyword-only 命中 score 回填为 sparse_final。
"""

from __future__ import annotations

from datetime import datetime, timezone

from memory2.sparse_lane import (
    DEFAULT_NORMALIZATION_K,
    DEFAULT_SPARSE_HOTNESS_ALPHA,
    enrich_keyword_hits,
    fuse_sparse_final,
    normalize_bm25,
    sparse_final_of,
)

_NOW = datetime(2026, 9, 1, tzinfo=timezone.utc)


def _hotness_fn(item: dict, now: datetime, half_life_days: float) -> float:
    return float(item.get("_eval_hotness", 0.0))


# ---------------------------------------------------------------------------
# normalize_bm25
# ---------------------------------------------------------------------------


def test_normalize_bm25_formula() -> None:
    k = 4.0
    assert normalize_bm25(0.0, k) == 0.0
    assert normalize_bm25(-3.0, k) == 0.0
    # raw/(raw+K)：raw=4, K=4 → 0.5
    assert normalize_bm25(4.0, k) == 4.0 / 8.0
    # 大 raw 饱和趋近 1 但不越界
    assert normalize_bm25(1e9, k) < 1.0


def test_normalize_bm25_monotonic_and_bounded() -> None:
    k = 4.0
    values = [0.1, 0.5, 1.0, 3.0, 8.0, 20.0, 100.0]
    normed = [normalize_bm25(v, k) for v in values]
    assert normed == sorted(normed)
    assert all(0.0 <= v < 1.0 for v in normed)


def test_normalize_bm25_custom_k() -> None:
    # K 越大整体越低（更保守）。
    assert normalize_bm25(4.0, k=1.0) > normalize_bm25(4.0, k=16.0)


# ---------------------------------------------------------------------------
# fuse_sparse_final
# ---------------------------------------------------------------------------


def test_fuse_sparse_final_formula() -> None:
    alpha = 0.2
    expected = round((1.0 - alpha) * 0.6 + alpha * 0.4, 4)
    assert fuse_sparse_final(0.6, 0.4, alpha) == expected


def test_fuse_sparse_final_clamps_inputs() -> None:
    # 越界输入被 clamp 到 [0,1]。
    assert fuse_sparse_final(1.5, -0.5, 0.2) == round(0.8 * 1.0 + 0.2 * 0.0, 4)
    # alpha 越界钳制到 [0,1]。
    assert fuse_sparse_final(1.0, 1.0, 99.0) == 1.0
    assert fuse_sparse_final(1.0, 1.0, -99.0) == round(1.0 * 1.0 + 0.0 * 1.0, 4)


def test_fuse_sparse_final_hotness_dominance() -> None:
    # alpha>0 且 hotness 高时推高 sparse_final。
    assert fuse_sparse_final(0.2, 0.9, 0.2) > fuse_sparse_final(0.2, 0.1, 0.2)
    # alpha=0 时纯归一化分。
    assert fuse_sparse_final(0.7, 1.0, 0.0) == pytest_approx(0.7, 4)


def pytest_approx(value: float, digits: int) -> float:
    return round(value, digits)


# ---------------------------------------------------------------------------
# enrich_keyword_hits（BM25 命中）
# ---------------------------------------------------------------------------


def test_enrich_bm25_hit_fills_all_fields() -> None:
    hits = [
        {
            "id": "b1",
            "memory_type": "event",
            "summary": "用户调试过缓存问题",
            "keyword_score": 12.0,
            "bm25_raw": 12.0,
        }
    ]
    result = enrich_keyword_hits(
        hits,
        normalization_k=DEFAULT_NORMALIZATION_K,
        hotness_alpha=DEFAULT_SPARSE_HOTNESS_ALPHA,
        hit_hotness_fn=_hotness_fn,
        now=_NOW,
    )
    hit = result[0]
    assert hit["sparse_source"] == "bm25"
    assert hit["bm25_normalized"] == pytest_approx(
        normalize_bm25(12.0, DEFAULT_NORMALIZATION_K), 4
    )
    assert hit["hotness"] == 0.0
    expected_final = fuse_sparse_final(
        hit["bm25_normalized"], 0.0, DEFAULT_SPARSE_HOTNESS_ALPHA
    )
    assert hit["sparse_final"] == expected_final
    # keyword-only 命中 score 回填为 sparse_final。
    assert hit["score"] == expected_final
    assert hit["bm25_raw"] == 12.0


def test_enrich_bm25_hit_includes_hotness_in_final() -> None:
    hits = [
        {
            "id": "b1",
            "memory_type": "event",
            "summary": "用户调试过缓存问题",
            "keyword_score": 12.0,
            "bm25_raw": 12.0,
            "_eval_hotness": 0.8,
        }
    ]
    hit = enrich_keyword_hits(
        hits,
        hotness_alpha=0.2,
        hit_hotness_fn=_hotness_fn,
        now=_NOW,
    )[0]
    assert hit["hotness"] == 0.8
    expected = fuse_sparse_final(hit["bm25_normalized"], 0.8, 0.2)
    assert hit["sparse_final"] == expected
    # 热命中 sparse_final 高于同 normalized 的冷命中。
    cold = enrich_keyword_hits(
        [dict(hit, _eval_hotness=0.0)],
        hotness_alpha=0.2,
        hit_hotness_fn=_hotness_fn,
        now=_NOW,
    )[0]
    assert hit["sparse_final"] > cold["sparse_final"]


# ---------------------------------------------------------------------------
# enrich_keyword_hits（LIKE 保底）
# ---------------------------------------------------------------------------


def test_enrich_like_hit_marks_null_raw_and_ratio_normalized() -> None:
    hits = [
        {
            "id": "l1",
            "memory_type": "profile",
            "summary": "用户喜欢喝美式咖啡",
            "keyword_score": 0.5,
        }
    ]
    hit = enrich_keyword_hits(
        hits,
        hotness_alpha=0.2,
        hit_hotness_fn=_hotness_fn,
        now=_NOW,
    )[0]
    # LIKE 保底无 BM25 raw（字段不出现在命中上），normalized 直接用
    # keyword_score 命中词数比率。
    assert hit["bm25_raw"] is None
    assert hit["sparse_source"] == "like_fallback"
    # keyword_score 已是命中词数/词数比率，直接作为 normalized 基底
    assert hit["bm25_normalized"] == 0.5
    assert hit["sparse_final"] == fuse_sparse_final(0.5, 0.0, 0.2)


def test_enrich_like_hit_low_ratio_lowers_final() -> None:
    weak = enrich_keyword_hits(
        [{"id": "w", "keyword_score": 0.1}], hotness_alpha=0.0, now=_NOW
    )[0]
    strong = enrich_keyword_hits(
        [{"id": "s", "keyword_score": 1.0}], hotness_alpha=0.0, now=_NOW
    )[0]
    assert weak["sparse_final"] < strong["sparse_final"]


def test_enrich_keeps_lane_order_when_sparse_final_missing() -> None:
    # sparse_final_of 对缺失字段返回 0：stable sort 保持 lane 原顺序（兼容 fake store）。
    assert sparse_final_of({}) == 0.0
    assert sparse_final_of({"sparse_final": 0.42}) == 0.42
    assert sparse_final_of({"sparse_final": "oops"}) == 0.0
    assert sparse_final_of(None) == 0.0


def test_enrich_empty_and_non_dict_hits_are_safe() -> None:
    assert enrich_keyword_hits([], now=_NOW) == []
    hits = [{}, "not-a-dict", {"id": "x", "keyword_score": 0.5}]
    result = enrich_keyword_hits(hits, now=_NOW)
    assert len(result) == 3
    # 非 dict 原样跳过；空 dict 也是 dict，照常补齐字段（默认 0 基底）。
    assert result[1] == "not-a-dict"
    assert result[0]["sparse_final"] == 0.0
    assert result[0]["sparse_source"] == "like_fallback"
    assert result[2]["sparse_final"] == fuse_sparse_final(0.5, 0.0, 0.2)