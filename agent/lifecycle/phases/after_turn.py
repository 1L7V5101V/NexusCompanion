from __future__ import annotations

import copy
import logging
from typing import TYPE_CHECKING, cast

import agent.core.passive_support as support
from agent.core.types import to_tool_call_groups
from agent.lifecycle.phase import TapPhase
from agent.lifecycle.types import AfterTurnCtx, TurnSnapshot
from agent.turns.outbound import OutboundDispatch, OutboundPort
from bus.event_bus import EventBus
from bus.events import OutboundMessage
from bus.events_lifecycle import TurnCommitted

if TYPE_CHECKING:
    from agent.context import ContextBuilder
    from session.manager import Session

logger = logging.getLogger(__name__)


class AfterTurnPhase(TapPhase[TurnSnapshot, AfterTurnCtx, OutboundMessage]):

    def __init__(
        self,
        bus: EventBus,
        outbound: OutboundPort,
        context: ContextBuilder,
        history_window: int = 500,
    ) -> None:
        super().__init__(bus)
        self._outbound = outbound
        self._context = context
        self._history_window = max(1, int(history_window))

    async def _setup(self, snap: TurnSnapshot) -> AfterTurnCtx:
        state = snap.state
        msg = state.msg
        raw_session = state.session
        if raw_session is None:
            raise RuntimeError("AfterTurn requires TurnState.session")
        session = cast("Session", raw_session)

        # 1. build TurnCommitted payload
        hw = self._history_window
        post_reply_budget = support.build_post_reply_context_budget(
            context=self._context,
            history=session.get_history(max_messages=hw),
            history_window=hw,
        )
        react_stats = support.extract_react_stats(snap.ctx.context_retry)
        extra: dict[str, object] = (
            {"skip_post_memory": True}
            if (msg.metadata or {}).get("skip_post_memory")
            else {}
        )
        tool_chain_list = list(snap.ctx.tool_chain)
        omit_user_turn = bool((msg.metadata or {}).get("omit_user_turn"))

        # 2. fire TurnCommitted for internal workers (consolidation, memory, trace)
        await self._bus.fanout(
            TurnCommitted(
                session_key=state.session_key,
                channel=msg.channel,
                chat_id=msg.chat_id,
                input_message=msg.content,
                persisted_user_message=None if omit_user_turn else msg.content,
                assistant_response=snap.ctx.reply,
                tools_used=list(snap.ctx.tools_used),
                thinking=snap.ctx.thinking,
                raw_reply=snap.ctx.response_metadata.raw_text,
                meme_tag=snap.ctx.meme_tag,
                meme_media_count=len(snap.ctx.media),
                tool_chain_raw=copy.deepcopy(tool_chain_list),
                tool_call_groups=to_tool_call_groups(tool_chain_list),
                timestamp=msg.timestamp,
                retrieval_raw=state.retrieval_raw,
                post_reply_budget=dict(post_reply_budget),
                react_stats=dict(react_stats),
                extra=dict(extra),
            )
        )
        support.log_post_reply_context_budget(
            session_key=state.session_key,
            budget=post_reply_budget,
        )
        support.log_react_context_budget(
            session_key=state.session_key,
            react_stats=react_stats,
        )

        return AfterTurnCtx(
            session_key=state.session_key,
            channel=snap.outbound.channel,
            chat_id=snap.outbound.chat_id,
            reply=snap.outbound.content,
            tools_used=snap.ctx.tools_used,
            thinking=snap.ctx.thinking,
            will_dispatch=state.dispatch_outbound,
        )

    async def _finalize(self, ctx: AfterTurnCtx, snap: TurnSnapshot) -> OutboundMessage:
        outbound = snap.outbound
        if snap.state.dispatch_outbound:
            await self._outbound.dispatch(
                OutboundDispatch(
                    channel=outbound.channel,
                    chat_id=outbound.chat_id,
                    content=outbound.content,
                    thinking=outbound.thinking,
                    metadata=outbound.metadata,
                    media=outbound.media,
                )
            )
        return outbound
