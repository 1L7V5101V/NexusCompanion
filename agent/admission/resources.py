"""分类资源 semaphore（PILOT_ROADMAP §5.9.5）。

LLM=30 / embedding=4 / MCP=8 / process=2 是 §10 DECIDED 冻结的 Pilot 初始值。
semaphore 是异步许可计数器：LLM=30 表示最多 30 个 tenant 的交互 turn 同时占用
LLM 执行槽，第 31 个等待；四类分开计数，某一类慢资源不得耗尽其他类额度。

进程级共享实例经 :func:`set_default` 由 bootstrap 装配；未装配时 :func:`gate`
返回零开销直通（单测可注入小容量实例验证上限与互不挤占）。
"""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from enum import StrEnum
from types import TracebackType

from agent.admission.queues import (
    EMBEDDING_CONCURRENCY,
    LLM_CONCURRENCY,
    MCP_CONCURRENCY,
    PROCESS_CONCURRENCY,
)


class ResourceKind(StrEnum):
    LLM = "llm"
    EMBEDDING = "embedding"
    MCP = "mcp"
    PROCESS = "process"


@dataclass(frozen=True)
class ResourceLimits:
    llm: int = LLM_CONCURRENCY
    embedding: int = EMBEDDING_CONCURRENCY
    mcp: int = MCP_CONCURRENCY
    process: int = PROCESS_CONCURRENCY

    def __post_init__(self) -> None:
        for name in ("llm", "embedding", "mcp", "process"):
            if getattr(self, name) < 1:
                raise ValueError(f"资源并发上限必须为正: {name}")

    def for_kind(self, kind: ResourceKind) -> int:
        return getattr(self, kind.value)


class _NullGate:
    """未装配 semaphore 时的零开销直通。"""

    def __init__(self, kind: ResourceKind) -> None:
        self._kind = kind

    async def __aenter__(self) -> "_NullGate":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class ResourceSemaphores:
    """四类资源各自的 ``asyncio.Semaphore``；计数相互独立。"""

    def __init__(self, limits: ResourceLimits | None = None) -> None:
        self._limits = limits or ResourceLimits()
        self._semaphores: dict[ResourceKind, asyncio.Semaphore] = {
            kind: asyncio.Semaphore(self._limits.for_kind(kind))
            for kind in ResourceKind
        }

    @property
    def limits(self) -> ResourceLimits:
        return self._limits

    def active(self, kind: ResourceKind) -> int:
        """当前占用许可数（观测用）。"""
        sem = self._semaphores[kind]
        return self._limits.for_kind(kind) - sem._value

    def gate(self, kind: ResourceKind) -> AbstractAsyncContextManager[object]:
        """异步上下文管理器：进入取许可，退出释放（含取消/异常路径）。"""
        return self._semaphores[kind]


_default: ResourceSemaphores | None = None


def set_default(semaphores: ResourceSemaphores | None) -> None:
    """bootstrap 装配进程级共享实例；传 None 恢复直通。"""
    global _default
    _default = semaphores


def default() -> ResourceSemaphores | None:
    return _default


def gate(kind: ResourceKind) -> AbstractAsyncContextManager[object]:
    """接缝侧入口：有装配实例则走 semaphore，否则零开销直通。"""
    semaphores = _default
    if semaphores is None:
        return _NullGate(kind)
    return semaphores.gate(kind)
