from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from proactive_v2.turn_dispatcher import (
    ProactiveTurnDispatcher,
    ProactiveTurnOutbound,
    ProactiveTurnResult,
    ProactiveTurnTrace,
)


class _Session:
    def __init__(self, key: str) -> None:
        self.key = key
        self.messages: list[dict] = []

    def add_message(self, role: str, content: str, **kwargs) -> None:
        item = {"role": role, "content": content}
        item.update(kwargs)
        self.messages.append(item)


class _Effect:
    def __init__(self) -> None:
        self.calls = 0

    async def run(self) -> None:
        self.calls += 1


@pytest.mark.asyncio
async def test_dispatcher_reply_persists_and_runs_success_effects():
    session = _Session("telegram:1")
    session_manager = SimpleNamespace(
        get_or_create=lambda _key: session,
        append_messages=AsyncMock(),
    )
    push_tool = SimpleNamespace(
        execute=AsyncMock(return_value="文本已发送")
    )
    success = _Effect()
    dispatcher = ProactiveTurnDispatcher(
        session_manager=session_manager,
        push_tool=push_tool,
        channel="telegram",
        chat_id="1",
        presence=SimpleNamespace(record_proactive_sent=lambda _key: None),
        observe_writer=None,
        post_turn_pipeline=SimpleNamespace(schedule=lambda event: None),
    )

    sent = await dispatcher.handle(
        session_key="telegram:1",
        result=ProactiveTurnResult(
            decision="reply",
            outbound=ProactiveTurnOutbound(session_key="telegram:1", content="你好"),
            evidence=["feed:1"],
            trace=ProactiveTurnTrace(extra={"state_summary_tag": "none"}),
            success_side_effects=[success],
        ),
    )

    assert sent is True
    assert session.messages[-1]["content"] == "你好"
    assert session.messages[-1]["proactive"] is True
    assert success.calls == 1


@pytest.mark.asyncio
async def test_dispatcher_skip_runs_side_effects_without_persist():
    session = _Session("telegram:1")
    session_manager = SimpleNamespace(
        get_or_create=lambda _key: session,
        append_messages=AsyncMock(),
    )
    effect = _Effect()
    dispatcher = ProactiveTurnDispatcher(
        session_manager=session_manager,
        push_tool=SimpleNamespace(execute=AsyncMock(return_value="文本已发送")),
        channel="telegram",
        chat_id="1",
        presence=None,
        observe_writer=None,
        post_turn_pipeline=None,
    )

    sent = await dispatcher.handle(
        session_key="telegram:1",
        result=ProactiveTurnResult(
            decision="skip",
            outbound=None,
            trace=ProactiveTurnTrace(extra={"skip_reason": "no_content"}),
            side_effects=[effect],
        ),
    )

    assert sent is False
    assert session.messages == []
    assert effect.calls == 1
