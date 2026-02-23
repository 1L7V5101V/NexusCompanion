"""
信息流订阅管理工具。
FeedSubscribeTool / FeedUnsubscribeTool / FeedListTool

用法示例：
  用户："我很关心 Paul Graham 的博客"
  → Agent 调用 feed_subscribe(name="Paul Graham", url="https://paulgraham.com/rss.html")

  用户："不太关注这个了"
  → Agent 调用 feed_unsubscribe(name="Paul Graham")
"""
from __future__ import annotations

from typing import Any

from agent.tools.base import Tool
from feeds.base import FeedSubscription
from feeds.store import FeedStore


class FeedSubscribeTool(Tool):
    name = "feed_subscribe"
    description = (
        "订阅一个 RSS 信息源。当用户表达对某个博客、新闻源感兴趣时调用。\n"
        "订阅成功后，该信息源会纳入主动推送的信息收集范围。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "信息源的人类可读名称，如 'Paul Graham' 或 'Hacker News'",
            },
            "url": {
                "type": "string",
                "description": "RSS/Atom feed 地址",
            },
            "note": {
                "type": "string",
                "description": "备注，记录用户为何关注（可选）",
            },
        },
        "required": ["name", "url"],
    }

    def __init__(self, store: FeedStore) -> None:
        self._store = store

    async def execute(self, **kwargs: Any) -> str:
        name: str = kwargs.get("name", "").strip()
        url: str = kwargs.get("url", "").strip()
        note: str | None = kwargs.get("note")

        if not name:
            return "错误：name 不能为空"
        if not url:
            return "错误：url 不能为空"

        # 检查是否已订阅同一地址
        for s in self._store.load():
            if s.url == url:
                return f"已经订阅过该地址：{s.name!r}（id: {s.id[:8]}），无需重复添加"

        sub = FeedSubscription.new(type="rss", name=name, url=url, note=note)
        self._store.add(sub)
        return f"已订阅 {name!r}（{url}），下次主动巡检时开始收集"


class FeedUnsubscribeTool(Tool):
    name = "feed_unsubscribe"
    description = (
        "取消订阅一个信息源。当用户说不再关心某人/某博客时调用。"
        "按名称模糊匹配，若匹配到多个会全部取消。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "信息源名称（支持模糊匹配）",
            },
        },
        "required": ["name"],
    }

    def __init__(self, store: FeedStore) -> None:
        self._store = store

    async def execute(self, **kwargs: Any) -> str:
        name: str = kwargs.get("name", "").strip()
        if not name:
            return "错误：name 不能为空"

        matches = self._store.find_by_name(name)
        if not matches:
            return f"没有找到名称包含 {name!r} 的订阅"

        for sub in matches:
            self._store.remove(sub.id)
        names = "、".join(f"「{s.name}」" for s in matches)
        return f"已取消订阅：{names}"


class FeedListTool(Tool):
    name = "feed_list"
    description = "列出当前所有订阅的 RSS 信息源"
    parameters = {"type": "object", "properties": {}}

    def __init__(self, store: FeedStore) -> None:
        self._store = store

    async def execute(self, **kwargs: Any) -> str:
        subs = self._store.load()
        if not subs:
            return "当前没有订阅任何信息源"

        lines = [f"RSS 订阅列表（共 {len(subs)} 个）："]
        for sub in subs:
            status = "启用" if sub.enabled else "停用"
            note_part = f"  备注: {sub.note}" if sub.note else ""
            lines.append(f"  [{status}] {sub.name}  {sub.url}{note_part}")
        return "\n".join(lines)
