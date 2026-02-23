"""
FeedRegistry — 动态信息源注册与批量拉取。

每次 fetch_all() 都重新从 FeedStore 读取订阅，
确保用户新增/删除订阅后立即生效，无需重启。

注册方式（对标 MessagePushTool.register_channel）：
    registry.register_source_type("rss", lambda sub: RSSFeedSource(sub))
"""
from __future__ import annotations

import asyncio
import logging
from typing import Callable

from feeds.base import FeedItem, FeedSource, FeedSubscription
from feeds.store import FeedStore

logger = logging.getLogger(__name__)


class FeedRegistry:
    def __init__(self, store: FeedStore) -> None:
        self._store = store
        # type_name -> factory(sub) -> FeedSource
        self._factories: dict[str, Callable[[FeedSubscription], FeedSource]] = {}

    def register_source_type(
        self,
        type_name: str,
        factory: Callable[[FeedSubscription], FeedSource],
    ) -> None:
        """注册一种信息源类型的构造工厂。"""
        self._factories[type_name] = factory
        logger.debug(f"FeedRegistry: 注册类型 {type_name!r}")

    async def fetch_all(self, limit_per_source: int = 3) -> list[FeedItem]:
        """从所有启用的订阅中并发拉取内容。单个 source 失败不影响其他。"""
        subs = self._store.list_enabled()
        if not subs:
            return []

        sources: list[FeedSource] = []
        for sub in subs:
            factory = self._factories.get(sub.type)
            if factory is None:
                logger.warning(f"FeedRegistry: 未知类型 {sub.type!r}，跳过 {sub.name!r}")
                continue
            try:
                sources.append(factory(sub))
            except Exception as e:
                logger.warning(f"FeedRegistry: 构造 source {sub.name!r} 失败: {e}")

        if not sources:
            return []

        results = await asyncio.gather(
            *[src.fetch(limit_per_source) for src in sources],
            return_exceptions=True,
        )

        items: list[FeedItem] = []
        for src, result in zip(sources, results):
            if isinstance(result, Exception):
                logger.warning(f"FeedRegistry: {src.name!r} 拉取失败: {result}")
            else:
                items.extend(result)

        return items
