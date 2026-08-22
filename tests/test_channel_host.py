from __future__ import annotations

from types import SimpleNamespace

import pytest

from bootstrap.channel_host import ChannelHost


def _make_context_factory():
    def ctx_factory(channel):
        return SimpleNamespace(
            bus=SimpleNamespace(),
            session_manager=SimpleNamespace(),
            event_bus=SimpleNamespace(),
            push_tool=SimpleNamespace(),
            attachment_store=SimpleNamespace(),
            http_resources=SimpleNamespace(),
            interrupt_controller=None,
            bot_commands=[],
            log=SimpleNamespace(),
        )

    return ctx_factory


class _Channel:
    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        fail_start: bool = False,
        fail_stop: bool = False,
    ) -> None:
        self.name = name
        self._events = events
        self._fail_start = fail_start
        self._fail_stop = fail_stop

    async def start(self, ctx: object) -> None:
        self._events.append(f"start:{self.name}")
        if self._fail_start:
            raise RuntimeError("start failed")

    async def stop(self) -> None:
        self._events.append(f"stop:{self.name}")
        if self._fail_stop:
            raise RuntimeError("stop failed")


@pytest.mark.asyncio
async def test_channel_host_start_failure_does_not_block_others():
    events: list[str] = []
    host = ChannelHost(_make_context_factory())
    host.add(_Channel("a", events))  # type: ignore[arg-type]
    host.add(_Channel("b", events, fail_start=True))  # type: ignore[arg-type]
    host.add(_Channel("c", events))  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="b"):
        await host.start_all()

    assert events == ["start:a", "start:b", "stop:b", "start:c"]


@pytest.mark.asyncio
async def test_channel_host_stops_in_reverse_order():
    events: list[str] = []
    host = ChannelHost(_make_context_factory())
    host.add(_Channel("a", events))  # type: ignore[arg-type]
    host.add(_Channel("b", events, fail_stop=True))  # type: ignore[arg-type]
    host.add(_Channel("c", events))  # type: ignore[arg-type]

    await host.start_all()
    await host.stop_all()

    assert events == [
        "start:a",
        "start:b",
        "start:c",
        "stop:c",
        "stop:b",
        "stop:a",
    ]
