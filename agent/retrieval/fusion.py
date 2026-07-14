"""
FusionEngine — 跨源 RRF 融合 + 新鲜度加权 + 语义去重

仅在多源命中时激活。单源场景跳过此模块直接注入。
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Sequence

logger = logging.getLogger(__name__)

_RRF_K = 60  # RRF 常量，与 DefaultMemoryEngine 内部一致
_FRESHNESS_BOOST_24H = 1.5
_FRESHNESS_BOOST_1W = 1.2


@dataclass
class ScoredItem:
    """来自某一检索源的条目。"""

    source: str          # "graph" | "vector" | "web"
    content: str         # 可读的摘要/文本
    score: float         # 源内部的原始排序分
    timestamp: float | None = None  # Unix 时间戳，用于新鲜度加权
    metadata: dict[str, object] = field(default_factory=dict)


@dataclass
class FusedItem:
    """融合后的条目。"""

    source: str
    content: str
    fused_score: float   # RRF 融合后的分数
    original_score: float
    rank: int


class FusionEngine:
    """跨源 RRF 融合引擎。

    用法:
        engine = FusionEngine()
        fused = engine.fuse({"graph": [...], "vector": [...]})
    """

    # ── RRF 融合（核心） ──────────────────────────────────────

    def fuse(
        self,
        source_items: dict[str, Sequence[ScoredItem]],
        k: int = _RRF_K,
    ) -> list[FusedItem]:
        """多源 RRF 融合 + 新鲜度加权 + 去重。"""
        all_fused: list[FusedItem] = []

        for source, items in source_items.items():
            if not items:
                continue
            # 对每个源内部打分排序（高→低），计算 RRF score
            sorted_items = sorted(items, key=lambda x: x.score, reverse=True)
            for rank, item in enumerate(sorted_items):
                rrf_score = 1.0 / (k + rank + 1)
                # 新鲜度加权
                weighted = rrf_score * self._freshness_factor(item)
                all_fused.append(FusedItem(
                    source=item.source,
                    content=item.content,
                    fused_score=weighted,
                    original_score=item.score,
                    rank=rank + 1,
                ))

        if not all_fused:
            return []

        # 排序（高→低）
        all_fused.sort(key=lambda x: x.fused_score, reverse=True)

        # 去重（按内容 hash）
        return self._dedup(all_fused)

    # ── 新鲜度加权 ────────────────────────────────────────────

    @staticmethod
    def _freshness_factor(item: ScoredItem) -> float:
        """时间越近，加权越高。仅对有时戳的 web 结果生效。"""
        if item.source != "web" or item.timestamp is None:
            return 1.0

        import time
        age_seconds = time.time() - item.timestamp
        age_hours = age_seconds / 3600

        if age_hours < 24:
            return _FRESHNESS_BOOST_24H
        elif age_hours < 168:  # 7天
            return _FRESHNESS_BOOST_1W
        return 1.0

    # ── 去重 ──────────────────────────────────────────────────

    @staticmethod
    def _dedup(items: list[FusedItem]) -> list[FusedItem]:
        """按内容 hash 去重，保留第一个（分数最高的）。"""
        seen: set[str] = set()
        result: list[FusedItem] = []
        for item in items:
            h = hashlib.md5(item.content[:200].encode()).hexdigest()
            if h not in seen:
                seen.add(h)
                result.append(item)
            else:
                logger.debug("去重丢弃: [%s] %s...", item.source, item.content[:60])
        return result

    # ── 格式化输出 ────────────────────────────────────────────

    @staticmethod
    def format_block(fused: list[FusedItem]) -> str:
        """将融合结果格式化为注入 block。"""
        if not fused:
            return ""

        lines: list[str] = []
        current_source: str | None = None
        for item in fused:
            if item.source != current_source:
                source_label = {
                    "graph": "对话上下文 (Graph)",
                    "vector": "知识库 (Vector RAG)",
                    "web": "互联网 (Web Search)",
                }.get(item.source, item.source)
                lines.append(f"---\n[检索来源: {source_label}]")
                current_source = item.source
            lines.append(f"- {item.content}")

        return "\n".join(lines)
