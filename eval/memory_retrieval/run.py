"""离线评测核心 runner（ADR-7）：计算 A 基线检索指标。

流程：合成数据集 → MemoryStore2（SQLite）+ Retriever（真实 sparse BM25 /
RRF 分数链）→ 每 query 检索 → 注入筛选 → 指标摘要 + 每 query 明细。
P95 只记录观测值，不做阈值断言。
"""

from __future__ import annotations

import logging
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from infra.storage.interfaces import TenantContext
from memory2.retriever import Retriever
from memory2.store import MemoryStore2

from eval.memory_retrieval.dataset import RetrievalItem, RetrievalQuery, build_dataset
from eval.memory_retrieval.embedder_stub import EmbedderStub
from eval.memory_retrieval.metrics import QueryEval, degrade_report, summarize

logger = logging.getLogger("eval.memory_retrieval")


@dataclass(frozen=True)
class EvalConfig:
    seed: int = 202600901
    n_items: int = 120
    n_queries: int = 40
    top_k: int = 8
    score_threshold: float = 0.3
    inject_max_chars: int = 4000
    inject_max_procedure_preference: int = 6
    inject_max_event_profile: int = 8
    embed_dim: int = 256
    # 消融注入点：与默认路径分离开关，均为 query -> ... 的可调用。
    aux_queries_for: object = field(default_factory=lambda: None)  # -> list[str]
    rewrite_query: object = field(default_factory=lambda: None)  # -> str
    reranker_for: object = field(default_factory=lambda: None)  # -> Reranker|None


@dataclass(frozen=True)
class RunResult:
    mode: str
    summary: dict[str, float | int]
    queries: list[QueryEval]
    cost: dict[str, int]
    degrade: dict[str, float] | None = None


async def run_baseline(
    *,
    items: list[RetrievalItem],
    queries: list[RetrievalQuery],
    config: EvalConfig | None = None,
    embedder: EmbedderStub | None = None,
    failing_embedder: bool = False,
    mode: str = "A",
) -> RunResult:
    """跑一轮评测；embedder 可覆盖（消融 stub），失败仿真置 true。"""
    cfg = config or EvalConfig()
    tenant = TenantContext(tenant_id="eval")

    store = MemoryStore2(Path(tempfile.mkdtemp()) / "eval.db", vec_dim=cfg.embed_dim)
    stub_embedder = embedder if embedder is not None else EmbedderStub(cfg.embed_dim)
    # store.upsert_item 生成自己的 content-hash id，与 dataset 的条目 id 不同；
    # 按 upsert 返回 id 建 dataset_id -> store_id 映射，gold 才能与命中对齐。
    id_map: dict[str, str] = {}
    for item in items:
        embedding = None
        if not failing_embedder:
            embedding = await stub_embedder.embed(item.summary)
        write_result = str(
            store.upsert_item(
                "event",
                item.summary,
                embedding=embedding,
                extra=dict(item.extra),
            )
        )
        store_id = write_result.split(":", 1)[1] if ":" in write_result else write_result
        id_map[item.id] = store_id

    active_embedder = embedder if embedder is not None else EmbedderStub(cfg.embed_dim)
    if failing_embedder:
        from eval.memory_retrieval.embedder_stub import FailingEmbedder

        active_embedder = FailingEmbedder()

    retriever = Retriever(
        store,
        active_embedder,
        top_k=cfg.top_k,
        score_threshold=cfg.score_threshold,
        score_thresholds={
            "procedure": cfg.score_threshold,
            "preference": cfg.score_threshold,
            "event": cfg.score_threshold,
            "profile": cfg.score_threshold,
        },
        inject_max_chars=cfg.inject_max_chars,
        inject_max_forced=2,
        inject_max_procedure_preference=cfg.inject_max_procedure_preference,
        inject_max_event_profile=cfg.inject_max_event_profile,
        inject_line_max=180,
        procedure_guard_enabled=False,
        hotness_beta=0.05,
        sparse_normalization_k=4.0,
        sparse_hotness_alpha=0.2,
        rrf_k=60,
        keyword_rrf_weight=0.5,
    )

    evals: list[QueryEval] = []
    token_calls = 0
    for query in queries:
        aux = []
        if callable(cfg.aux_queries_for):
            aux = list(cfg.aux_queries_for(query)) or []
        qtext = query.text
        if callable(cfg.rewrite_query):
            rewritten = cfg.rewrite_query(query)
            if rewritten and rewritten != query.text:
                qtext = rewritten
        resolver = None
        if callable(cfg.reranker_for):
            resolver = cfg.reranker_for(query)
        if resolver is not None:
            retriever._reranker = resolver

        t0 = time.perf_counter()
        hits = await retriever.retrieve(
            qtext,
            top_k=cfg.top_k + 12,  # 候选宽松，截断由 RRF 内做
            score_threshold=cfg.score_threshold,
            aux_queries=aux,
            tenant=tenant,
            keyword_enabled=True,
        )
        latency = time.perf_counter() - t0
        token_calls += 1 + len(aux)

        _text, injected_ids = retriever.build_injection_block(hits)
        evals.append(
            QueryEval(
                query_id=query.id,
                hit_ids=[str(hit.get("id", "")) for hit in hits],
                injected_ids=[str(i) for i in injected_ids],
                gold={id_map.get(gid, gid): grade for gid, grade in query.gold.items()},
                latency_s=latency,
            )
        )

    summary = summarize(evals)
    cost = {"embed_calls": token_calls, "llm_calls": 0}
    return RunResult(mode=mode, summary=summary, queries=evals, cost=cost)


async def run_degrade(
    *,
    items: list[RetrievalItem],
    queries: list[RetrievalQuery],
    config: EvalConfig | None = None,
) -> tuple[RunResult, dict[str, float]]:
    """正常 vs embed 失败两轮，计算 keyword-only 退化幅度。"""
    base = await run_baseline(items=items, queries=queries, config=config, mode="A")
    degraded = await run_baseline(
        items=items,
        queries=queries,
        config=config,
        failing_embedder=True,
        mode="degrade",
    )
    delta = degrade_report(base.queries, degraded.queries)
    return degraded, delta