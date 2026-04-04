from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from agent.looping.handlers import process_spawn_completion_event
from bus.events import InboundMessage, OutboundMessage
from bus.internal_events import is_spawn_completion_message

if TYPE_CHECKING:
    from agent.core.agent_core import AgentCore
    from agent.core.context_store import ContextStore
    from agent.context import ContextBuilder
    from agent.looping.handlers import ConversationTurnHandler, InternalEventHandler
    from agent.looping.ports import AgentLoopRunner, SessionServices
    from agent.tools.registry import ToolRegistry


@dataclass
class CoreRunnerDeps:
    agent_core: "AgentCore"
    conversation_handler: "ConversationTurnHandler | None" = None
    session: "SessionServices | None" = None
    context: "ContextBuilder | None" = None
    context_store: "ContextStore | None" = None
    tools: "ToolRegistry | None" = None
    memory_window: int | None = None
    run_agent_loop_fn: "AgentLoopRunner | None" = None
    internal_event_handler: "InternalEventHandler | None" = None


class CoreRunner:
    """
    ┌──────────────────────────────────────┐
    │ CoreRunner                           │
    ├──────────────────────────────────────┤
    │ 1. 判断是否内部事件                  │
    │ 2. 显式注入优先                       │
    │ 3. 否则走默认新主链                   │
    └──────────────────────────────────────┘
    """

    def __init__(self, deps: CoreRunnerDeps) -> None:
        self._agent_core = deps.agent_core
        self._conversation_handler = deps.conversation_handler
        self._session = deps.session
        self._context = deps.context
        self._context_store = deps.context_store
        self._tools = deps.tools
        self._memory_window = deps.memory_window
        self._run_agent_loop_fn = deps.run_agent_loop_fn
        self._internal_event_handler = deps.internal_event_handler

    async def process(
        self,
        msg: InboundMessage,
        key: str,
        *,
        dispatch_outbound: bool = True,
    ) -> OutboundMessage:
        # 1. 先处理内部事件；若显式注入旧 handler，则它优先接管。
        if is_spawn_completion_message(msg):
            if self._internal_event_handler is not None:
                return await self._internal_event_handler.process_spawn_completion(msg, key)
            if (
                self._session is not None
                and self._context is not None
                and self._context_store is not None
                and self._tools is not None
                and self._memory_window is not None
                and self._run_agent_loop_fn is not None
            ):
                return await process_spawn_completion_event(
                    msg=msg,
                    key=key,
                    session_svc=self._session,
                    context=self._context,
                    context_store=self._context_store,
                    tools=self._tools,
                    memory_window=self._memory_window,
                    run_agent_loop_fn=self._run_agent_loop_fn,
                )
            if self._internal_event_handler is None:
                raise RuntimeError("spawn completion 缺少处理依赖")
            return await self._internal_event_handler.process_spawn_completion(msg, key)

        # 2. 普通被动消息同样遵守“显式注入优先”。
        if self._conversation_handler is not None:
            return await self._conversation_handler.process(
                msg,
                key,
                dispatch_outbound=dispatch_outbound,
            )

        # 3. 默认普通被动消息统一走 AgentCore。
        return await self._agent_core.process(
            msg,
            key,
            dispatch_outbound=dispatch_outbound,
        )
