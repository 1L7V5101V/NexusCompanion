from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Protocol

logger = logging.getLogger("agent.core.scheduler")


class SessionLike(Protocol):
    key: str
    messages: list[dict]


ConsolidationRunner = Callable[[SessionLike], Awaitable[None]]


class TurnScheduler:
    def __init__(
        self,
        post_mem_worker: object | None,
        consolidation_runner: ConsolidationRunner,
        memory_window: int,
    ) -> None:
        self._post_mem_worker = post_mem_worker
        self._consolidation_runner = consolidation_runner
        self._memory_window = memory_window
        self._consolidating: set[str] = set()

    def is_consolidating(self, key: str) -> bool:
        return key in self._consolidating

    def mark_manual_start(self, key: str) -> bool:
        if key in self._consolidating:
            return False
        self._consolidating.add(key)
        return True

    def mark_manual_end(self, key: str) -> None:
        self._consolidating.discard(key)

    def schedule_consolidation(self, session: SessionLike, key: str) -> None:
        # 1. 只有消息数超过窗口，且当前不在 consolidate 中，才起后台任务。
        if len(session.messages) <= self._memory_window or key in self._consolidating:
            return

        # 2. consolidation 自身 fire-and-forget，重复请求按 key 去重。
        self._consolidating.add(key)
        task = asyncio.create_task(
            self._run_consolidation_bg(session, key),
            name=f"consolidation:{key}",
        )
        task.add_done_callback(lambda t: self._on_consolidation_done(t, key))

    async def _run_consolidation_bg(self, session: SessionLike, key: str) -> None:
        try:
            # 3. 真正 consolidate/save 细节由外部注入。
            await self._consolidation_runner(session)
        finally:
            self._consolidating.discard(key)

    def _on_consolidation_done(self, task: asyncio.Task, key: str) -> None:
        if task.cancelled():
            logger.info("consolidation task cancelled: %s", key)
            return

        try:
            exc = task.exception()
        except Exception as e:
            logger.warning(
                "consolidation task inspection failed: session=%s err=%s",
                key,
                e,
            )
            return

        if exc is not None:
            logger.warning("consolidation task failed: session=%s err=%s", key, exc)

    def _on_post_mem_done(self, task: asyncio.Task, key: str) -> None:
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            logger.info("post_mem cancelled: %s", key)
            return
        except Exception as e:
            logger.warning(
                "post_mem inspect failed session=%s err=%s",
                key,
                e,
            )
            return

        if exc is not None:
            logger.warning("post_mem failed session=%s err=%s", key, exc)
