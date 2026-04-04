from __future__ import annotations

import inspect
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from agent.core.history_types import to_tool_call_groups
from agent.postturn.protocol import PostTurnEvent

logger = logging.getLogger("proactive_v2.turn_dispatcher")


@dataclass
class ProactiveTurnOutbound:
    session_key: str
    content: str


@dataclass
class ProactiveTurnTrace:
    source: str = "proactive"
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProactiveTurnResult:
    decision: str
    outbound: ProactiveTurnOutbound | None
    evidence: list[str] = field(default_factory=list)
    trace: ProactiveTurnTrace | None = None
    side_effects: list[Any] = field(default_factory=list)
    success_side_effects: list[Any] = field(default_factory=list)
    failure_side_effects: list[Any] = field(default_factory=list)


class ProactiveTurnDispatcher:
    """
    ┌──────────────────────────────────────┐
    │ ProactiveTurnDispatcher              │
    ├──────────────────────────────────────┤
    │ 1. 处理 skip / reply                 │
    │ 2. 落 proactive session              │
    │ 3. 调 push_tool 发送                 │
    │ 4. 执行 success / failure effects    │
    └──────────────────────────────────────┘
    """

    def __init__(
        self,
        *,
        session_manager: Any,
        push_tool: Any,
        channel: str,
        chat_id: str,
        presence: Any | None = None,
        observe_writer: Any | None = None,
        post_turn_pipeline: Any | None = None,
    ) -> None:
        self._session_manager = session_manager
        self._push_tool = push_tool
        self._channel = str(channel or "").strip()
        self._chat_id = str(chat_id or "").strip()
        self._presence = presence
        self._observe_writer = observe_writer
        self._post_turn_pipeline = post_turn_pipeline

    async def handle(self, *, result: ProactiveTurnResult, session_key: str) -> bool:
        # 1. skip 路径不发消息，只跑 side effects 并记 trace。
        if result.decision == "skip":
            self._emit_trace(
                result=result,
                session_key=session_key,
                sent=False,
            )
            await self._run_effects(result.side_effects)
            return False

        if result.outbound is None:
            raise ValueError("proactive reply result requires outbound")

        # 2. 先把主动消息写进 session，并安排 post-turn。
        session = self._session_manager.get_or_create(session_key)
        self._persist_session(
            session=session,
            content=result.outbound.content,
            result=result,
        )
        await self._session_manager.append_messages(session, session.messages[-1:])
        self._schedule_post_turn(
            session_key=session_key,
            session=session,
            result=result,
        )

        # 3. 再执行发送前 side effects，最后真正走 push_tool。
        sent = False
        try:
            await self._run_effects(result.side_effects)
            sent = await self._dispatch(result.outbound.content)
        except Exception as e:
            logger.warning("proactive outbound dispatch failed: %s", e)

        # 4. 根据发送结果执行 success / failure effects，并记 trace。
        if sent:
            if self._presence is not None:
                self._presence.record_proactive_sent(session_key)
            await self._run_effects(result.success_side_effects)
        else:
            await self._run_effects(result.failure_side_effects)

        self._emit_trace(
            result=result,
            session_key=session_key,
            sent=sent,
        )
        return sent

    async def _dispatch(self, content: str) -> bool:
        if not content or not self._channel or not self._chat_id:
            return False
        result = await self._push_tool.execute(
            channel=self._channel,
            chat_id=self._chat_id,
            message=content,
        )
        return "已发送" in str(result)

    async def _run_effects(self, effects: list[Any]) -> None:
        for effect in effects:
            try:
                maybe = effect.run()
                if inspect.isawaitable(maybe):
                    await maybe
            except Exception as e:
                logger.warning("proactive side effect failed: %s", e)

    def _persist_session(
        self,
        *,
        session: Any,
        content: str,
        result: ProactiveTurnResult,
    ) -> None:
        source_refs = []
        state_summary_tag = "none"
        if result.trace is not None and isinstance(result.trace.extra, dict):
            raw_refs = result.trace.extra.get("source_refs", [])
            if isinstance(raw_refs, list):
                source_refs = [ref for ref in raw_refs if isinstance(ref, dict)]
            state_summary_tag = str(result.trace.extra.get("state_summary_tag", "none"))
        session.add_message(
            "assistant",
            content,
            proactive=True,
            tools_used=["message_push"],
            evidence_item_ids=[str(item_id) for item_id in result.evidence],
            source_refs=source_refs,
            state_summary_tag=state_summary_tag,
        )

    def _schedule_post_turn(
        self,
        *,
        session_key: str,
        session: Any,
        result: ProactiveTurnResult,
    ) -> None:
        if self._post_turn_pipeline is None:
            return
        tool_chain = _trace_tool_chain(result.trace)
        tools_used = _trace_tools_used(result.trace)
        self._post_turn_pipeline.schedule(
            PostTurnEvent(
                session_key=session_key,
                channel=self._channel,
                chat_id=self._chat_id,
                user_message="",
                assistant_response=result.outbound.content if result.outbound else "",
                tools_used=tools_used,
                tool_chain=to_tool_call_groups(tool_chain),
                session=session,
            )
        )

    def _emit_trace(
        self,
        *,
        result: ProactiveTurnResult,
        session_key: str,
        sent: bool,
    ) -> None:
        if self._observe_writer is None:
            return
        from core.observe.events import TurnTrace as TurnTraceEvent

        trace = result.trace
        extra = trace.extra if trace is not None and isinstance(trace.extra, dict) else {}
        self._observe_writer.emit(
            TurnTraceEvent(
                source="proactive",
                session_key=session_key,
                user_msg="",
                llm_output=result.outbound.content if result.outbound else "",
                tool_calls=[
                    {
                        "name": "proactive_turn",
                        "args": json.dumps(
                            {
                                "channel": self._channel,
                                "chat_id": self._chat_id,
                                "decision": result.decision,
                                "evidence": list(result.evidence),
                                "sent": sent,
                                "steps_taken": int(extra.get("steps_taken", 0) or 0),
                                "skip_reason": str(extra.get("skip_reason", "")),
                            },
                            ensure_ascii=False,
                        ),
                        "result": "",
                    }
                ],
            )
        )


def _trace_tools_used(trace: Any | None) -> list[str]:
    if trace is None or not isinstance(trace.extra, dict):
        return []
    raw = trace.extra.get("tools_used", [])
    if not isinstance(raw, list):
        return []
    return [str(name) for name in raw if isinstance(name, str)]


def _trace_tool_chain(trace: Any | None) -> list[dict]:
    if trace is None or not isinstance(trace.extra, dict):
        return []
    raw = trace.extra.get("tool_chain", [])
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]
