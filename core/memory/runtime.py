from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, cast

if TYPE_CHECKING:
    from core.memory.engine import (
        MemoryEngine,
        MemoryMutation,
        MemoryMutationResult,
        MemoryQuery,
        MemoryQueryResult,
    )
    from core.memory.markdown import MarkdownMemoryRuntime

logger = logging.getLogger(__name__)


class _AsyncCloseable(Protocol):
    def aclose(self) -> object: ...


class _Closeable(Protocol):
    def close(self) -> object: ...


@dataclass
class MemoryRuntime:
    """记忆运行时：检索引擎 + 文件后端的装配容器。

    画像读写不在这里。`read_long_term` / `read_self` / `read_recent_context` /
    `read_recent_history` / `get_memory_context` / `has_long_term_memory` 这六个无租户代理
    已删除（c18 task 4.1，design ADR-3）：它们一律转手到进程级共享 markdown store，调用方
    拿不到租户，多租户下就是跨租户泄漏。画像的租户归属入口在
    `bootstrap/persona.py`（`resolve_persona_snapshot` / `SqlitePersonaResolver` 与两个
    RelationshipState 写缝），组装侧只吃解析出来的不可变快照。
    """

    markdown: "MarkdownMemoryRuntime"
    engine: "MemoryEngine"
    closeables: list[object] = field(default_factory=list[object])
    engines: dict[str, "MemoryEngine"] = field(default_factory=dict)

    async def query(
        self,
        request: "MemoryQuery",
    ) -> "MemoryQueryResult":
        return await self.engine.query(request)

    async def mutate(
        self,
        request: "MemoryMutation",
    ) -> "MemoryMutationResult":
        return await self.engine.mutate(request)

    async def aclose(self) -> None:
        first_error: Exception | None = None
        for closeable in reversed(self.closeables):
            try:
                if hasattr(closeable, "aclose"):
                    result = cast(_AsyncCloseable, closeable).aclose()
                    if inspect.isawaitable(result):
                        await result
                elif hasattr(closeable, "close"):
                    _ = cast(_Closeable, closeable).close()
            except Exception as exc:
                if first_error is None:
                    first_error = exc
                logger.warning(
                    "memory runtime close failed for %s: %s",
                    type(closeable).__name__,
                    exc,
                )
        if first_error is not None:
            raise first_error
