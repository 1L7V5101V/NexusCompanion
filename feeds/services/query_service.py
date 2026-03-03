"""
feeds.services.query_service — 订阅内容查询服务。

职责：
- latest：最新条目
- search：关键词过滤
- summary：订阅概况
- catalog：分页目录
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from feeds.registry import FeedRegistry
from feeds.store import FeedStore

logger = logging.getLogger(__name__)


class QueryService:
    """
    订阅内容查询服务。

    将查询逻辑与工具层（FeedQueryTool）解耦，方便单独测试。
    """

    def __init__(self, store: FeedStore, registry: FeedRegistry) -> None:
        self._store = store
        self._registry = registry

    async def query(
        self,
        action: str,
        source: str = "",
        keyword: str = "",
        limit: int = 5,
        page: int = 1,
        page_size: int = 20,
    ) -> str:
        """
        统一查询入口。

        Args:
            action:    latest | search | summary | catalog
            source:    按来源名模糊过滤（可选）
            keyword:   search 关键词
            limit:     latest/search 返回条数（1-30）
            page:      catalog 页码（从 1 开始）
            page_size: catalog 每页条数（1-100）

        Returns:
            格式化字符串结果。
        """
        # 1. 规范化参数
        limit = max(1, min(30, limit))
        page = max(1, page)
        page_size = max(1, min(100, page_size))

        logger.info(
            "[query] action=%s source=%r keyword=%r limit=%d page=%d page_size=%d",
            action,
            source,
            keyword,
            limit,
            page,
            page_size,
        )

        # 2. 过滤订阅列表
        subs = self._store.list_enabled()
        if source:
            subs = [s for s in subs if source.lower() in s.name.lower()]
        if not subs:
            return "没有匹配的启用订阅"

        # 3. 抓取条目并排序（catalog 需要大量数据）
        fetch_limit = 300 if action == "catalog" else limit
        items = await self._registry.fetch_all(limit_per_source=fetch_limit)
        if source:
            items = [
                i for i in items if source.lower() in (i.source_name or "").lower()
            ]
        items.sort(
            key=lambda x: x.published_at or datetime(1970, 1, 1, tzinfo=timezone.utc),
            reverse=True,
        )

        # 4. 按 action 分发
        if action == "summary":
            return self._format_summary(subs, items)
        if action == "search":
            return self._format_search(items, keyword, limit)
        if action == "catalog":
            return self._format_catalog(items, source, page, page_size)
        if action in ("latest", "search"):
            return self._format_latest(items, limit)

        return "错误：action 必须是 latest|search|summary|catalog"

    # ------------------------------------------------------------------
    # 格式化方法
    # ------------------------------------------------------------------

    def _format_summary(self, subs: list[Any], items: list[Any]) -> str:
        source_names = sorted({i.source_name for i in items if i.source_name})
        if not source_names:
            names = "（无）"
        else:
            max_names = 50
            shown = source_names[:max_names]
            names = "、".join(shown)
            if len(source_names) > max_names:
                names += f" …（其余 {len(source_names) - max_names} 个省略）"
        return (
            f"订阅概况：sources={len(subs)} items={len(items)} 来源={names}。"
            "如需逐条订阅 URL 与启用状态，请使用 feed_manage(action=list)。"
        )

    def _format_search(self, items: list[Any], keyword: str, limit: int) -> str:
        if not keyword:
            return "错误：search 需要 keyword"
        kw = keyword.lower()
        matched = [
            i
            for i in items
            if kw in (i.title or "").lower() or kw in (i.content or "").lower()
        ]
        return self._format_latest(matched, limit)

    def _format_catalog(
        self, items: list[Any], source: str, page: int, page_size: int
    ) -> str:
        total = len(items)
        start = (page - 1) * page_size
        end = start + page_size
        if start >= total and total > 0:
            return json.dumps(
                {
                    "action": "catalog",
                    "source": source or None,
                    "page": page,
                    "page_size": page_size,
                    "total": total,
                    "has_more": False,
                    "next_page": None,
                    "items": [],
                    "error": "page out of range",
                },
                ensure_ascii=False,
            )
        picked = items[start:end]
        payload_items = [
            {
                "source": i.source_name,
                "title": i.title or "(无标题)",
                "url": i.url or "",
                "published_at": i.published_at.isoformat() if i.published_at else None,
            }
            for i in picked
        ]
        has_more = end < total
        return json.dumps(
            {
                "action": "catalog",
                "source": source or None,
                "page": page,
                "page_size": page_size,
                "total": total,
                "has_more": has_more,
                "next_page": (page + 1) if has_more else None,
                "items": payload_items,
            },
            ensure_ascii=False,
        )

    def _format_latest(self, items: list[Any], limit: int) -> str:
        picked = items[:limit]
        if not picked:
            return "没有找到匹配条目"
        lines = []
        for i in picked:
            ts = (
                i.published_at.astimezone().strftime("%Y-%m-%d %H:%M")
                if i.published_at
                else "未知时间"
            )
            title = i.title or "(无标题)"
            lines.append(f"- [{i.source_name}] {title} ({ts})")
            if i.url:
                lines.append(f"  {i.url}")
        return "\n".join(lines)
