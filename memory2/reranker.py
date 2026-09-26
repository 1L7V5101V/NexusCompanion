"""可实验开关的 reranker：对 RRF 截断后的候选做 listwise 重排。

§4.4：reranker 的输入是 RRF 截断后的候选；启用后最终注入顺序以 reranker
结果为准。三实验开关之一，默认关闭（不注入 Retriever 即不进入默认路径）。
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

logger = logging.getLogger(__name__)

DEFAULT_RERANK_TIMEOUT_S = 3.0
DEFAULT_MAX_CANDIDATES = 20
_MAX_TOKENS = 200

_ChatCall = Callable[..., Awaitable[Any]]


class Reranker(Protocol):
    """reranker 协议：返回按相关性重排的候选；失败时 fail-open 回原顺序。"""

    async def rerank(self, query: str, items: list[dict]) -> list[dict]: ...


class LightLLMReranker:
    """light LLM listwise reranker。

    候选按 RRF 序编号交给 LLM；解析出的 id 序决定新顺序，未覆盖/解析失败的
    id 按原序追加；整体失败（超时/异常/空输出）返回原顺序，绝不因重排失败
    丢候选。
    """

    def __init__(
        self,
        chat: _ChatCall,
        *,
        model: str,
        timeout_s: float = DEFAULT_RERANK_TIMEOUT_S,
        max_candidates: int = DEFAULT_MAX_CANDIDATES,
    ) -> None:
        self._chat = chat
        self._model = model
        self._timeout_s = max(0.1, float(timeout_s))
        self._max_candidates = max(2, int(max_candidates))

    async def rerank(self, query: str, items: list[dict]) -> list[dict]:
        if len(items) <= 1:
            return items
        candidates = list(items[: self._max_candidates])
        overflow = list(items[self._max_candidates :])
        try:
            ordered_ids = await asyncio.wait_for(
                self._ask_order(query, candidates), timeout=self._timeout_s
            )
        except Exception as e:
            logger.debug("memory2 reranker failed, keep RRF order: %s", e)
            return list(items)
        if not ordered_ids:
            return list(items)
        by_id: dict[str, dict] = {}
        for item in candidates:
            item_id = str(item.get("id", "") or "")
            if item_id and item_id not in by_id:
                by_id[item_id] = item
        ranked: list[dict] = []
        seen: set[str] = set()
        for item_id in ordered_ids:
            item = by_id.get(item_id)
            if item is not None and item_id not in seen:
                seen.add(item_id)
                ranked.append(item)
        for item in candidates:
            item_id = str(item.get("id", "") or "")
            if item_id not in seen:
                ranked.append(item)
        return ranked + overflow

    async def _ask_order(self, query: str, candidates: list[dict]) -> list[str]:
        lines: list[str] = []
        for index, item in enumerate(candidates, start=1):
            item_id = str(item.get("id", "") or "")
            summary = str(item.get("summary", "") or "")[:80]
            mtype = str(item.get("memory_type", "") or "")
            lines.append(f"{index}. id={item_id} type={mtype} {summary}")
        prompt = (
            "你是记忆检索重排器。根据 query 与候选记忆的相关性，"
            "把候选按最相关到最不相关排序。\n"
            f"query：{query}\n候选：\n" + "\n".join(lines)
            + "\n只输出按相关性排序的 id 列表（逗号分隔），不要其他内容："
        )
        resp = await self._chat(
            messages=[{"role": "user", "content": prompt}],
            tools=[],
            model=self._model,
            max_tokens=_MAX_TOKENS,
        )
        content = str(getattr(resp, "content", "") or "").strip()
        ids: list[str] = []
        for token in re.split(r"[,\s，、]+", content):
            cleaned = token.strip().strip(".`")
            if cleaned:
                ids.append(cleaned)
        return ids
