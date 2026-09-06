from __future__ import annotations

import asyncio
import logging
from typing import Any, cast

from agent.admission.queues import PER_TENANT_PENDING_INTERACTIVE
from agent.admission.lanes import resolve_admission_tenant
from agent.control.errors import RuntimeClosedError, ThreadBusyError
from agent.control.models import TurnRequest, TurnStatus
from agent.control.runtime import ConversationRuntime
from agent.looping.core import AgentLoop
from bus.events import InboundMessage, OutboundMessage
from bus.queue import MessageBus

logger = logging.getLogger(__name__)


class PassiveMessageWorker:
    """把渠道入站消息转换为 ConversationRuntime turn。

    C3 §5.9.5：lane key = 服务端派生 tenant admission key；per-tenant pending
    interactive 有界（默认 16），tenant 内 active work 仍为 1，第 17 条未接受
    消息明确拒绝，不无限堆积。
    """

    def __init__(
        self,
        bus: MessageBus,
        runtime: ConversationRuntime,
        legacy_loop: AgentLoop,
        *,
        per_tenant_pending: int = PER_TENANT_PENDING_INTERACTIVE,
    ) -> None:
        if per_tenant_pending < 1:
            raise ValueError("per_tenant_pending 必须为正")
        self._bus = bus
        self._runtime = runtime
        self._legacy_loop = legacy_loop
        self._per_tenant_pending = per_tenant_pending
        self._running = False
        self._lane_queues: dict[str, asyncio.Queue[InboundMessage | object]] = {}
        self._lane_tasks: dict[str, asyncio.Task[None]] = {}
        self._rejected_inbound = 0

    async def run(self) -> None:
        self._running = True
        try:
            while self._running:
                try:
                    item = await asyncio.wait_for(self._bus.consume_inbound(), timeout=1.0)
                except asyncio.TimeoutError:
                    continue
                self._enqueue(item)
        finally:
            self._running = False
            for task in tuple(self._lane_tasks.values()):
                task.cancel()
            if self._lane_tasks:
                await asyncio.gather(
                    *tuple(self._lane_tasks.values()),
                    return_exceptions=True,
                )
            self._lane_tasks.clear()
            self._lane_queues.clear()

    @property
    def rejected_inbound(self) -> int:
        """per-tenant pending 满被明确拒绝的入站消息累计数（观测用）。"""
        return self._rejected_inbound

    def _enqueue(self, item: object) -> None:
        # C3：lane key 从 channel-specific session_key 切换为 tenant admission key。
        tenant_key = resolve_admission_tenant(cast(InboundMessage, item))
        queue = self._lane_queues.get(tenant_key)
        if queue is None:
            queue = asyncio.Queue(self._per_tenant_pending)
            self._lane_queues[tenant_key] = queue
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            # §5.9.5：per-tenant 第 17 条未接受消息明确拒绝，不无限堆积；
            # 未接受项不进入 lane，仅释放 bus 侧 pending 标记并回执拒绝。
            self._rejected_inbound += 1
            logger.warning(
                "per-tenant pending interactive 队列满，拒绝消息 tenant=%s depth=%s",
                tenant_key,
                self._per_tenant_pending,
                extra={"tenant_id": tenant_key, "overload_kind": "per_tenant_interactive"},
            )
            self._reject_inbound(cast(InboundMessage, item))
            return
        task = self._lane_tasks.get(tenant_key)
        if task is None or task.done():
            self._lane_tasks[tenant_key] = asyncio.create_task(
                self._run_lane(tenant_key, queue),
                name=f"passive-lane:{tenant_key}",
            )

    def _reject_inbound(self, item: InboundMessage) -> None:
        """对被拒绝的入站消息回执明确拒绝文案，并完成 bus 侧确认。"""

        async def _notify() -> None:
            try:
                await self._bus.publish_outbound(
                    OutboundMessage(
                        channel=item.channel,
                        chat_id=item.chat_id,
                        content="当前会话排队已满，请稍后再发这条消息。",
                        metadata={"nexus_overload": True},
                    )
                )
            finally:
                await self._bus.complete_inbound(item)

        asyncio.create_task(_notify(), name=f"passive-lane-reject:{item.session_key}")

    async def _run_lane(
        self,
        key: str,
        queue: asyncio.Queue[InboundMessage | object],
    ) -> None:
        """串行执行单 tenant 队列，并隔离单条消息失败。"""

        while True:
            item = await queue.get()
            try:
                if isinstance(item, InboundMessage):
                    await self._run_message(item)
                else:
                    await self._legacy_loop._run_inbound_turn(cast(Any, item))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("passive lane message failed tenant=%s", key)
            if queue.empty():
                task = asyncio.current_task()
                if self._lane_tasks.get(key) is task:
                    self._lane_tasks.pop(key)
                    self._lane_queues.pop(key)
                return

    async def _run_message(self, item: InboundMessage) -> None:
        """执行一条渠道消息，并始终完成 MessageBus 入站确认。"""

        try:
            # 1. 渠道信息只作为 executor 所需的受控 metadata，不改变 thread identity。
            request = TurnRequest(
                item.session_key,
                item.content,
                {
                    "channel": item.channel,
                    "chatId": item.chat_id,
                    "sender": item.sender,
                    "media": list(item.media),
                    "tenantId": item.tenant_id,
                },
            )
            while True:
                await self._runtime.wait_thread_available(item.session_key)
                try:
                    handle = await self._runtime.start_turn(request)
                except ThreadBusyError:
                    continue
                except RuntimeClosedError:
                    await self._bus.publish_outbound(
                        OutboundMessage(
                            channel=item.channel,
                            chat_id=item.chat_id,
                            content="服务正在重启，请稍后重发这条消息。",
                        )
                    )
                    return
                break
            result = await handle.result()

            # 2. channel adapter 在领域终态外层映射用户安全文案。
            if result.status is TurnStatus.COMPLETED:
                assistant = next(
                    entry for entry in reversed(result.items) if entry.kind.value == "assistantMessage"
                )
                data = assistant.data
                outbound = OutboundMessage(
                    channel=item.channel,
                    chat_id=item.chat_id,
                    content=result.final_response or "",
                    thinking=cast(str | None, data.get("thinking")),
                    reply_to=cast(str | None, data.get("replyTo")),
                    media=list(cast(list[str], data.get("media", []))),
                    metadata=dict(cast(dict[str, Any], data.get("metadata", {}))),
                    control_turn_id=handle.id,
                )
            elif result.status is TurnStatus.FAILED:
                outbound = OutboundMessage(
                    channel=item.channel,
                    chat_id=item.chat_id,
                    content="处理消息时出错，请稍后再试。",
                    # channel adapter 据此映射协议级失败帧（如 WebChat turn.failed）；
                    # 对不识别该标记的 channel 无行为影响。
                    metadata={"nexus_error": True},
                )
            else:
                return
            await self._bus.publish_outbound(outbound)
        finally:
            await self._bus.complete_inbound(item)

    def stop(self) -> None:
        self._running = False
