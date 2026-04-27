from __future__ import annotations

from typing import TYPE_CHECKING

from bus.event_bus import EventBus
from agent.core.types import ContextRequest
import agent.core.passive_support as support
from agent.lifecycle.phase import GatePhase
from agent.lifecycle.types import BeforeReasoningCtx, BeforeReasoningInput

if TYPE_CHECKING:
    from agent.context import ContextBuilder
    from agent.tools.registry import ToolRegistry
    from session.manager import SessionManager


class BeforeReasoningPhase(
    GatePhase[BeforeReasoningInput, BeforeReasoningCtx, BeforeReasoningCtx]
):

    def __init__(
        self,
        bus: EventBus,
        tools: ToolRegistry,
        session_manager: SessionManager,
        context: ContextBuilder,
    ) -> None:
        super().__init__(bus)
        self._tools = tools
        self._session_manager = session_manager
        self._context = context

    async def _setup(
        self,
        input: BeforeReasoningInput,
    ) -> BeforeReasoningCtx:
        state = input.state
        before_turn = input.before_turn
        if state.session is None:
            raise RuntimeError("BeforeReasoning requires TurnState.session")

        # 1. 同步 tool context，供后续工具调用按需读取 channel/chat_id/current_user_source_ref。
        self._tools.set_context(
            channel=before_turn.channel,
            chat_id=before_turn.chat_id,
            current_user_source_ref=support.predict_current_user_source_ref(
                session_manager=self._session_manager,
                session=state.session,
            ),
        )

        # 2. 从 BeforeTurnCtx 转到 BeforeReasoningCtx，skill_names/retrieved_memory_block 可被 chain 修改。
        return BeforeReasoningCtx(
            session_key=before_turn.session_key,
            channel=before_turn.channel,
            chat_id=before_turn.chat_id,
            content=before_turn.content,
            timestamp=before_turn.timestamp,
            skill_names=list(before_turn.skill_names),
            retrieved_memory_block=before_turn.retrieved_memory_block,
        )

    async def _finalize(
        self,
        ctx: BeforeReasoningCtx,
        input: BeforeReasoningInput,
    ) -> BeforeReasoningCtx:
        # 1. prompt cache 预热：用 chain 修改后的 skill_names 和 retrieved_memory_block 渲染一次。
        _ = self._context.render(
            ContextRequest(
                history=[],
                current_message="",
                skill_names=ctx.skill_names,
                channel=ctx.channel,
                chat_id=ctx.chat_id,
                message_timestamp=ctx.timestamp,
                retrieved_memory_block=ctx.retrieved_memory_block,
            )
        )
        return ctx
