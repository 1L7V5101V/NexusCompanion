"""原始会话消息查询工具。"""

from __future__ import annotations

import json
from typing import Any

from agent.tools.base import Tool
from session.store import SessionStore

_MAX_CONTEXT = 5


class FetchMessagesTool(Tool):
    name = "fetch_messages"
    description = "按消息 ID 列表精确拉取原始对话内容。通常在找到 memory 条目后追溯 source_ref 使用。支持 context 参数扩展前后文。"
    parameters = {
        "type": "object",
        "properties": {
            "ids": {
                "type": "array",
                "items": {"type": "string"},
                "description": "消息 ID 列表，格式如 'telegram:7674283004:495'",
            },
            "context": {
                "type": "integer",
                "description": "每条消息前后各扩展的上下文条数（0=仅精确匹配，最大 5，默认 0）",
                "minimum": 0,
                "maximum": _MAX_CONTEXT,
                "default": 0,
            },
        },
        "required": ["ids"],
    }

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def execute(self, ids: list[str], context: int = 0, **_: Any) -> str:
        clean_ids = [str(i).strip() for i in (ids or []) if str(i).strip()]
        if not clean_ids:
            return json.dumps({"count": 0, "matched_count": 0, "messages": []}, ensure_ascii=False)

        ctx = max(0, min(int(context), _MAX_CONTEXT))
        if ctx == 0:
            messages = self._store.fetch_by_ids(clean_ids)
            return json.dumps(
                {"count": len(messages), "matched_count": len(messages), "messages": messages},
                ensure_ascii=False,
            )

        messages = self._store.fetch_by_ids_with_context(clean_ids, ctx)
        matched = sum(1 for m in messages if m.get("in_source_ref"))
        return json.dumps(
            {"count": len(messages), "matched_count": matched, "messages": messages},
            ensure_ascii=False,
        )


class SearchMessagesTool(Tool):
    name = "search_messages"
    description = "在原始对话历史中全文检索。用于想不起 ID 或需模糊查找时。支持 context 参数扩展匹配结果的前后文。"
    parameters = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词或短语"},
            "session_key": {
                "type": "string",
                "description": "限定 session，如 'telegram:7674283004'（可选）",
            },
            "role": {
                "type": "string",
                "enum": ["user", "assistant"],
                "description": "限定发言方（可选）",
            },
            "limit": {
                "type": "integer",
                "description": "最多返回条数，默认 10，最大 50",
                "minimum": 1,
                "maximum": 50,
                "default": 10,
            },
            "context": {
                "type": "integer",
                "description": "每条匹配结果前后各扩展的上下文条数（0=仅返回命中条，最大 5，默认 0）",
                "minimum": 0,
                "maximum": _MAX_CONTEXT,
                "default": 0,
            },
        },
        "required": ["query"],
    }

    def __init__(self, store: SessionStore) -> None:
        self._store = store

    async def execute(self, query: str, **kwargs: Any) -> str:
        term = (query or "").strip()
        if not term:
            return json.dumps({"count": 0, "matched_count": 0, "messages": []}, ensure_ascii=False)

        limit = max(1, min(int(kwargs.get("limit", 10)), 50))
        ctx = max(0, min(int(kwargs.get("context", 0)), _MAX_CONTEXT))

        matched = self._store.search_messages(
            term,
            session_key=(kwargs.get("session_key") or "").strip() or None,
            role=(kwargs.get("role") or "").strip() or None,
            limit=limit,
        )

        if ctx == 0:
            return json.dumps(
                {"count": len(matched), "matched_count": len(matched), "messages": matched},
                ensure_ascii=False,
            )

        hit_ids = [m["id"] for m in matched]
        messages = self._store.fetch_by_ids_with_context(hit_ids, ctx)
        matched_count = sum(1 for m in messages if m.get("in_source_ref"))
        return json.dumps(
            {"count": len(messages), "matched_count": matched_count, "messages": messages},
            ensure_ascii=False,
        )
