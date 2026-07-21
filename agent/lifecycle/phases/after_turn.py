from __future__ import annotations

import copy
from dataclasses import dataclass, replace
import logging
from typing import TYPE_CHECKING, Any, TypeAlias, cast

from agent.core.passive_support import (
    build_post_reply_context_budget,
    extract_react_stats,
    log_post_reply_context_budget,
    log_react_context_budget,
)
from agent.control.context import current_turn_id
from agent.core.types import to_tool_call_groups
from agent.lifecycle.phase import (
    PhaseFrame,
    PhaseModule,
    collect_prefixed_slots,
    topo_sort_modules,
)
from agent.lifecycle.types import AfterTurnCtx, TurnSnapshot
from agent.turns.outbound import OutboundDispatch, OutboundPort
from bus.event_bus import EventBus
from bus.events import OutboundMessage
from bus.events_lifecycle import TurnCommitted
from logging.models import TurnLogData

if TYPE_CHECKING:
    from agent.context import ContextBuilder
    from logging.turn_logger import RoutingTurnLogger
    from session.manager import Session

logger = logging.getLogger(__name__)


@dataclass
class AfterTurnFrame(PhaseFrame[TurnSnapshot, OutboundMessage]):
    pass


AfterTurnModules: TypeAlias = list[PhaseModule[AfterTurnFrame]]


_BUDGET_SLOT = "turn:budget"
_REACT_STATS_SLOT = "turn:react_stats"
_TOOL_CHAIN_SLOT = "turn:tool_chain"
_OMIT_USER_TURN_SLOT = "turn:omit_user_turn"
_EXTRA_SLOT = "turn:extra"
_EXTRA_COLLECTED_SLOT = "turn:extra_collected"
_TURN_COMMITTED_SLOT = "turn:committed"
_CTX_SLOT = "turn:ctx"
_EXTRA_PREFIX = "turn:extra:"
_TELEMETRY_PREFIX = "turn:telemetry:"


class _BuildTurnWorkModule:
    slot = "after_turn.build_work"
    requires: tuple[str, ...] = ()

    def __init__(
        self,
        context: ContextBuilder,
        history_window: int = 500,
    ) -> None:
        self._context = context
        self._history_window = max(1, int(history_window))

    produces = (
        _BUDGET_SLOT,
        _REACT_STATS_SLOT,
        _TOOL_CHAIN_SLOT,
        _OMIT_USER_TURN_SLOT,
        _EXTRA_SLOT,
    )

    async def run(self, frame: AfterTurnFrame) -> AfterTurnFrame:
        snap = frame.input
        state = snap.state
        msg = state.msg
        raw_session = state.session
        if raw_session is None:
            raise RuntimeError("AfterTurn requires TurnState.session")
        session = cast("Session", raw_session)
        hw = self._history_window
        frame.slots[_BUDGET_SLOT] = build_post_reply_context_budget(
            context=self._context,
            history=session.get_history(max_messages=hw),
            history_window=hw,
        )
        frame.slots[_REACT_STATS_SLOT] = extract_react_stats(snap.ctx.context_retry)
        frame.slots[_EXTRA_SLOT] = (
            {"skip_post_memory": True}
            if (msg.metadata or {}).get("skip_post_memory")
            else {}
        )
        frame.slots[_TOOL_CHAIN_SLOT] = list(snap.ctx.tool_chain)
        frame.slots[_OMIT_USER_TURN_SLOT] = bool(
            (msg.metadata or {}).get("omit_user_turn")
        )
        return frame


class _BuildTurnCommittedModule:
    requires = (
        "after_turn.collect_extras",
        _BUDGET_SLOT,
        _REACT_STATS_SLOT,
        _TOOL_CHAIN_SLOT,
        _OMIT_USER_TURN_SLOT,
        _EXTRA_SLOT,
        _EXTRA_COLLECTED_SLOT,
    )
    slot = "after_turn.build_committed"
    produces = (_TURN_COMMITTED_SLOT,)

    async def run(self, frame: AfterTurnFrame) -> AfterTurnFrame:
        snap = frame.input
        state = snap.state
        msg = state.msg
        tool_chain_list = cast(list[dict[str, Any]], frame.slots[_TOOL_CHAIN_SLOT])
        omit_user_turn = bool(frame.slots[_OMIT_USER_TURN_SLOT])
        frame.slots[_TURN_COMMITTED_SLOT] = TurnCommitted(
            session_key=state.session_key,
            channel=msg.channel,
            chat_id=msg.chat_id,
            input_message=msg.content,
            persisted_user_message=None if omit_user_turn else msg.content,
            assistant_response=snap.ctx.reply,
            tools_used=list(snap.ctx.tools_used),
            turn_id=current_turn_id.get(),
            thinking=snap.ctx.thinking,
            raw_reply=snap.ctx.response_metadata.raw_text,
            meme_tag=snap.ctx.meme_tag,
            meme_media_count=len(snap.ctx.media),
            tool_chain_raw=copy.deepcopy(tool_chain_list),
            tool_call_groups=to_tool_call_groups(tool_chain_list),
            timestamp=msg.timestamp,
            post_reply_budget=dict(cast(dict[str, int], frame.slots[_BUDGET_SLOT])),
            react_stats=dict(cast(dict[str, int], frame.slots[_REACT_STATS_SLOT])),
            extra=dict(cast(dict[str, object], frame.slots[_EXTRA_SLOT])),
        )
        return frame


class _CollectAfterTurnExtraSlotsModule:
    slot = "after_turn.collect_extras"
    requires = ("after_turn.build_work", _EXTRA_SLOT)
    produces = (_EXTRA_SLOT, _EXTRA_COLLECTED_SLOT)

    async def run(self, frame: AfterTurnFrame) -> AfterTurnFrame:
        extra = dict(cast(dict[str, object], frame.slots[_EXTRA_SLOT]))
        extra.update(collect_prefixed_slots(frame.slots, _EXTRA_PREFIX))
        frame.slots[_EXTRA_SLOT] = extra
        frame.slots[_EXTRA_COLLECTED_SLOT] = True
        return frame


class _FanoutTurnCommittedModule:
    slot = "after_turn.fanout_committed"
    requires = ("after_turn.build_committed", _TURN_COMMITTED_SLOT)

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def run(self, frame: AfterTurnFrame) -> AfterTurnFrame:
        await self._bus.fanout(cast(TurnCommitted, frame.slots[_TURN_COMMITTED_SLOT]))
        return frame


class _LogBudgetModule:
    slot = "after_turn.log_budget"
    requires = ("after_turn.build_work", _BUDGET_SLOT, _REACT_STATS_SLOT)

    async def run(self, frame: AfterTurnFrame) -> AfterTurnFrame:
        state = frame.input.state
        log_post_reply_context_budget(
            session_key=state.session_key,
            budget=cast(dict[str, int], frame.slots[_BUDGET_SLOT]),
        )
        log_react_context_budget(
            session_key=state.session_key,
            react_stats=cast(dict[str, int], frame.slots[_REACT_STATS_SLOT]),
        )
        return frame


class _BuildAfterTurnCtxModule:
    slot = "after_turn.build_ctx"
    requires = ("after_turn.fanout_committed",)
    produces = (_CTX_SLOT,)

    async def run(self, frame: AfterTurnFrame) -> AfterTurnFrame:
        snap = frame.input
        state = snap.state
        frame.slots[_CTX_SLOT] = AfterTurnCtx(
            session_key=state.session_key,
            channel=snap.outbound.channel,
            chat_id=snap.outbound.chat_id,
            reply=snap.outbound.content,
            tools_used=snap.ctx.tools_used,
            thinking=snap.ctx.thinking,
            will_dispatch=state.dispatch_outbound,
        )
        return frame


class _FanoutAfterTurnCtxModule:
    slot = "after_turn.fanout_ctx"
    requires = ("after_turn.collect_telemetry", _CTX_SLOT)

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def run(self, frame: AfterTurnFrame) -> AfterTurnFrame:
        await self._bus.fanout(cast(AfterTurnCtx, frame.slots[_CTX_SLOT]))
        return frame


class _CollectAfterTurnTelemetrySlotsModule:
    slot = "after_turn.collect_telemetry"
    requires = ("after_turn.build_ctx", _CTX_SLOT)
    produces = (_CTX_SLOT,)

    async def run(self, frame: AfterTurnFrame) -> AfterTurnFrame:
        ctx = cast(AfterTurnCtx, frame.slots[_CTX_SLOT])
        extra_metadata = dict(ctx.extra_metadata)
        extra_metadata.update(collect_prefixed_slots(frame.slots, _TELEMETRY_PREFIX))
        frame.slots[_CTX_SLOT] = replace(ctx, extra_metadata=extra_metadata)
        return frame


class _DispatchOutboundModule:
    slot = "after_turn.dispatch"
    requires = ("after_turn.fanout_ctx", _CTX_SLOT)

    def __init__(self, outbound: OutboundPort) -> None:
        self._outbound = outbound

    async def run(self, frame: AfterTurnFrame) -> AfterTurnFrame:
        snap = frame.input
        outbound = snap.outbound
        if snap.state.dispatch_outbound:
            _ = await self._outbound.dispatch(
                OutboundDispatch(
                    channel=outbound.channel,
                    chat_id=outbound.chat_id,
                    content=outbound.content,
                    thinking=outbound.thinking,
                    metadata=outbound.metadata,
                    media=outbound.media,
                )
            )
        return frame


class _ReturnOutboundMessageModule:
    slot = "after_turn.return"
    requires = ("after_turn.dispatch",)

    async def run(self, frame: AfterTurnFrame) -> AfterTurnFrame:
        frame.output = frame.input.outbound
        return frame


class _LogTurnModule:
    """在 turn 完成后, 将 turn 数据写入对应的 SQLite 日志库。"""

    slot = "after_turn.log_turn"
    requires: tuple[str, ...] = ()

    def __init__(self, turn_logger: RoutingTurnLogger | None = None) -> None:
        self._turn_logger = turn_logger

    async def run(self, frame: AfterTurnFrame) -> AfterTurnFrame:
        if self._turn_logger is None:
            return frame

        snap = frame.input
        state = snap.state
        msg = state.msg
        ctx = snap.ctx

        # 从 context_retry 中提取 token 统计
        react_stats: dict[str, int] = {}
        if ctx.context_retry:
            react_stats = cast(
                dict[str, int], ctx.context_retry.get("react_stats") or {}
            )

        retry_attempts_raw: list[dict[str, object]] = (
            cast(list[dict[str, object]], ctx.context_retry.get("attempts"))
            if ctx.context_retry
            else []
        )

        data = TurnLogData(
            session_key=state.session_key,
            turn_type="passive",
            channel=msg.channel,
            chat_id=msg.chat_id,
            timestamp=(
                msg.timestamp.isoformat()
                if hasattr(msg.timestamp, "isoformat")
                else str(msg.timestamp)
            ),
            messages=ctx.initial_messages,
            tools_schema=ctx.tools_schema,
            llm_response=ctx.reply,
            tool_calls=list(ctx.tool_chain),
            input_tokens=react_stats.get("turn_input_sum_tokens", 0),
            cache_hit_tokens=react_stats.get("cache_hit_tokens", 0),
            retry_attempts=list(retry_attempts_raw),
            metadata={
                "thinking": ctx.thinking,
                "tools_used": list(ctx.tools_used),
                "streamed": ctx.streamed,
                "raw_text": ctx.response_metadata.raw_text if ctx.response_metadata else "",
                "meme_tag": ctx.meme_tag,
            },
        )

        await self._turn_logger.log(data)
        return frame


def default_after_turn_modules(
    bus: EventBus,
    outbound: OutboundPort,
    context: ContextBuilder,
    history_window: int = 500,
    plugin_modules: AfterTurnModules | None = None,
    turn_logger: RoutingTurnLogger | None = None,
) -> AfterTurnModules:
    builtins: AfterTurnModules = [
        _BuildTurnWorkModule(context, history_window),
        _CollectAfterTurnExtraSlotsModule(),
        _BuildTurnCommittedModule(),
        _FanoutTurnCommittedModule(bus),
        _LogBudgetModule(),
        _LogTurnModule(turn_logger),
        _BuildAfterTurnCtxModule(),
        _CollectAfterTurnTelemetrySlotsModule(),
        _FanoutAfterTurnCtxModule(bus),
        _DispatchOutboundModule(outbound),
        _ReturnOutboundMessageModule(),
    ]
    return cast(
        AfterTurnModules,
        topo_sort_modules(builtins + list(plugin_modules or [])),
    )
