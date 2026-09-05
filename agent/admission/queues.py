"""有界队列与 overload 语义。

容量默认值为 PILOT_ROADMAP §10 DECIDED「Queue 容量初始值」冻结数字
（global interactive 128、per-tenant pending 16、maintenance 64、per-kind 1、
WS outbound 256/soft 192/1 MiB、LLM/embedding/MCP/process 30/4/8/2）。
作为可配置 Pilot 初始值实现；P0/P0.5 压测记录拒绝率、backlog、429/退避和内存后，
后续 change 才可调整（§5.9.5：不允许保留无界默认）。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

# §10 DECIDED Queue 容量初始值
GLOBAL_INTERACTIVE_QUEUE = 128
PER_TENANT_PENDING_INTERACTIVE = 16
GLOBAL_MAINTENANCE_QUEUE = 64
PER_KIND_MAINTENANCE_PENDING = 1
WS_OUTBOUND_HARD_LIMIT = 256
WS_OUTBOUND_SOFT_LIMIT = 192
WS_OUTBOUND_MAX_PAYLOAD_BYTES = 1024 * 1024

# §10 DECIDED 资源 semaphore 初始值
LLM_CONCURRENCY = 30
EMBEDDING_CONCURRENCY = 4
MCP_CONCURRENCY = 8
PROCESS_CONCURRENCY = 2

DEFAULT_RETRY_AFTER_SECONDS = 5.0

_LIMIT_KINDS = (
    "global_interactive",
    "per_tenant_interactive",
    "global_maintenance",
    "per_kind_maintenance",
    "ws_outbound_soft",
    "ws_outbound_hard",
)


class AdmissionOverloadError(Exception):
    """admission 队列满载的类型化 overload 信号。

    §5.9.5：interactive overload 必须发生在 durable acceptance 之前；
    HTTP 侧映射为 429 + ``Retry-After``，WebSocket 侧映射为结构化 overload 事件，
    Telegram 等可重试 channel 不 ack 让 provider 重试。本异常只承载语义，
    渠道 HTTP/WS 响应映射归 C4/C5。
    """

    def __init__(
        self,
        limit_kind: str,
        retry_after: float = DEFAULT_RETRY_AFTER_SECONDS,
        detail: str = "",
    ) -> None:
        if limit_kind not in _LIMIT_KINDS:
            raise ValueError(f"未知 overload 类别: {limit_kind!r}")
        self.limit_kind = limit_kind
        self.retry_after = retry_after
        super().__init__(detail or f"admission overload: {limit_kind}")


@dataclass(frozen=True)
class AdmissionLimits:
    """admission 容量初始值（§10 DECIDED），部署可配置。"""

    global_interactive_queue: int = GLOBAL_INTERACTIVE_QUEUE
    per_tenant_pending_interactive: int = PER_TENANT_PENDING_INTERACTIVE
    global_maintenance_queue: int = GLOBAL_MAINTENANCE_QUEUE
    per_kind_maintenance_pending: int = PER_KIND_MAINTENANCE_PENDING

    def __post_init__(self) -> None:
        for name in (
            "global_interactive_queue",
            "per_tenant_pending_interactive",
            "global_maintenance_queue",
            "per_kind_maintenance_pending",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"admission 容量必须为正: {name}")


class BoundedAdmissionQueue:
    """有界 FIFO 队列；满载 ``try_put_nowait`` 抛 :class:`AdmissionOverloadError`。

    §5.9.5：满载语义是明确拒绝（durable acceptance 前），不是阻塞等待，
    也不是先接受后丢弃。
    """

    def __init__(
        self,
        capacity: int,
        *,
        limit_kind: str,
        retry_after: float = DEFAULT_RETRY_AFTER_SECONDS,
    ) -> None:
        if capacity < 1:
            raise ValueError("bounded queue capacity 必须为正")
        self._capacity = capacity
        self._limit_kind = limit_kind
        self._retry_after = retry_after
        self._queue: asyncio.Queue[object] = asyncio.Queue(capacity)

    @property
    def capacity(self) -> int:
        return self._capacity

    @property
    def limit_kind(self) -> str:
        return self._limit_kind

    def qsize(self) -> int:
        return self._queue.qsize()

    def empty(self) -> bool:
        return self._queue.empty()

    def try_put_nowait(self, item: object) -> None:
        """入队；满载抛 :class:`AdmissionOverloadError`（不阻塞、不丢弃已接受项）。"""
        try:
            self._queue.put_nowait(item)
        except asyncio.QueueFull as exc:
            raise AdmissionOverloadError(
                self._limit_kind,
                self._retry_after,
                detail=f"{self._limit_kind} 队列已满（{self._capacity}）",
            ) from exc

    def get_nowait(self) -> object | None:
        if self._queue.empty():
            return None
        return self._queue.get_nowait()

    async def get(self) -> object:
        return await self._queue.get()

    def task_done(self) -> None:
        self._queue.task_done()
