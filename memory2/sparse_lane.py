"""sparse lane 分数链：BM25 raw → query-local 归一化 → hotness 融合为 sparse_final。

目标基线（PILOT_ROADMAP §4.4）：
    BM25 raw ──(raw/(raw+K))──▶ bm25_normalized ──┐
                                                  ├─▶ sparse_final = (1-αs)·normalized + αs·hotness
    reinforcement/时间/emotional_weight ─▶ hotness ┘

LIKE 保底命中没有 BM25 raw：keyword_search_summary 的 keyword_score 已是
「命中词数/总词数」归一化比率，直接作为 bm25_normalized 基底（bm25_raw=None）。
所有字段只在此处生成、全程分离，禁止把异构分数相加或互相覆盖（C13 验收 1）。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone

DEFAULT_NORMALIZATION_K = 4.0
DEFAULT_SPARSE_HOTNESS_ALPHA = 0.2

_SPARSE_SOURCE_BM25 = "bm25"
_SPARSE_SOURCE_LIKE = "like_fallback"


def normalize_bm25(raw: float, k: float = DEFAULT_NORMALIZATION_K) -> float:
    """BM25 raw（越高越匹配）饱和式归一化到 [0, 1)：单调、query-local、无上界稳健。

    选择 raw/(raw+K) 而非 min-max：单命中结果集 min-max 退化（max-min=0），
    BM25 raw 无上界，饱和式映射对分布稳健。
    """
    if raw <= 0.0:
        return 0.0
    denom = max(float(k), 1e-9)
    return float(raw) / (float(raw) + denom)


def fuse_sparse_final(
    bm25_normalized: float,
    hotness: float,
    alpha: float = DEFAULT_SPARSE_HOTNESS_ALPHA,
) -> float:
    """sparse_final = (1-αs)·bm25_normalized + αs·hotness（与 dense final 公式同构）。"""
    clamped = max(0.0, min(1.0, float(alpha)))
    normalized = max(0.0, min(1.0, float(bm25_normalized)))
    hot = max(0.0, min(1.0, float(hotness)))
    return round((1.0 - clamped) * normalized + clamped * hot, 4)


def enrich_keyword_hits(
    hits: list[dict],
    *,
    normalization_k: float = DEFAULT_NORMALIZATION_K,
    hotness_alpha: float = DEFAULT_SPARSE_HOTNESS_ALPHA,
    hotness_half_life_days: float = 14.0,
    hit_hotness_fn: Callable[[dict, datetime, float], float] | None = None,
    now: datetime | None = None,
) -> list[dict]:
    """就地补齐 keyword lane 命中的 sparse 分数字段。

    hit_hotness_fn 由调用方注入（Retriever._hit_hotness），保持本模块不依赖
    store 内部实现；未注入时热度按 0 处理（融合公式仍成立）。
    """
    if not hits:
        return hits
    if now is None:
        now = datetime.now(timezone.utc)
    hotness_fn = hit_hotness_fn
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        raw = hit.get("bm25_raw")
        if raw is None:
            # LIKE 保底：keyword_score 已是命中词数/总词数的归一化比率；
            # bm25_raw 显式置 None（fixture 契约要求字段存在且可断言）。
            hit["bm25_raw"] = None
            hit.setdefault("sparse_source", _SPARSE_SOURCE_LIKE)
            normalized = max(0.0, min(1.0, float(hit.get("keyword_score") or 0.0)))
        else:
            hit.setdefault("sparse_source", _SPARSE_SOURCE_BM25)
            normalized = normalize_bm25(float(raw), normalization_k)
        hotness = (
            float(hotness_fn(hit, now, hotness_half_life_days)) if hotness_fn else 0.0
        )
        hit["bm25_normalized"] = round(normalized, 4)
        hit["hotness"] = round(hotness, 4)
        hit["sparse_final"] = fuse_sparse_final(normalized, hotness, hotness_alpha)
        # keyword-only 命中的 score 语义统一为 sparse_final（取代旧 keyword_score
        # raw 值回填），供注入阈值与诊断使用；keyword_score 原值保留。
        hit["score"] = hit["sparse_final"]
    return hits


def sparse_final_of(item: dict) -> float:
    """取 sparse_final 排名键；字段缺失返回 0.0（stable sort 保持 lane 原序）。"""
    if not isinstance(item, dict):
        return 0.0
    raw = item.get("sparse_final")
    return float(raw) if isinstance(raw, int | float) else 0.0
