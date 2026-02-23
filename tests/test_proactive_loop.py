from unittest.mock import AsyncMock

import pytest

from feeds.base import FeedItem
from proactive.loop import ProactiveConfig, ProactiveLoop, _parse_decision
from session.manager import SessionManager


class _DummyFeedRegistry:
    async def fetch_all(self, limit_per_source: int = 3):
        return []


class _DummyProvider:
    async def chat(self, **kwargs):
        raise RuntimeError("not used in this test")


def _build_loop(tmp_path, push_tool, chat_id: str = "7674283004", default_channel: str = "telegram"):
    session_manager = SessionManager(tmp_path)
    return ProactiveLoop(
        feed_registry=_DummyFeedRegistry(),
        session_manager=session_manager,
        provider=_DummyProvider(),
        push_tool=push_tool,
        config=ProactiveConfig(
            enabled=True,
            default_channel=default_channel,
            default_chat_id=chat_id,
        ),
        model="test-model",
        max_tokens=128,
        state_path=tmp_path / "proactive_state.json",
    ), session_manager


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


def test_parse_decision_string_false_is_false():
    d = _parse_decision(
        '{"score": 0.9, "should_send": "false", "message": "hello", "reasoning": "r"}'
    )
    assert d.should_send is False


@pytest.mark.asyncio
async def test_send_uses_configured_channel(tmp_path):
    push_tool = AsyncMock()
    push_tool.execute = AsyncMock(return_value="文本已发送")
    loop, _ = _build_loop(tmp_path, push_tool, chat_id="7674283004", default_channel="qq")

    await loop._send("主动消息")

    push_tool.execute.assert_called_once_with(
        channel="qq",
        chat_id="7674283004",
        message="主动消息",
    )


@pytest.mark.asyncio
async def test_send_writes_proactive_message_into_target_session(tmp_path):
    push_tool = AsyncMock()
    push_tool.execute = AsyncMock(return_value="文本已发送")
    loop, session_manager = _build_loop(tmp_path, push_tool)

    await loop._send("你好，这是一次主动触达")

    session = session_manager.get_or_create("telegram:7674283004")
    assert session.messages
    last = session.messages[-1]
    assert last["role"] == "assistant"
    assert last["content"] == "你好，这是一次主动触达"
    assert last.get("proactive") is True


@pytest.mark.asyncio
async def test_tick_dedupes_seen_items_and_skips_second_reflect(tmp_path):
    push_tool = AsyncMock()
    push_tool.execute = AsyncMock(return_value="文本已发送")

    feed = _DummyFeedRegistry()
    item = FeedItem(
        source_name="TestFeed",
        source_type="rss",
        title="Same News",
        content="content",
        url="https://example.com/a",
        author=None,
        published_at=None,
    )
    feed.fetch_all = AsyncMock(side_effect=[[item], [item]])

    provider = _DummyProvider()
    provider.chat = AsyncMock(
        return_value=_Resp('{"reasoning":"ok","score":0.9,"should_send":true,"message":"ping"}')
    )
    session_manager = SessionManager(tmp_path)
    loop = ProactiveLoop(
        feed_registry=feed,
        session_manager=session_manager,
        provider=provider,
        push_tool=push_tool,
        config=ProactiveConfig(
            enabled=True,
            default_channel="telegram",
            default_chat_id="7674283004",
            only_new_items_trigger=True,
        ),
        model="test-model",
        state_path=tmp_path / "proactive_state.json",
    )

    await loop._tick()
    await loop._tick()

    assert provider.chat.await_count == 1
    assert push_tool.execute.await_count == 1


@pytest.mark.asyncio
async def test_tick_delivery_dedupe_blocks_duplicate_send(tmp_path):
    push_tool = AsyncMock()
    push_tool.execute = AsyncMock(return_value="文本已发送")

    feed = _DummyFeedRegistry()
    item = FeedItem(
        source_name="TestFeed",
        source_type="rss",
        title="A",
        content="content",
        url="https://example.com/a",
        author=None,
        published_at=None,
    )
    feed.fetch_all = AsyncMock(side_effect=[[item], [item]])

    provider = _DummyProvider()
    provider.chat = AsyncMock(
        return_value=_Resp(
            '{"reasoning":"ok","score":0.9,"should_send":true,"message":"same msg","evidence_item_ids":[]}'
        )
    )
    session_manager = SessionManager(tmp_path)
    loop = ProactiveLoop(
        feed_registry=feed,
        session_manager=session_manager,
        provider=provider,
        push_tool=push_tool,
        config=ProactiveConfig(
            enabled=True,
            default_channel="telegram",
            default_chat_id="7674283004",
            only_new_items_trigger=False,
            delivery_dedupe_hours=24,
        ),
        model="test-model",
        state_path=tmp_path / "proactive_state.json",
    )

    await loop._tick()
    await loop._tick()

    assert provider.chat.await_count == 2
    assert push_tool.execute.await_count == 1
