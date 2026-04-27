from __future__ import annotations

from typing import TYPE_CHECKING

from bus.event_bus import EventBus
from agent.lifecycle.phase import GatePhase
from agent.lifecycle.types import BeforeTurnCtx, TurnState

if TYPE_CHECKING:
    from agent.core.passive_turn import ContextStore
    from session.manager import SessionManager


class BeforeTurnPhase(GatePhase[TurnState, BeforeTurnCtx, BeforeTurnCtx]):

    def __init__(
        self,
        bus: EventBus,
        session_manager: SessionManager,
        context_store: ContextStore,
    ) -> None:
        super().__init__(bus)
        self._session_manager = session_manager
        self._context_store = context_store

    async def _setup(self, state: TurnState) -> BeforeTurnCtx:
        # 1. 从 SessionManager 拿到 or 创建本次 turn 的 session。
        session = self._session_manager.get_or_create(state.session_key)
        state.session = session
        state.retrieval_raw = None

        # 2. 通过 ContextStore.prepare 准备上下文（history、retrieval、skill mentions）。
        bundle = await self._context_store.prepare(
            msg=state.msg,
            session_key=state.session_key,
            session=session,
        )
        state.retrieval_raw = bundle.retrieval_trace_raw

        # 3. 组装 BeforeTurnCtx 返回，供 chain 修改和后续阶段读取。
        return BeforeTurnCtx(
            session_key=state.session_key,
            channel=state.msg.channel,
            chat_id=state.msg.chat_id,
            content=state.msg.content,
            timestamp=state.msg.timestamp,
            skill_names=list(bundle.skill_mentions),
            retrieved_memory_block=bundle.retrieved_memory_block,
            retrieval_trace_raw=bundle.retrieval_trace_raw,
            history_messages=tuple(bundle.history_messages),
        )

    async def _finalize(
        self,
        ctx: BeforeTurnCtx,
        input: TurnState,
    ) -> BeforeTurnCtx:
        return ctx
