from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, cast

import agent.core.passive_support as support
from agent.core.response_parser import parse_response
from agent.lifecycle.phase import GatePhase
from agent.lifecycle.types import (
    AfterReasoningCtx,
    AfterReasoningInput,
    AfterReasoningResult,
)
from bus.event_bus import EventBus
from bus.events import OutboundMessage

if TYPE_CHECKING:
    from agent.looping.ports import SessionServices
    from session.manager import Session

logger = logging.getLogger(__name__)


class AfterReasoningPhase(
    GatePhase[AfterReasoningInput, AfterReasoningCtx, AfterReasoningResult]
):

    def __init__(
        self,
        bus: EventBus,
        session_services: SessionServices,
    ) -> None:
        super().__init__(bus)
        self._session_services = session_services

    async def _setup(self, input: AfterReasoningInput) -> AfterReasoningCtx:
        msg = input.state.msg
        turn_result = input.turn_result

        # 1. None reply fallback
        raw_reply = turn_result.reply
        if raw_reply is None:
            raw_reply = "I've completed processing but have no response to give."

        tool_chain = cast(list[dict[str, object]], turn_result.tool_chain)

        # 2. parse_response: clean text + extract metadata
        parsed = parse_response(raw_reply, tool_chain=tool_chain)

        return AfterReasoningCtx(
            session_key=input.state.session_key,
            channel=msg.channel,
            chat_id=msg.chat_id,
            reply=parsed.clean_text,
            response_metadata=parsed.metadata,
            tools_used=tuple(turn_result.tools_used),
            tool_chain=tuple(tool_chain),
            thinking=turn_result.thinking,
            streamed=turn_result.streamed,
            context_retry=dict(turn_result.context_retry),
            outbound_metadata={
                **(msg.metadata or {}),
                "tools_used": list(turn_result.tools_used),
                "tool_chain": list(tool_chain),
                "context_retry": dict(turn_result.context_retry),
                "streamed_reply": turn_result.streamed,
            },
        )

    async def _finalize(
        self,
        ctx: AfterReasoningCtx,
        input: AfterReasoningInput,
    ) -> AfterReasoningResult:
        state = input.state
        msg = state.msg
        raw_session = state.session
        if raw_session is None:
            raise RuntimeError("AfterReasoning requires TurnState.session")
        session = cast("Session", raw_session)

        cited_memory_ids = list(ctx.response_metadata.cited_memory_ids)

        # 1. persist user message
        omit_user_turn = bool((msg.metadata or {}).get("omit_user_turn"))
        if not omit_user_turn:
            if self._session_services.presence:
                self._session_services.presence.record_user_message(session.key)
            user_kwargs: dict[str, object] = {}
            llm_user_content = ctx.context_retry.get("llm_user_content")
            if isinstance(llm_user_content, (str, list)):
                user_kwargs["llm_user_content"] = llm_user_content
            llm_context_frame = ctx.context_retry.get("llm_context_frame")
            if isinstance(llm_context_frame, str) and llm_context_frame.strip():
                user_kwargs["llm_context_frame"] = llm_context_frame
            session.add_message(
                "user",
                msg.content,
                media=msg.media if msg.media else None,
                **user_kwargs,
            )

        # 2. persist assistant message
        assistant_kwargs: dict[str, Any] = {
            "tools_used": list(ctx.tools_used) if ctx.tools_used else None,
            "tool_chain": list(ctx.tool_chain) if ctx.tool_chain else None,
        }
        if ctx.thinking is not None:
            assistant_kwargs["reasoning_content"] = ctx.thinking
        if cited_memory_ids:
            assistant_kwargs["cited_memory_ids"] = cited_memory_ids
        session.add_message("assistant", ctx.reply, **assistant_kwargs)

        # 3. update_session_runtime_metadata BEFORE append_messages
        support.update_session_runtime_metadata(
            session,
            tools_used=list(ctx.tools_used),
            tool_chain=list(ctx.tool_chain),
        )

        persist_count = 1 if omit_user_turn else 2
        await self._session_services.session_manager.append_messages(
            session,
            session.messages[-persist_count:],
        )

        # 4. build outbound (do NOT dispatch here)
        outbound = OutboundMessage(
            channel=ctx.channel,
            chat_id=ctx.chat_id,
            content=ctx.reply,
            thinking=ctx.thinking,
            media=list(ctx.media),
            metadata=dict(ctx.outbound_metadata),
        )

        return AfterReasoningResult(ctx=ctx, outbound=outbound)
