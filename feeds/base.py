"""主仓库仍保留的最小内容视图类型。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass
class FeedItem:
    source_name: str  # "Paul Graham's Blog"
    source_type: str  # "content" 子来源类型，如 rss / github / steam
    title: str | None
    content: str  # 正文摘要（截断后）
    url: str | None
    author: str | None
    published_at: datetime | None
    display_text: str = ""  # MCP 侧提供的预格式化展示文本；为空时由 _format_items 自动拼
