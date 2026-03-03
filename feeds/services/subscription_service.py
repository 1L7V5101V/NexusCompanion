"""
feeds.services.subscription_service — 订阅/取消订阅业务服务。

职责：
- 添加订阅（重复检测 + 写 store）
- 取消订阅（模糊匹配 + 写 store）
- 触发 SourceScorer 增量打分（异步后台）
"""

from __future__ import annotations

import logging
from typing import Any

from feeds.base import FeedSubscription
from feeds.store import FeedStore

logger = logging.getLogger(__name__)


class SubscriptionService:
    """
    订阅管理服务。

    将订阅/取消业务逻辑与 Tool 层解耦，方便单独测试与复用。
    """

    def __init__(self, store: FeedStore, source_scorer: Any | None = None) -> None:
        self._store = store
        self._source_scorer = source_scorer

    def set_scorer(self, source_scorer: Any | None) -> None:
        """事后注入 SourceScorer（主循环构建后调用）。"""
        self._source_scorer = source_scorer

    # ------------------------------------------------------------------
    # 订阅
    # ------------------------------------------------------------------

    async def subscribe(
        self,
        name: str,
        url: str,
        source_type: str = "rss",
        note: str | None = None,
    ) -> str:
        """
        添加订阅。

        Returns:
            操作结果字符串（供工具层直接返回给 LLM）。
        """
        # 1. 基本校验
        if not name:
            return "错误：name 不能为空"
        if not url:
            return "错误：url 不能为空"

        # 2. 重复检测
        for s in self._store.load():
            if s.url == url:
                return f"已经订阅过该地址：{s.name!r}（id: {s.id[:8]}），无需重复添加"

        # 3. 写入 store 并触发后台打分
        sub = FeedSubscription.new(type=source_type, name=name, url=url, note=note)
        self._store.add(sub)
        self._schedule_score(sub)

        return f"已订阅 {name!r}（类型={source_type} {url}），下次主动巡检时开始收集"

    def _schedule_score(self, sub: FeedSubscription) -> None:
        """后台调度新源评分（失败不影响主流程）。"""
        if self._source_scorer is None:
            return
        try:
            import asyncio

            asyncio.ensure_future(self._trigger_score_new(sub))
        except Exception as e:
            logger.warning("[subscription] source_scorer 增量打分调度失败: %s", e)

    async def _trigger_score_new(self, sub: FeedSubscription) -> None:
        """后台异步触发新源打分，写入 SourceScorer 缓存。"""
        try:
            await self._source_scorer.score_new_source(sub, memory_text="")
        except Exception as e:
            logger.warning("[subscription] source_scorer 增量打分失败: %s", e)

    # ------------------------------------------------------------------
    # 取消订阅
    # ------------------------------------------------------------------

    async def unsubscribe(self, name: str) -> str:
        """
        按名称模糊匹配并取消所有命中的订阅。

        Returns:
            操作结果字符串。
        """
        # 1. 校验
        if not name:
            return "错误：name 不能为空"

        # 2. 模糊匹配
        matches = self._store.find_by_name(name)
        if not matches:
            return f"没有找到名称包含 {name!r} 的订阅"

        # 3. 逐条删除并使 scorer 缓存失效
        for sub in matches:
            self._store.remove(sub.id)
            self._invalidate_score(sub)

        names = "、".join(f"「{s.name}」" for s in matches)
        return f"已取消订阅：{names}"

    def _invalidate_score(self, sub: FeedSubscription) -> None:
        """使 SourceScorer 中该订阅的缓存失效（同步，直接 pop）。"""
        if self._source_scorer is None:
            return
        try:
            self._source_scorer.invalidate_source(sub.id)
        except Exception as e:
            logger.warning("[subscription] source_scorer 缓存失效失败: %s", e)

    # ------------------------------------------------------------------
    # 列表
    # ------------------------------------------------------------------

    def list_subscriptions(self) -> str:
        """列出所有订阅，格式化为可读字符串。"""
        subs = self._store.load()
        if not subs:
            return "当前没有订阅任何信息源"
        lines = [f"RSS 订阅列表（共 {len(subs)} 个）："]
        for sub in subs:
            status = "启用" if sub.enabled else "停用"
            note_part = f"  备注: {sub.note}" if sub.note else ""
            lines.append(f"  [{status}] {sub.name}  {sub.url}{note_part}")
        return "\n".join(lines)
