from __future__ import annotations

import asyncio
import inspect
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Generic, TypeAlias, TypeVar, cast

logger = logging.getLogger(__name__)

E = TypeVar("E")
Handler: TypeAlias = Callable[[E], Awaitable[E | None] | E | None]


class EventSubscription(Generic[E]):
    """A registered event subscription that can be closed (unsubscribed)."""

    def __init__(
        self,
        event_bus: EventBus,
        event_type: type[E],
        handler: Handler[E],
    ) -> None:
        self._event_bus = event_bus
        self._event_type = event_type
        self._handler = cast(Handler[object], handler)
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._event_bus._unsubscribe(self._event_type, self._handler)


class EventBus:
    """Typed lifecycle hooks: observe + ordered intercept pipeline."""

    def __init__(self) -> None:
        self._handlers: dict[type[object], list[Handler[object]]] = {}
        self._any_handlers: list[Handler[object]] = []
        self._observe_queue: asyncio.Queue[object] | None = None
        self._observe_task: asyncio.Task[None] | None = None
        self._closed = False
        self._snapshot_store: Any = None

    def bind_runtime_snapshot_store(self, store: Any) -> None:
        """绑定 PluginManager 的 RuntimeSnapshotStore 引用。"""
        self._snapshot_store = store

    def on(
        self,
        event_type: type[E],
        handler: Handler[E],
    ) -> EventSubscription[E]:
        handlers = self._handlers.setdefault(cast(type[object], event_type), [])
        handlers.append(cast(Handler[object], handler))
        return EventSubscription(self, event_type, handler)

    def on_any(
        self,
        handler: Handler[object],
    ) -> EventSubscription[object]:
        """Register a handler for ALL event types."""
        self._any_handlers.append(handler)
        return EventSubscription(self, object, handler)  # type: ignore[arg-type]

    def _unsubscribe(self, event_type: type[object], handler: Handler[object]) -> None:
        """Remove a previously registered handler."""
        if event_type is object:
            try:
                self._any_handlers.remove(handler)
            except ValueError:
                pass
            return
        handlers = self._handlers.get(event_type)
        if handlers is None:
            return
        try:
            handlers.remove(handler)
        except ValueError:
            pass
        if not handlers:
            self._handlers.pop(event_type, None)

    async def emit(
        self,
        event: E,
    ) -> E:
        # 1. 依次执行干预链，handler 返回新事件时替换当前事件。
        for raw_handler in self._handlers.get(cast(type[object], type(event)), []):
            handler = cast(Handler[E], raw_handler)
            result = handler(event)
            if inspect.isawaitable(result):
                result = await result
            if result is not None:
                event = cast(E, result)
        # 2. 通知 on_any 监听者（只读，不干预）。
        for handler in list(self._any_handlers):
            _ = await self._run_observer(event, cast(Handler[object], handler))
        return event

    async def observe(
        self,
        event: object,
        ) -> None:
        # 1. 依次执行观察者，单个观察者失败不打断主流程。
        for handler in self._handlers.get(type(event), []):
            _ = await self._run_observer(event, handler)
        # 2. on_any 监听者。
        for handler in list(self._any_handlers):
            _ = await self._run_observer(event, handler)

    async def fanout(
        self,
        event: object,
    ) -> None:
        # 1. 并发执行类型匹配的观察者；每个观察者自己记录异常，fanout 只汇总失败数量。
        typed_handlers = list(self._handlers.get(type(event), []))
        any_handlers = list(self._any_handlers)
        all_handlers = typed_handlers + any_handlers
        if not all_handlers:
            return
        results = await asyncio.gather(
            *(self._run_observer(event, handler) for handler in all_handlers)
        )
        failed_count = results.count(False)
        if failed_count:
            logger.warning(
                "fanout completed with observer errors: event=%s failed=%d total=%d",
                type(event).__name__,
                failed_count,
                len(all_handlers),
            )

    def enqueue(
        self,
        event: object,
    ) -> None:
        # 1. 后台队列只负责把事件交给 fanout，避免主回复等待后处理。
        if self._closed:
            logger.warning("event enqueue ignored after close: %s", type(event).__name__)
            return
        queue = self._ensure_observe_queue()
        queue.put_nowait(event)

    async def drain(
        self,
    ) -> None:
        queue = self._observe_queue
        if queue is None:
            return
        self._ensure_observe_task()
        await queue.join()

    async def aclose(
        self,
    ) -> None:
        await self.drain()
        self._closed = True
        task = self._observe_task
        if task is None:
            return
        _ = task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    async def _run_observer(
        self,
        event: object,
        handler: Handler[object],
    ) -> bool:
        try:
            result = handler(event)
            if inspect.isawaitable(result):
                await result
            return True
        except Exception:
            logger.exception(
                "observer error for %s handler=%s",
                type(event).__name__,
                _handler_name(handler),
            )
            return False

    def _ensure_observe_queue(
        self,
    ) -> asyncio.Queue[object]:
        if self._observe_queue is None:
            self._observe_queue = asyncio.Queue()
        self._ensure_observe_task()
        return self._observe_queue

    def _ensure_observe_task(
        self,
    ) -> None:
        if self._closed:
            return
        if self._observe_task is not None and not self._observe_task.done():
            return
        task = asyncio.create_task(
            self._run_observe_queue(),
            name="event_bus_observe_queue",
        )
        self._observe_task = task
        task.add_done_callback(self._on_observe_task_done)

    async def _run_observe_queue(
        self,
    ) -> None:
        while True:
            queue = self._observe_queue
            if queue is None:
                return
            event = await queue.get()
            try:
                await self.fanout(event)
            finally:
                queue.task_done()

    def _on_observe_task_done(
        self,
        task: asyncio.Task[None],
    ) -> None:
        if self._observe_task is task:
            self._observe_task = None
        if self._closed or task.cancelled():
            return
        try:
            exc = task.exception()
        except Exception as e:
            logger.warning("event dispatcher inspect failed: %s", e)
            exc = None
        if exc is not None:
            logger.error(
                "event dispatcher stopped unexpectedly",
                exc_info=(type(exc), exc, exc.__traceback__),
            )
        if self._observe_queue is not None:
            self._ensure_observe_task()


def _handler_name(handler: Handler[object]) -> str:
    return str(
        getattr(
            handler,
            "__qualname__",
            getattr(handler, "__name__", repr(handler)),
        )
    )
