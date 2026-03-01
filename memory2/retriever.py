"""
Memory v2 检索器：查询 → top-k items + 格式化注入块
"""
from __future__ import annotations

import logging

from memory2.store import MemoryStore2
from memory2.embedder import Embedder

logger = logging.getLogger(__name__)


class Retriever:
    def __init__(
        self,
        store: MemoryStore2,
        embedder: Embedder,
        top_k: int = 8,
        score_threshold: float = 0.45,
    ) -> None:
        self._store = store
        self._embedder = embedder
        self._top_k = top_k
        self._score_threshold = score_threshold

    async def retrieve(
        self,
        query: str,
        memory_types: list[str] | None = None,
    ) -> list[dict]:
        """embed query → cosine search → 返回命中条目列表"""
        query_vec = await self._embedder.embed(query)
        items = self._store.vector_search(
            query_vec=query_vec,
            top_k=self._top_k,
            memory_types=memory_types,
            score_threshold=self._score_threshold,
        )
        logger.debug(f"memory2 retrieve: query={query[:60]!r} hits={len(items)}")
        return items

    def format_injection_block(self, items: list[dict]) -> str:
        """
        格式化为 system prompt 注入块：
        - procedure with tool_requirement → ## 【强制约束】段
        - procedure without tool_requirement, preference → ## 【流程规范】段
        - event → ## 【相关历史】段
        """
        if not items:
            return ""

        forced: list[str] = []
        norms: list[str] = []
        events: list[str] = []

        for item in items:
            mtype = item.get("memory_type", "")
            summary = item.get("summary", "").strip()
            extra = item.get("extra_json") or {}
            happened_at = item.get("happened_at") or ""

            if mtype == "procedure":
                tool_req = extra.get("tool_requirement")
                if tool_req:
                    line = f"- {summary}（必须调用工具：{tool_req}）"
                    forced.append(line)
                else:
                    steps = extra.get("steps") or []
                    if steps:
                        step_text = "；".join(str(s) for s in steps)
                        line = f"- {summary}（步骤：{step_text}）"
                    else:
                        line = f"- {summary}"
                    norms.append(line)
            elif mtype == "preference":
                norms.append(f"- {summary}")
            elif mtype in ("event", "profile"):
                ts = f"[{happened_at}] " if happened_at else ""
                events.append(f"- {ts}{summary}")

        parts: list[str] = []
        if forced:
            parts.append("## 【强制约束】记忆规则（必须执行）\n" + "\n".join(forced))
        if norms:
            parts.append("## 【流程规范】用户偏好与规则\n" + "\n".join(norms))
        if events:
            parts.append("## 【相关历史】过往事件\n" + "\n".join(events))

        if not parts:
            return ""

        return "\n\n".join(parts)
