from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from bus.events import InboundMessage, OutboundMessage
from bus.internal_events import is_spawn_completion_message

if TYPE_CHECKING:
    from agent.core.agent_core import AgentCore
    from agent.looping.handlers import InternalEventHandler


@dataclass
class CoreRunnerDeps:
    agent_core: "AgentCore"
    internal_event_handler: "InternalEventHandler"


class CoreRunner:
    """
    ┌──────────────────────────────────────┐
    │ CoreRunner                           │
    ├──────────────────────────────────────┤
    │ 1. 判断是否内部事件                  │
    │ 2. spawn completion 走旧 handler     │
    │ 3. 普通被动消息走 AgentCore          │
    └──────────────────────────────────────┘
    """

    def __init__(self, deps: CoreRunnerDeps) -> None:
        self._agent_core = deps.agent_core
        self._internal_event_handler = deps.internal_event_handler

    async def process(
        self,
        msg: InboundMessage,
        key: str,
        *,
        dispatch_outbound: bool = True,
    ) -> OutboundMessage:
        # 1. 先保留内部事件旧链路，避免这一步切太大。
        if is_spawn_completion_message(msg):
            return await self._internal_event_handler.process_spawn_completion(msg, key)

        # 2. 普通被动消息统一切到 AgentCore。
        return await self._agent_core.process(
            msg,
            key,
            dispatch_outbound=dispatch_outbound,
        )
