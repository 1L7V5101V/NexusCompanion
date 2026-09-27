"""
Memory v2 检索器：查询 → top-k items + 格式化注入块
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import json
import logging
import re
from typing import Any, TypeVar, cast

from infra.storage.interfaces import MemoryStorage, TenantContext
from memory2.reranker import Reranker
from memory2.sparse_lane import enrich_keyword_hits, sparse_final_of
from memory2.store import _coerce_emotional_weight, _coerce_int, _hotness_score
from memory2.embedder import Embedder
from memory2.tokenizer import tokenize_query

logger = logging.getLogger(__name__)

R = TypeVar("R")

_RRF_K = 60
_KEYWORD_RRF_WEIGHT = 0.5
# RRF 之后的乘性热度系数 β：final = rrf_score × (1 + β × hotness)。
# lane 内热度混合已置零，热度只在这个乘子里出现一次；β=0.05 是温和的形状调整，
# 不改变纯相关性的主排序，只让接近的候选中更热的一方靠前。
_POST_RRF_HOTNESS_BETA = 0.05
_DEFAULT_SPARSE_NORMALIZATION_K = 4.0
_DEFAULT_SPARSE_HOTNESS_ALPHA = 0.2
_KEYWORD_LIMIT_FLOOR = 30
_KEYWORD_LIMIT_MULTIPLIER = 2
_EMBED_TIMEOUT_S = 8.0
_LOW_CONFIDENCE_PHRASES = (
    "未在对话中明确记录",
    "无法凭记忆确认",
    "没有记录",
    "真的没有",
    "未找到",
    "不确定",
)


def _store_bm25_ready(store: MemoryStorage) -> bool:
    """BM25 路径运行时守卫（duck-typed，SQLite FTS5 与 PG pg_search 共用）。

    - SQLite（MemoryStore2）：`keyword_search_bm25` 存在且 `_fts_available` 为
      True（FTS5 建表成功）。
    - PG（PostgresMemoryStore）：调用 `ensure_bm25_ready()` 做 pg_search 运行时
      探测 + 幂等建索引，结果缓存于 backend；探测/建索引失败 fail-open 返回
      False，检索路径降级 OR-LIKE，绝不影响启动。
    - 两者都不满足返回 False，keyword lane 走 `keyword_search_summary` 保底。
    """
    if not callable(getattr(store, "keyword_search_bm25", None)):
        return False
    ensure = getattr(store, "ensure_bm25_ready", None)
    if callable(ensure):
        try:
            return bool(ensure())
        except Exception as e:  # noqa: BLE001 - fail-open，不抛给检索路径
            logger.debug("memory2 retrieve: ensure_bm25_ready 失败，降级 ILIKE: %s", e)
            return False
    return bool(getattr(store, "_fts_available", False))


class Retriever:
    INJECT_MAX_CHARS = 1200
    INJECT_MAX_FORCED = 3
    INJECT_MAX_EVENTS = 4
    INJECT_LINE_MAX = 180

    def __init__(
        self,
        store_for: MemoryStorage | Callable[[TenantContext], MemoryStorage],
        embedder: Embedder,
        top_k: int = 8,
        score_threshold: float = 0.45,
        score_thresholds: dict[str, float] | None = None,
        relative_delta: float = 0.06,
        inject_max_chars: int = 1200,
        inject_max_forced: int = 3,
        inject_max_procedure_preference: int = 4,
        inject_max_event_profile: int = 2,
        inject_line_max: int = 180,
        procedure_guard_enabled: bool = True,
        high_inject_delta: float = 0.15,
        hotness_alpha: float = 0.0,
        hotness_half_life_days: float = 14.0,
        hotness_beta: float = _POST_RRF_HOTNESS_BETA,
        sparse_normalization_k: float = _DEFAULT_SPARSE_NORMALIZATION_K,
        sparse_hotness_alpha: float = _DEFAULT_SPARSE_HOTNESS_ALPHA,
        rrf_k: int = _RRF_K,
        keyword_rrf_weight: float = _KEYWORD_RRF_WEIGHT,
        reranker: Reranker | None = None,
        run_db: Callable[..., Awaitable[Any]] | None = None,
    ) -> None:
        self._store_for = (
            store_for if callable(store_for) else lambda _tenant: store_for
        )
        self._embedder = embedder
        self._run_db_cb = run_db
        self._top_k = top_k
        self._score_threshold = score_threshold
        thresholds = score_thresholds or {}
        self._score_thresholds = {
            "procedure": float(thresholds.get("procedure", score_threshold)),
            "preference": float(thresholds.get("preference", score_threshold)),
            "event": float(thresholds.get("event", score_threshold)),
            "profile": float(thresholds.get("profile", score_threshold)),
        }
        self._relative_delta = max(0.0, float(relative_delta))
        self._inject_max_chars = max(200, int(inject_max_chars))
        self._inject_max_forced = max(1, int(inject_max_forced))
        self._inject_max_procedure_preference = max(
            1, int(inject_max_procedure_preference)
        )
        self._inject_max_event_profile = max(0, int(inject_max_event_profile))
        self._inject_line_max = max(60, int(inject_line_max))
        self._procedure_guard_enabled = bool(procedure_guard_enabled)
        self._high_inject_delta = max(0.0, float(high_inject_delta))
        self._hotness_alpha = max(0.0, min(1.0, float(hotness_alpha)))
        self._hotness_half_life_days = max(1.0, float(hotness_half_life_days))
        self._hotness_beta = max(0.0, float(hotness_beta))
        # sparse lane：BM25 归一化 K 与 hotness 融合 αs（C13 目标基线，§4.4）。
        self._sparse_normalization_k = max(1e-9, float(sparse_normalization_k))
        self._sparse_hotness_alpha = max(0.0, min(1.0, float(sparse_hotness_alpha)))
        # RRF 参数（默认与历史常量一致）；RRF 输入排名只来自 lane final（验收 4）。
        self._rrf_k = max(1, int(rrf_k))
        self._keyword_rrf_weight = max(0.0, float(keyword_rrf_weight))
        # 可选 reranker：RRF 截断后应用；默认 None（不进入默认路径）。
        self._reranker = reranker

    # 统一检索入口：recall_memory 和被动预检索都复用这条查库路径。
    async def retrieve(
        self,
        query: str,
        memory_types: list[str] | None = None,
        top_k: int | None = None,
        scope_channel: str | None = None,
        scope_chat_id: str | None = None,
        require_scope_match: bool = False,
        aux_queries: list[str] | None = None,
        score_threshold: float | None = None,
        time_start: datetime | None = None,
        time_end: datetime | None = None,
        keyword_enabled: bool = True,
        *,
        tenant: TenantContext,
    ) -> list[dict]:
        store = self._store_for(tenant)
        # 1. query 与辅助 query 一起进入向量 lane，避免多入口语义漂移。
        actual_top_k = self._top_k if top_k is None else max(1, int(top_k))
        actual_threshold = (
            self._score_threshold if score_threshold is None else float(score_threshold)
        )
        query_texts = _dedupe_texts([query, *(aux_queries or [])])
        vector_items = await self._retrieve_vector_lanes(
            query_texts,
            store=store,
            actual_top_k=actual_top_k,
            memory_types=memory_types,
            score_threshold=actual_threshold,
            scope_channel=scope_channel,
            scope_chat_id=scope_chat_id,
            require_scope_match=require_scope_match,
            time_start=time_start,
            time_end=time_end,
        )

        # 2. 关键词 lane 只用原始 query，保留用户字面命中的召回能力。
        # sparse 分数链：BM25 raw → query-local 归一化 → 与 hotness 融合为
        # sparse_final；字段全程分离（bm25_raw/bm25_normalized/hotness/sparse_final）。
        keyword_items: list[dict] = []
        if keyword_enabled:
            keyword_items = await self._run_db(
                self._retrieve_keyword_lane,
                query,
                store=store,
                actual_top_k=actual_top_k,
                memory_types=memory_types,
                scope_channel=scope_channel,
                scope_chat_id=scope_chat_id,
                require_scope_match=require_scope_match,
                time_start=time_start,
                time_end=time_end,
            )

        # 3. 最终只在这里做 RRF 融合，调用方不再各自拼召回列表。
        # RRF 输入排名：dense 侧按 _score_debug.final（dense final），sparse 侧按
        # sparse_final；cosine/BM25/hotness 原始数值不进入跨 lane 比较（验收 4）。
        # 热度不进 dense lane 内（alpha=0），RRF 融合后按 (1 + β×hotness) 乘性增强，
        # 对向量命中与 keyword 命中一视同仁；sparse lane 的热度已在 sparse_final
        # 内融合（αs），β 只是融合后的统一形状微调。
        items = _rrf_merge(
            vector_items,
            keyword_items,
            top_n=actual_top_k,
            k=self._rrf_k,
            keyword_weight=self._keyword_rrf_weight,
            hotness_beta=self._hotness_beta,
            hotness_half_life_days=self._hotness_half_life_days,
        )
        # 4. 可选 reranker（默认不注入）：对 RRF 截断后候选重排，最终注入序以
        # reranker 结果为准；失败由 reranker 自行 fail-open 回 RRF 顺序。
        if self._reranker is not None and items:
            items = await self._reranker.rerank(query, items)
        logger.debug(
            "memory2 retrieve: query=%r vector=%d keyword=%d fused=%d",
            query[:60],
            len(vector_items),
            len(keyword_items),
            len(items),
        )
        return items

    async def _run_db(self, fn: Callable[..., R], *args: Any, **kwargs: Any) -> R:
        # 无 run_db（legacy single-store / 测试 / sqlite 无 executor）时直接同步调。
        if self._run_db_cb is None:
            return fn(*args, **kwargs)
        return await self._run_db_cb(fn, *args, **kwargs)

    async def _retrieve_vector_lanes(
        self,
        query_texts: list[str],
        *,
        store: MemoryStorage,
        actual_top_k: int,
        memory_types: list[str] | None,
        score_threshold: float,
        scope_channel: str | None,
        scope_chat_id: str | None,
        require_scope_match: bool,
        time_start: datetime | None,
        time_end: datetime | None,
    ) -> list[dict]:
        if not query_texts:
            return []
        vectors = await self._embed_lanes(query_texts)
        if not vectors:
            return []
        hit_groups: list[list[dict]] = []
        try:
            hit_groups = await self._run_db(
                store.vector_search_batch,
                vectors,
                top_k=actual_top_k,
                memory_types=memory_types,
                score_threshold=score_threshold,
                scope_channel=scope_channel,
                scope_chat_id=scope_chat_id,
                require_scope_match=require_scope_match,
                hotness_alpha=self._hotness_alpha,
                hotness_half_life_days=self._hotness_half_life_days,
                time_start=time_start,
                time_end=time_end,
            )
        except Exception as e:
            logger.debug("memory2 retrieve: vector_search_batch failed: %s", e)

        seen: dict[str, dict] = {}
        if hit_groups:
            for hits in hit_groups:
                for hit in hits:
                    _remember_vector_hit(seen, hit)
            return list(seen.values())

        for vector in vectors:
            try:
                hits = await self._run_db(
                    store.vector_search,
                    query_vec=vector,
                    top_k=actual_top_k,
                    memory_types=memory_types,
                    score_threshold=score_threshold,
                    scope_channel=scope_channel,
                    scope_chat_id=scope_chat_id,
                    require_scope_match=require_scope_match,
                    hotness_alpha=self._hotness_alpha,
                    hotness_half_life_days=self._hotness_half_life_days,
                    time_start=time_start,
                    time_end=time_end,
                )
            except Exception as e:
                logger.debug("memory2 retrieve: vector_search failed: %s", e)
                continue
            for hit in hits:
                _remember_vector_hit(seen, hit)
        return list(seen.values())

    async def _embed_lanes(self, query_texts: list[str]) -> list[list[float]]:
        results = await asyncio.gather(
            *(
                asyncio.wait_for(
                    self._embedder.embed(text),
                    timeout=_EMBED_TIMEOUT_S,
                )
                for text in query_texts
            ),
            return_exceptions=True,
        )
        vectors: list[list[float]] = []
        for result in results:
            if isinstance(result, BaseException):
                logger.warning("memory2 retrieve: embed failed, fallback lane skipped: %s", result)
                continue
            vectors.append(cast(list[float], result))
        return vectors

    def _retrieve_keyword_lane(
        self,
        query: str,
        *,
        store: MemoryStorage,
        actual_top_k: int,
        memory_types: list[str] | None,
        scope_channel: str | None,
        scope_chat_id: str | None,
        require_scope_match: bool,
        time_start: datetime | None,
        time_end: datetime | None,
    ) -> list[dict]:
        # 统一 term 来源：jieba 搜索粒度分词优先（不可用时 regex bigram 回退）。
        terms = tokenize_query(query)
        if not terms:
            return []
        limit = max(_KEYWORD_LIMIT_FLOOR, actual_top_k * _KEYWORD_LIMIT_MULTIPLIER)
        # FTS5 BM25 (trigram tokenizer 需要 3+ 字符词条). 只在 query
        # 包含至少一个 3+ ASCII/CJK token 时启用, 否则纯 2 字 CJK
        # 查询会空结果, 直接走 OR-LIKE 保底.
        if _store_bm25_ready(store) and (
            re.search(r"[a-zA-Z0-9_]{3,}", query)
            or re.search(r"[\u4e00-\u9fff]{3,}", query)
        ):
            bm25_results = store.keyword_search_bm25(
                terms,
                memory_types=memory_types,
                limit=limit,
                time_start=time_start,
                time_end=time_end,
                scope_channel=scope_channel,
                scope_chat_id=scope_chat_id,
                require_scope_match=require_scope_match,
                raw_query=query,
            )
            if bm25_results:
                # BM25 原始分（keyword_score = -bm25，越高越匹配）进 bm25_raw，
                # 归一化与 hotness 融合在 enrich 中完成。
                for hit in bm25_results:
                    if isinstance(hit, dict) and hit.get("bm25_raw") is None:
                        hit["bm25_raw"] = float(hit.get("keyword_score") or 0.0)
                return self._enrich_sparse_hits(bm25_results)
        like_results = store.keyword_search_summary(
            terms,
            memory_types=memory_types,
            limit=limit,
            time_start=time_start,
            time_end=time_end,
            scope_channel=scope_channel,
            scope_chat_id=scope_chat_id,
            require_scope_match=require_scope_match,
        )
        return self._enrich_sparse_hits(like_results)

    def _enrich_sparse_hits(self, hits: list[dict]) -> list[dict]:
        """补齐 sparse 分数字段；hotness 复用与 post-RRF β 同一计算。"""
        return enrich_keyword_hits(
            [hit for hit in hits if isinstance(hit, dict)],
            normalization_k=self._sparse_normalization_k,
            hotness_alpha=self._sparse_hotness_alpha,
            hotness_half_life_days=self._hotness_half_life_days,
            hit_hotness_fn=_hit_hotness,
        )

    async def embed(self, query: str) -> list[float]:
        """仅做 embedding，不触发 vector_search。"""
        return await self._embedder.embed(query)

    async def retrieve_with_vec(
        self,
        query_vec: list[float],
        memory_types: list[str] | None = None,
        top_k: int | None = None,
        scope_channel: str | None = None,
        scope_chat_id: str | None = None,
        require_scope_match: bool = False,
        *,
        tenant: TenantContext,
    ) -> list[dict]:
        """复用已有 query_vec 做本地 vector_search，跳过 embedding 步骤。"""
        store = self._store_for(tenant)
        actual_top_k = self._top_k if top_k is None else max(1, int(top_k))
        items = await self._run_db(
            store.vector_search,
            query_vec=query_vec,
            top_k=actual_top_k,
            memory_types=memory_types,
            score_threshold=self._score_threshold,
            scope_channel=scope_channel,
            scope_chat_id=scope_chat_id,
            require_scope_match=require_scope_match,
            hotness_alpha=self._hotness_alpha,
            hotness_half_life_days=self._hotness_half_life_days,
        )
        logger.debug(f"memory2 retrieve_with_vec: hits={len(items)}")
        return items

    def build_injection_block(self, items: list[dict]) -> tuple[str, list[str]]:
        """单次流程：筛选条目 → 分段格式化 → 应用字符预算。"""
        selected, forced, norms, events = self._select_injection_sections(items)
        if not selected:
            return "", []

        parts = self._build_section_parts(forced, norms, events)
        return self._apply_char_budget(parts, has_forced=bool(forced))

    def _select_for_injection(self, items: list[dict]) -> list[dict]:
        selected, _forced, _norms, _events = self._select_injection_sections(items)
        return selected

    def _select_injection_sections(
        self,
        items: list[dict],
    ) -> tuple[list[dict], list[tuple[str, str]], list[tuple[str, str]], list[tuple[str, str]]]:
        """1. 按返回序筛选条目（不再重排） 2. 按段落准备格式化文本。

        items 已按 RRF（或 reranker）顺序返回：注入以该顺序为默认顺序（§4.4），
        不再按 score 重排覆盖 RRF 序。类型阈值门只作用于 dense 命中（cosine
        校准阈值）；keyword-only 命中的相关性证据是 sparse rank，不适用该量纲
        （见 _passes_type_threshold），由分区配额与字符预算兜底。
        """
        if not items:
            return [], [], [], []

        ordered_items = [i for i in items if isinstance(i, dict)]
        if not ordered_items:
            return [], [], [], []

        selected: list[dict] = []
        forced: list[tuple[str, str]] = []
        norms: list[tuple[str, str]] = []
        events: list[tuple[str, str]] = []
        forced_count = 0
        norm_count = 0
        event_count = 0
        for item in ordered_items:
            mtype = str(item.get("memory_type", "") or "")
            score = float(item.get("score", 0.0) or 0.0)
            extra = item.get("extra_json") or {}
            item_id = str(item.get("id", "") or "")
            summary = str(item.get("summary", "") or "").strip()
            happened_at = item.get("happened_at") or ""
            if (
                self._procedure_guard_enabled
                and mtype == "procedure"
                and extra.get("tool_requirement")
            ):
                if forced_count >= self._inject_max_forced:
                    continue
                forced_count += 1
                item["forced"] = True
                selected.append(item)
                if summary:
                    tool_req = extra.get("tool_requirement")
                    forced.append((item_id, f"- [{item_id}] {summary}（必须调用工具：{tool_req}）"))
                continue
            type_th = self._score_thresholds.get(mtype, self._score_threshold)
            if not self._passes_type_threshold(item, type_th):
                continue
            if mtype in ("procedure", "preference"):
                if norm_count >= self._inject_max_procedure_preference:
                    continue
                norm_count += 1
            elif mtype in ("event", "profile"):
                if event_count >= self._inject_max_event_profile:
                    continue
                event_count += 1
            else:
                continue
            selected.append(item)
            if not summary:
                continue
            confidence_label = ""
            if score < type_th + self._high_inject_delta:
                confidence_label = "有印象，不确定"
            item["confidence_label"] = confidence_label
            if mtype == "procedure":
                steps = extra.get("steps") or []
                if steps:
                    step_text = "；".join(str(s) for s in steps)
                    norms.append(
                        (
                            item_id,
                            f"- [{item_id}] {summary}{_format_memory_meta(item, mtype, confidence_label=confidence_label)}（步骤：{step_text}）",
                        )
                    )
                else:
                    norms.append(
                        (
                            item_id,
                            f"- [{item_id}] {summary}{_format_memory_meta(item, mtype, confidence_label=confidence_label)}",
                        )
                    )
            elif mtype == "preference":
                norms.append(
                    (
                        item_id,
                        f"- [{item_id}] {summary}{_format_memory_meta(item, mtype, confidence_label=confidence_label)}",
                    )
                )
            elif mtype in ("event", "profile"):
                ts = f"[{happened_at}] " if happened_at else ""
                events.append(
                    (
                        item_id,
                        f"- [{item_id}] {ts}{summary}{_format_memory_meta(item, mtype, confidence_label=confidence_label)}",
                    )
                )

        return selected, forced, norms, events

    def _passes_type_threshold(self, item: dict, type_th: float) -> bool:
        """类型阈值门（cosine 校准量纲）。

        dense 命中（含未带 _lane_ranks 的直连调用方，保持既有行为）按
        score（dense final）校验；keyword-only 命中的相关性证据是 BM25/RRF
        rank 而非 cosine，不适用本量纲（sparse_final ∈ [0,1) 与 cosine 阈值
        不可比），由 RRF top-k 选择、分区配额与字符预算兜底。
        """
        ranks = item.get("_lane_ranks")
        if isinstance(ranks, dict) and "dense" not in ranks:
            return True
        score = float(item.get("score", 0.0) or 0.0)
        return score >= type_th

    def _build_section_parts(
        self,
        forced: list[tuple[str, str]],
        norms: list[tuple[str, str]],
        events: list[tuple[str, str]],
    ) -> list[tuple[str, list[str]]]:
        parts: list[tuple[str, list[str]]] = []
        if forced:
            parts.append(
                (
                    "## 【强制约束】记忆规则（必须执行）\n"
                    + "\n".join(line for _, line in forced),
                    [item_id for item_id, _ in forced if item_id],
                )
            )
        if norms:
            parts.append(
                (
                    "## 【流程规范】用户偏好与规则\n"
                    + "\n".join(line for _, line in norms),
                    [item_id for item_id, _ in norms if item_id],
                )
            )
        if events:
            parts.append(
                (
                    "## 【相关历史】你与当前用户的过往对话（来自记忆检索，时间戳可信，可直接引用，不得自行否定；数字/金额/地名等具体值以记录为准，不得用常识替换；可根据上下文合理推断，如去某城市探望姐姐可推断姐姐住在该城市）\n"
                    + "\n".join(line for _, line in events),
                    [item_id for item_id, _ in events if item_id],
                )
            )
        return parts

    def _apply_char_budget(
        self,
        parts: list[tuple[str, list[str]]],
        *,
        has_forced: bool,
    ) -> tuple[str, list[str]]:
        if not parts:
            return "", []

        final_parts: list[str] = []
        injected_ids: list[str] = []
        seen_ids: set[str] = set()
        total = 0
        for idx, (part, part_ids) in enumerate(parts):
            add_len = len(part) + (2 if final_parts else 0)
            is_forced_part = idx == 0 and has_forced
            if total + add_len > self._inject_max_chars and not is_forced_part:
                continue
            final_parts.append(part)
            total += add_len
            for item_id in part_ids:
                if item_id and item_id not in seen_ids:
                    seen_ids.add(item_id)
                    injected_ids.append(item_id)
        return "\n\n".join(final_parts), injected_ids


def _dedupe_texts(texts: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for text in texts:
        normalized = (text or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
    return result


def _remember_vector_hit(
    seen: dict[str, dict],
    hit: dict,
) -> None:
    hit_id = _hit_id(hit)
    hit_score = _hit_score(hit)
    seen_score = _hit_score(seen.get(hit_id, {}))
    if hit_id and (hit_id not in seen or hit_score > seen_score):
        seen[hit_id] = hit


def _hit_id(item: dict) -> str:
    return str(item.get("id", "") or "")


def _hit_score(item: dict, fallback_key: str = "score") -> float:
    raw = item.get(fallback_key)
    if raw is None and fallback_key != "score":
        raw = item.get("score")
    return float(raw) if isinstance(raw, int | float) else 0.0


def _hit_hotness(item: dict, now: datetime, half_life_days: float) -> float:
    """从命中里取热度三件套算 hotness；数据缺失时返回 0（乘性因子退化为 1）。

    向量 lane 把 _reinforcement/_updated_at/_emotional_weight 放在 extra_json 里，
    keyword lane 为保持命中形状简单放在顶层，这里两种都认。无法解析时视为不热。
    """
    extra = item.get("extra_json")
    if isinstance(extra, dict):
        reinforcement = extra.get("_reinforcement")
        updated_at_raw = extra.get("_updated_at")
        emotional_weight = extra.get("_emotional_weight")
    else:
        reinforcement = item.get("_reinforcement")
        updated_at_raw = item.get("_updated_at")
        emotional_weight = item.get("_emotional_weight")
    if not updated_at_raw:
        return 0.0
    updated_at_str = str(updated_at_raw)
    try:
        updated_at = datetime.fromisoformat(updated_at_str)
    except (ValueError, TypeError):
        return 0.0
    return _hotness_score(
        _coerce_int(reinforcement, 1),
        updated_at,
        now,
        half_life_days,
        emotional_weight=_coerce_emotional_weight(emotional_weight),
    )


def _dense_final_score(item: dict) -> float:
    """dense lane 排名键：_score_debug.final（dense final），缺失回退 score。

    α=0（默认）时 final==semantic，与既有纯 cosine 排名兼容；α>0 时 final
    已含热度融合。
    """
    debug = item.get("_score_debug")
    if isinstance(debug, dict):
        raw = debug.get("final")
        if isinstance(raw, int | float):
            return float(raw)
    return _hit_score(item)


def _rrf_merge(
    vector_items: list[dict],
    keyword_items: list[dict],
    *,
    top_n: int,
    k: int = _RRF_K,
    keyword_weight: float = _KEYWORD_RRF_WEIGHT,
    hotness_beta: float = 0.0,
    hotness_half_life_days: float = 14.0,
) -> list[dict]:
    """RRF 融合两条 lane；输入排名只来自 lane final（C13 验收 4）。

    dense 侧排名键 = dense final（_score_debug.final，缺失回退 score）；
    sparse 侧排名键 = sparse_final（缺失时 stable sort 保持 lane 原序，兼容
    未改造的 fake store）。cosine、BM25 raw、hotness 的原始数值永不跨 lane
    比较，只以 rank 整数进入 1/(k+rank)。

    排序分 = rrf_score × (1 + hotness_beta × hotness)。默认 beta=0 即纯 RRF；
    传入 hotness_beta 后热度才参与排序（Retriever 生产路径传 _POST_RRF_HOTNESS_BETA）。
    平序由 rrf_score 与 item id 决出，不引入异构原始分。
    截断 top_n 之前增强，避免边界候选中"更热的那条"被提前截掉。
    """
    vec_rank: dict[str, int] = {}
    for index, item in enumerate(
        sorted(vector_items, key=_dense_final_score, reverse=True)
    ):
        item_id = _hit_id(item)
        if item_id and item_id not in vec_rank:
            vec_rank[item_id] = index + 1

    keyword_rank: dict[str, int] = {}
    for index, item in enumerate(
        sorted(keyword_items, key=sparse_final_of, reverse=True)
    ):
        item_id = _hit_id(item)
        if item_id and item_id not in keyword_rank:
            keyword_rank[item_id] = index + 1

    id_to_item: dict[str, dict] = {}
    for item in keyword_items:
        item_id = _hit_id(item)
        if item_id and item_id not in id_to_item:
            id_to_item[item_id] = dict(item)
    for item in vector_items:
        item_id = _hit_id(item)
        if item_id:
            id_to_item[item_id] = item

    now = datetime.now(timezone.utc)
    scored: list[tuple[str, float, float]] = []
    for item_id in set(vec_rank) | set(keyword_rank):
        rrf_score = 0.0
        if item_id in vec_rank:
            rrf_score += 1.0 / (k + vec_rank[item_id])
        if item_id in keyword_rank:
            rrf_score += keyword_weight / (k + keyword_rank[item_id])
        item = id_to_item.get(item_id, {})
        boosted = rrf_score * (
            1.0 + hotness_beta * _hit_hotness(item, now, hotness_half_life_days)
        )
        scored.append((item_id, boosted, rrf_score))

    # 排序用热度增强后的分数；rrf_score 字段仍记录融合前的原始 RRF 值。
    scored.sort(key=lambda entry: (entry[1], entry[2], entry[0]), reverse=True)
    result: list[dict] = []
    for item_id, _boosted, rrf_score in scored[:top_n]:
        item = dict(id_to_item[item_id])
        item["rrf_score"] = rrf_score
        item["_lane_ranks"] = {
            lane: rank
            for lane, rank in (
                ("dense", vec_rank.get(item_id)),
                ("sparse", keyword_rank.get(item_id)),
            )
            if rank is not None
        }
        result.append(item)
    return result


def _format_source_tag(source_ref: str | None) -> str:
    """从 source_ref（格式如 '["id1","id2"]#h:abc' 或 'channel@seq1-seq2#tag'）中提取消息 ID，
    返回供注入块附加的短标记，如 ' (src: telegram:<chat_id>:<message_id>)'。
    最多显示 2 个 ID，保持注入文本简洁。
    """
    if not source_ref:
        return ""
    raw = source_ref.split("#h:")[0].strip()
    ids: list[str] = []
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, list):
            ids = [str(i) for i in parsed if i]
    except (json.JSONDecodeError, ValueError):
        if raw:
            ids = [raw]
    if not ids:
        return ""
    shown = ids[:2]
    tag = ", ".join(shown)
    return f" (src: {tag})"


def _format_memory_meta(
    item: dict,
    memory_type: str,
    *,
    confidence_label: str = "",
) -> str:
    parts: list[str] = []
    if confidence_label:
        parts.append(confidence_label)
    happened_at_raw = item.get("happened_at")
    happened_at = _normalize_happened_at(happened_at_raw)
    if happened_at:
        parts.append(f"发生于: {happened_at}")
        age = _format_relative_age(happened_at_raw)
        if age:
            parts.append(age)
    source_ref = item.get("source_ref")
    src_tag = _format_source_tag(source_ref)
    if src_tag:
        parts.append("证据: 可回源原文")
        parts.append(src_tag.strip())
    else:
        parts.append("证据: 记忆摘要")
    if memory_type == "preference" and _looks_low_confidence_memory(item.get("summary", "")):
        parts.append("低置信线索: 不能单独证明历史细节")
    if not parts:
        return ""
    return "（" + "；".join(parts) + "）"


def _normalize_happened_at(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return text
    if dt.hour == 0 and dt.minute == 0 and dt.second == 0 and "T" not in text:
        return dt.strftime("%Y-%m-%d")
    return dt.strftime("%Y-%m-%d %H:%M")


def _format_relative_age(raw: object) -> str:
    text = str(raw or "").strip()
    if not text:
        return ""
    dt: datetime | None = None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        try:
            dt = datetime.fromisoformat(f"{text}T00:00:00")
        except ValueError:
            return ""
    now = datetime.now(dt.tzinfo)
    delta = now - dt
    if delta.days >= 1:
        return f"距今约 {delta.days} 天"
    hours = max(0, int(delta.total_seconds() // 3600))
    if hours >= 1:
        return f"距今约 {hours} 小时"
    minutes = max(0, int(delta.total_seconds() // 60))
    return f"距今约 {minutes} 分钟"


def _looks_low_confidence_memory(summary: object) -> bool:
    text = str(summary or "")
    return any(phrase in text for phrase in _LOW_CONFIDENCE_PHRASES)
