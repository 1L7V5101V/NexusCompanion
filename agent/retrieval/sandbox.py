"""
RetrievalSandbox — 隔离的检索沙箱

职责:
1. 按 source_list 并行调度各检索源（Graph/Vector/Web）
2. 单源 → 直出，多源 → RRF Fusion
3. 原始检索结果、中间产物、失败的 query rewrite 结果全部留在 Sandbox 内部
   只有最终合格的 block 才被传递到主生成上下文

和 ToolRegistry 的关系: Sandbox 内部直接调用引擎 API 和 HTTP 客户端，
不经过 LLM 可见的工具层。LLM 在生成阶段看到的 recall_memory/web_search
是独立的、显式的工具调用，和这里的系统级检索无关。
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from agent.retrieval.fusion import FusionEngine, ScoredItem
from core.memory.engine import MemoryQuery, MemoryQueryFilters, MemoryScope

if TYPE_CHECKING:
    from core.memory.engine import MemoryEngine

logger = logging.getLogger(__name__)

_WEB_SEARCH_TIMEOUT_S = 15.0
_MAX_WEB_RESULTS = 6


class RetrievalSandbox:
    """检索沙箱。管理多个检索源的并行调度和结果融合。

    Args:
        rachael_engine: Graph 引擎实例，可选
        vector_engine: Vector RAG 引擎实例，可选
        web_search_fn: 异步 web 搜索函数，接收 query 返回 JSON 字符串，可选
    """

    def __init__(
        self,
        rachael_engine: MemoryEngine | None = None,
        vector_engine: MemoryEngine | None = None,
        web_search_fn: Callable[[str], Awaitable[str]] | None = None,
    ) -> None:
        self._rachael = rachael_engine
        self._vector = vector_engine
        self._web_search_fn = web_search_fn
        self._fusion = FusionEngine()

    # ── 主入口 ────────────────────────────────────────────────

    async def retrieve(
        self,
        source_list: list[str],
        *,
        query: str,
        scope: MemoryScope,
        context: dict[str, Any],
        timestamp: datetime | None = None,
        filters: MemoryQueryFilters | None = None,
    ) -> str:
        """并行调度指定源，返回融合后的文本 block。

        Args:
            source_list: Planner 输出的源列表
            query: 用户原始问题
            scope: 会话范围（session_key/channel/chat_id）
            context: 额外上下文（history, session_metadata 等）
            timestamp: 查询时间戳
            filters: 可选的检索过滤器

        Returns:
            格式化后的检索结果 block。单源直接注入，多源 RRF 融合。
        """
        # 1. 收集各源 coroutine
        coros: dict[str, asyncio.Task] = {}

        for src in source_list:
            if src == "graph":
                coros[src] = asyncio.ensure_future(
                    self._query_graph(query, scope, context, timestamp, filters)
                )
            elif src == "vector":
                coros[src] = asyncio.ensure_future(
                    self._query_vector(query, scope, context, timestamp, filters)
                )
            elif src == "web":
                coros[src] = asyncio.ensure_future(
                    self._query_web(query)
                )

        if not coros:
            return ""

        # 2. 并行执行（各自带超时）
        results: dict[str, list[ScoredItem] | None] = {}
        for src, task in coros.items():
            timeout = _WEB_SEARCH_TIMEOUT_S if src == "web" else 5.0
            try:
                items = await asyncio.wait_for(task, timeout=timeout)
                if items:
                    results[src] = items
            except asyncio.TimeoutError:
                logger.warning("检索源 %s 超时(%ss)", src, timeout)
            except Exception as e:
                logger.warning("检索源 %s 失败: %s", src, e)

        if not results:
            return ""

        # 3. 单源 vs 多源
        if len(results) == 1:
            src, items = next(iter(results.items()))
            return self._fusion.format_block([
                ScoredItem(source=src, content=item.content, score=item.score)
                for item in items  # type: ignore[union-attr]
            ])

        return self._fusion.format_block(self._fusion.fuse(results))

    # ── 各源检索 ──────────────────────────────────────────────

    async def _query_graph(
        self,
        query: str,
        scope: MemoryScope,
        context: dict[str, Any],
        timestamp: datetime | None,
        filters: MemoryQueryFilters | None,
    ) -> list[ScoredItem] | None:
        if self._rachael is None:
            return None
        try:
            result = await self._rachael.query(MemoryQuery(
                text=query,
                intent="context",
                scope=scope,
                context=context,
                filters=filters or MemoryQueryFilters(),
                timestamp=timestamp,
            ))
            return self._records_to_items(result.records, "graph")
        except Exception as e:
            logger.warning("Graph 检索失败: %s", e)
            return None

    async def _query_vector(
        self,
        query: str,
        scope: MemoryScope,
        context: dict[str, Any],
        timestamp: datetime | None,
        filters: MemoryQueryFilters | None,
    ) -> list[ScoredItem] | None:
        if self._vector is None:
            return None
        try:
            result = await self._vector.query(MemoryQuery(
                text=query,
                intent="context",
                scope=scope,
                context=context,
                filters=filters or MemoryQueryFilters(),
                timestamp=timestamp,
            ))
            return self._records_to_items(result.records, "vector")
        except Exception as e:
            logger.warning("Vector 检索失败: %s", e)
            return None

    async def _query_web(self, query: str) -> list[ScoredItem] | None:
        if self._web_search_fn is None:
            return None
        try:
            raw = await self._web_search_fn(query)
            return self._parse_web_results(raw)
        except Exception as e:
            logger.warning("Web 检索失败: %s", e)
            return None

    # ── 数据转换 ──────────────────────────────────────────────

    @staticmethod
    def _records_to_items(
        records: list[Any],
        source: str,
    ) -> list[ScoredItem] | None:
        """将 MemoryRecord 列表转为 ScoredItem 列表。"""
        if not records:
            return None
        items: list[ScoredItem] = []
        for r in records:
            summary = getattr(r, "summary", "") or getattr(r, "text", "") or ""
            score = float(getattr(r, "score", 0) or 0)
            if summary:
                items.append(ScoredItem(
                    source=source,
                    content=summary,
                    score=score,
                ))
        return items or None

    @staticmethod
    def _parse_web_results(raw: str) -> list[ScoredItem] | None:
        """从 Exa MCP 的 JSON 响应中解析 ScoredItem。"""
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Web 检索结果 JSON 解析失败")
            return None

        # 支持两种格式: 直接结果列表 或 嵌套在 result.text 里
        results = data.get("results") or data.get("result", "")

        if isinstance(results, str):
            # 单块文本结果 → 拆成一条
            return [ScoredItem(source="web", content=results[:500], score=0.5)]

        if isinstance(results, list):
            items: list[ScoredItem] = []
            for i, r in enumerate(results):
                if isinstance(r, dict):
                    title = r.get("title", "") or ""
                    snippet = r.get("text") or r.get("snippet") or r.get("content") or ""
                    content = f"{title}: {snippet}" if title else snippet
                    if content:
                        items.append(ScoredItem(
                            source="web",
                            content=content[:300],
                            score=1.0 - i * 0.05,  # 按排名递减
                        ))
            return items or None

        return None
