from __future__ import annotations
from typing import Any, Callable, cast

import pytest

from agent.plugins.specs import ProactiveSourceSpec, RegisteredProactiveSource
from proactive_v2 import mcp_sources


class _FakePool:
    def __init__(
        self,
        responses: dict[tuple[str, str], object | Callable[[dict], object]],
        failures: set[tuple[str, str]] | None = None,
    ) -> None:
        self._responses = responses
        self._failures = failures or set()
        self.calls: list[tuple[str, str, dict]] = []
        self.timeouts: list[float | None] = []

    async def call(
        self,
        server: str,
        tool_name: str,
        args: dict[str, Any],
        *,
        timeout: float | None = None,
    ):
        self.calls.append((server, tool_name, dict(args)))
        self.timeouts.append(timeout)
        if (server, tool_name) in self._failures:
            raise RuntimeError(f"failed: {server}.{tool_name}")
        response = self._responses[(server, tool_name)]
        if callable(response):
            return response(args)
        return response


def _source(
    plugin_id: str,
    spec_id: str,
    channels: tuple[str, ...],
    server: str,
    fetch_tool: str,
    ack_tool: str = "",
    fetch_page_size: int = 0,
) -> RegisteredProactiveSource:
    return RegisteredProactiveSource(
        plugin_id=plugin_id,
        spec=ProactiveSourceSpec(
            id=spec_id,
            channels=cast(Any, channels),
            server=server,
            fetch_tool=fetch_tool,
            ack_tool=ack_tool,
            fetch_page_size=fetch_page_size,
        ),
    )


@pytest.mark.asyncio
async def test_fetch_sources_async_filters_kind_and_sets_ack_server():
    sources = [
        _source("p1", "s1", ("alert",), "s1", "get_proactive_events"),
        _source("p1", "ctx", ("context",), "ctx", "get_context"),
    ]
    pool = _FakePool(
        {
            ("s1", "get_proactive_events"): [
                {"kind": "alert", "event_id": "a1"},
                {"kind": "content", "event_id": "c1"},
            ],
            ("ctx", "get_context"): {"available": True},
        }
    )

    result = await mcp_sources.fetch_sources_async(cast(Any, pool), sources)

    assert result == {
        "alert": [{"kind": "alert", "event_id": "a1", "ack_server": "p1:s1"}],
        "content": [],
        "context": [{"available": True, "_source": "p1:ctx"}],
    }


@pytest.mark.asyncio
async def test_fetch_sources_async_keeps_items_of_all_configured_channels():
    sources = [
        _source("p1", "s1", ("content", "alert"), "s1", "get_proactive_events"),
    ]
    pool = _FakePool(
        {
            ("s1", "get_proactive_events"): [
                {"kind": "content", "event_id": "n1"},
                {"kind": "alert", "event_id": "a1"},
            ],
        }
    )

    result = await mcp_sources.fetch_sources_async(cast(Any, pool), sources)

    assert result["alert"] == [{"kind": "alert", "event_id": "a1", "ack_server": "p1:s1"}]
    assert result["content"] == [{"kind": "content", "event_id": "n1", "ack_server": "p1:s1"}]


@pytest.mark.asyncio
async def test_fetch_sources_async_raises_when_all_sources_failed():
    sources = [
        _source("p1", "s1", ("content",), "s1", "poll"),
        _source("p1", "s2", ("content",), "s2", "poll"),
    ]
    pool = _FakePool(
        {
            ("s1", "poll"): {"ok": True},
            ("s2", "poll"): {"ok": True},
        },
        failures={("s1", "poll"), ("s2", "poll")},
    )

    with pytest.raises(RuntimeError) as exc:
        await mcp_sources.fetch_sources_async(cast(Any, pool), sources)

    assert "p1:s1" in str(exc.value)
    assert "p1:s2" in str(exc.value)


@pytest.mark.asyncio
async def test_fetch_sources_async_keeps_successes_when_partial_failure():
    sources = [
        _source("p1", "s1", ("alert",), "s1", "get_proactive_events"),
        _source("p1", "bad", ("content",), "bad", "poll"),
    ]
    pool = _FakePool(
        {
            ("s1", "get_proactive_events"): [{"kind": "alert", "event_id": "a1"}],
            ("bad", "poll"): [],
        },
        failures={("bad", "poll")},
    )

    result = await mcp_sources.fetch_sources_async(cast(Any, pool), sources)

    assert result["alert"] == [{"kind": "alert", "event_id": "a1", "ack_server": "p1:s1"}]
    assert result["content"] == []


@pytest.mark.asyncio
async def test_fetch_source_strict_async_rejects_item_without_event_id():
    source = _source("p1", "s1", ("alert",), "s1", "get_proactive_events")
    pool = _FakePool({("s1", "get_proactive_events"): [{"kind": "alert"}]})

    with pytest.raises(RuntimeError):
        await mcp_sources.fetch_source_strict_async(cast(Any, pool), source)


@pytest.mark.asyncio
async def test_fetch_source_strict_async_paginates_when_page_size_set():
    source = _source("p1", "s1", ("content",), "s1", "poll", fetch_page_size=2)
    pool = _FakePool(
        {
            ("s1", "poll"): lambda args: (
                [
                    {"kind": "content", "event_id": "n1"},
                    {"kind": "content", "event_id": "n2"},
                ]
                if args["offset"] == 0
                else [{"kind": "content", "event_id": "n3"}]
            ),
        }
    )

    result = await mcp_sources.fetch_source_strict_async(cast(Any, pool), source)

    assert len(result["content"]) == 3
    assert ("s1", "poll", {"offset": 0, "limit": 2}) in pool.calls
    assert ("s1", "poll", {"offset": 2, "limit": 2}) in pool.calls


@pytest.mark.asyncio
async def test_acknowledge_async_dispatches_to_source_ack_tool_with_feedback():
    sources = [
        _source("p1", "s1", ("content",), "s1", "fetch", ack_tool="ack_events"),
        _source("p1", "s2", ("content",), "s2", "fetch", ack_tool="ack_events"),
    ]
    pool = _FakePool(
        {
            ("s1", "ack_events"): {"ok": True},
            ("s2", "ack_events"): {"ok": True},
        }
    )

    await mcp_sources.acknowledge_async(
        cast(Any, pool), sources, "p1:s1", ["a1", "a2"], feedback="good"
    )

    assert ("s1", "ack_events", {"event_ids": ["a1", "a2"], "feedback": "good"}) in pool.calls
    assert ("s2", "ack_events", {}) not in pool.calls


@pytest.mark.asyncio
async def test_acknowledge_async_skips_without_ack_tool_or_events():
    no_ack = _source("p1", "s1", ("content",), "s1", "fetch")
    no_events = _source("p1", "s2", ("content",), "s2", "fetch", ack_tool="ack_events")
    pool = _FakePool({("s2", "ack_events"): {"ok": True}})

    await mcp_sources.acknowledge_async(cast(Any, pool), [no_ack, no_events], "p1:s1", [])
    await mcp_sources.acknowledge_async(cast(Any, pool), [no_ack, no_events], "p1:s2", [])
    await mcp_sources.acknowledge_async(cast(Any, pool), [no_ack, no_events], "unknown", ["x"])

    assert pool.calls == []


def test_source_key_combines_plugin_and_spec_id():
    source = _source("p1", "s1", ("content",), "s1", "fetch")
    assert mcp_sources.source_key(source) == "p1:s1"
