"""
DefaultMemoryRetrievalPipeline — Agentic RAG 三步编排

Router → RetrievalSandbox → Evaluator 质检闭环
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Literal

from agent.core.types import RetrievalTrace
from agent.looping.ports import MemoryServices
from agent.retrieval.evaluator import Evaluator
from agent.retrieval.fusion import ScoredItem
from agent.retrieval.planner import QueryPlanner
from agent.retrieval.protocol import (
    MemoryRetrievalPipeline,
    RetrievalRequest,
    RetrievalResult,
)
from agent.retrieval.sandbox import RetrievalSandbox
from core.memory.engine import MemoryQueryFilters, MemoryScope

if TYPE_CHECKING:
    from agent.provider import LLMProvider

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3


class AgenticRAGPipeline(MemoryRetrievalPipeline):
    """Agentic RAG 检索管线——Router → Sandbox → Evaluator。

    分层:
    1. Router: 规则 + light_provider 决策检索源
    2. RetrievalSandbox: 并行检索 + RRF 融合（隔离草稿区）
    3. Evaluator: light_provider 结构化评分 + 迭代重试

    不在 ToolRegistry 中，LLM 不可见。所有中间结果留在 Sandbox 内。
    """



    def __init__(
        self,
        memory: MemoryServices,
        light_provider: LLMProvider | None = None,
        web_search_fn: Callable[[str], Awaitable[str]] | None = None,
        light_model: str = "",
        router_mode: Literal["rule", "llm"] = "rule",
    ) -> None:
        self._memory = memory
        self._light_provider = light_provider

        self._planner = QueryPlanner(
            light_provider=light_provider,
            light_model=light_model,
            router_mode=router_mode,
        )
        self._evaluator = Evaluator(light_provider=light_provider, light_model=light_model)
        self._sandbox = RetrievalSandbox(
            rachael_engine=memory.engines.get("rachael"),
            vector_engine=memory.engines.get("default"),
            web_search_fn=web_search_fn,
        )

    async def retrieve(self, request: RetrievalRequest) -> RetrievalResult:
        """三步编排: 路由 → 检索 → 质检。"""

        # ── 0. 无引擎 → 空 ────────────────────────────────────
        if not self._memory.engines:
            return RetrievalResult(block="", trace=None)

        # ── 1. 后向兼容: 单引擎 + 无 light_provider → 旧行为 ──
        if len(self._memory.engines) == 1 and self._light_provider is None:
            engine = next(iter(self._memory.engines.values()))
            try:
                result = await engine.query(self._build_query(request))
                return RetrievalResult(
                    block=result.text_block,
                    trace=_build_trace(result),
                )
            except Exception as e:
                logger.error("单引擎检索失败: %s", e)
                return RetrievalResult(block="", trace=None)

        # ── 2. Agentic RAG 管线 ──────────────────────────────
        query = request.message
        scope = MemoryScope(
            session_key=request.session_key,
            channel=request.channel,
            chat_id=request.chat_id,
        )
        context: dict[str, Any] = {
            "history": request.history,
            "session_metadata": request.session_metadata,
        }
        filters = MemoryQueryFilters(hints=dict(request.extra or {}))
        ts = request.timestamp

        # 2a. Router: 决策检索源
        plan = await self._planner.classify(query)
        if not plan.sources:
            return RetrievalResult(block="", trace=None)

        logger.info(
            "Router: sources=%s method=%s reason=%s",
            plan.sources, plan.method, plan.reason,
        )

        # 2b. Sandbox + Evaluator 迭代
        eval_result = None
        block = ""
        current_query = query

        for retry in range(_MAX_RETRIES):
            block = await self._sandbox.retrieve(
                plan.sources,
                query=current_query,
                scope=scope,
                context=context,
                timestamp=ts,
                filters=filters,
            )

            if not block:
                break

            # 日志：本轮检索内容（截取前 300 字，方便判断质检结果）
            _block_preview = block[:300].replace("\n", " ")
            logger.info(
                "检索内容 (第%d轮, %s): %s",
                retry + 1,
                plan.sources,
                _block_preview + ("..." if len(block) > 300 else ""),
            )

            # Evaluator 质检（传入 Router 决策，供按场景调整严格度）
            if self._light_provider is not None:
                eval_result = await self._evaluator.evaluate(
                    query, block,
                    router_sources=plan.sources,
                    router_reason=plan.reason,
                )
                if eval_result.verified:
                    logger.info("检索通过质检 (第%d轮)", retry + 1)
                    return RetrievalResult(block=block, verified=True)

                logger.info(
                    "检索未通过质检 (第%d轮): rel=%.2f com=%.2f %s",
                    retry + 1,
                    eval_result.relevance,
                    eval_result.completeness,
                    eval_result.missing_info,
                )

                # 最后一轮不再重试
                if retry >= _MAX_RETRIES - 1:
                    break

                # Query Rewrite
                current_query = await self._evaluator._rewrite_query(
                    query, eval_result.missing_info,
                )
                if current_query == query:
                    # 改写没变化 → 本轮是最后一次
                    break
            else:
                # 无 Evaluator: 信任结果，直接返回
                return RetrievalResult(block=block, verified=False)

        # 2c. 重试耗尽 → meta block
        meta = self._build_meta_block(query, plan.sources, retry_count=_MAX_RETRIES)
        return RetrievalResult(block=meta, verified=False)

    # ── 辅助 ──────────────────────────────────────────────────

    @staticmethod
    def _build_query(request: RetrievalRequest) -> MemoryQuery:
        from core.memory.engine import MemoryQuery, MemoryQueryFilters, MemoryScope

        return MemoryQuery(
            text=request.message,
            intent="context",
            scope=MemoryScope(
                session_key=request.session_key,
                channel=request.channel,
                chat_id=request.chat_id,
            ),
            context={
                "history": request.history,
                "session_metadata": request.session_metadata,
            },
            filters=MemoryQueryFilters(hints=dict(request.extra or {})),
            timestamp=request.timestamp,
        )

    @staticmethod
    def _build_meta_block(query: str, sources: list[str], retry_count: int) -> str:
        return (
            "[检索说明]\n"
            f"系统已从 {sources} 源尝试 {retry_count} 轮检索（含 query 重写），"
            "未能找到高置信度相关信息。\n"
            "若你认为需要进一步搜索，请使用 web_search 或 recall_memory。\n"
            "---"
        )


# 为向后兼容保留别名
DefaultMemoryRetrievalPipeline = AgenticRAGPipeline


# ── Trace 构建 ──────────────────────────────────────────────


def _build_trace(result: Any) -> RetrievalTrace | None:
    """从 MemoryQueryResult 构建 trace（单引擎向后兼容用）。"""
    trace = getattr(result, "trace", None)
    records = getattr(result, "records", None) or []
    raw = getattr(result, "raw", None) or {}
    text_block = getattr(result, "text_block", None) or ""

    if not trace and not records and not text_block:
        return None
    return RetrievalTrace(
        gate_type=str(trace.get("gate_type") or "") if isinstance(trace, dict) else None,
        route_decision=str(trace.get("route_decision") or "") if isinstance(trace, dict) else None,
        rewritten_query=str(raw.get("rewritten_query") or "") or None,
        injected_count=sum(1 for r in records if getattr(r, "injected", False)),
        raw=raw.get("retrieval_event"),
    )
