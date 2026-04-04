from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.core.runner import CoreRunner, CoreRunnerDeps
from bus.events import InboundMessage, OutboundMessage


@pytest.mark.asyncio
async def test_core_runner_routes_passive_message_to_agent_core():
    runner = CoreRunner(
        CoreRunnerDeps(
            agent_core=SimpleNamespace(
                process=AsyncMock(
                    return_value=OutboundMessage(
                        channel="cli",
                        chat_id="1",
                        content="final",
                    )
                )
            ),
            internal_event_handler=SimpleNamespace(
                process_spawn_completion=AsyncMock()
            ),
        )
    )
    msg = InboundMessage(channel="cli", sender="hua", chat_id="1", content="hi")

    out = await runner.process(msg, "cli:1")

    assert out.content == "final"
    runner._agent_core.process.assert_awaited_once_with(
        msg,
        "cli:1",
        dispatch_outbound=True,
    )
    runner._internal_event_handler.process_spawn_completion.assert_not_awaited()


@pytest.mark.asyncio
async def test_core_runner_keeps_spawn_completion_on_internal_event_handler():
    runner = CoreRunner(
        CoreRunnerDeps(
            agent_core=SimpleNamespace(process=AsyncMock()),
            internal_event_handler=SimpleNamespace(
                process_spawn_completion=AsyncMock(
                    return_value=OutboundMessage(
                        channel="telegram",
                        chat_id="123",
                        content="spawn done",
                    )
                )
            ),
        )
    )
    msg = InboundMessage(
        channel="telegram",
        sender="spawn",
        chat_id="123",
        content="[internal spawn completed]",
        metadata={"internal_event": "spawn_completed", "spawn": {"result": "ok"}},
    )

    out = await runner.process(msg, "telegram:123", dispatch_outbound=False)

    assert out.content == "spawn done"
    runner._internal_event_handler.process_spawn_completion.assert_awaited_once_with(
        msg,
        "telegram:123",
    )
    runner._agent_core.process.assert_not_awaited()
