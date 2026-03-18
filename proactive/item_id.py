"""
proactive/item_id.py — 内容条目标识计算公共模块。

统一 item_id / source_key 的计算逻辑，供 proactive 主链路与兼容层共用，
避免各自维护私有副本。
"""

from __future__ import annotations

import hashlib
from urllib.parse import urlsplit, urlunsplit

from proactive.event import ContentEvent


def normalize_url(url: str | None) -> str:
    """标准化 URL：小写 scheme/host、去掉尾部 /、保留 query。"""
    if not url:
        return ""
    try:
        p = urlsplit(url.strip())
        scheme = (p.scheme or "").lower()
        netloc = (p.netloc or "").lower()
        path = p.path.rstrip("/")
        return urlunsplit((scheme, netloc, path, p.query, ""))
    except Exception:
        return (url or "").strip()


def compute_item_id(item: ContentEvent) -> str:
    """计算内容条目的唯一 ID。

    优先用 URL hash（u_ 前缀）；无 URL 则用内容指纹（h_ 前缀）。
    与 loop.py 旧版 _item_id() 保持完全相同的逻辑，便于状态文件兼容。
    """
    url = normalize_url(getattr(item, "url", None))
    if url:
        return "u_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:16]
    published_at = getattr(item, "published_at", None)
    raw = "|".join(
        [
            str(getattr(item, "source_type", "") or "").strip().lower(),
            str(getattr(item, "source_name", "") or "").strip().lower(),
            str(getattr(item, "title", "") or "").strip().lower(),
            str(getattr(item, "content", "") or "").strip().lower()[:200],
            published_at.isoformat() if published_at else "",
        ]
    )
    return "h_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def compute_source_key(item: ContentEvent) -> str:
    """计算内容条目的 source 标识符，格式：'type:name'（均小写）。"""
    source_type = str(getattr(item, "source_type", "") or "").strip().lower()
    source_name = str(getattr(item, "source_name", "") or "").strip().lower()
    return f"{source_type}:{source_name}"
