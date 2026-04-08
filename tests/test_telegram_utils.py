from types import SimpleNamespace

import pytest

from infra.channels.telegram_utils import (
    TelegramStreamMessage,
    render_telegram_preview_html,
    send_markdown,
)


class BotStub:
    def __init__(self):
        self.messages = []
        self.edits = []
        self.document_calls = 0
        self.photo_calls = 0

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return SimpleNamespace(message_id=len(self.messages))

    async def edit_message_text(self, **kwargs):
        self.edits.append(kwargs)

    async def send_document(self, **kwargs):
        self.document_calls += 1

    async def send_photo(self, **kwargs):
        self.photo_calls += 1


@pytest.mark.asyncio
async def test_send_markdown_splits_long_code_block_into_multiple_messages():
    bot = BotStub()
    code = "print('x')\n" * 800
    markdown = f"```python\n{code}```"

    await send_markdown(bot, "123", markdown)

    assert len(bot.messages) >= 2
    assert bot.document_calls == 0
    assert bot.photo_calls == 0
    assert all(call["chat_id"] == 123 for call in bot.messages)
    assert all(call["text"].strip() for call in bot.messages)
    assert any(entity["type"] == "pre" for entity in bot.messages[0]["entities"])
    assert all(len(call["text"]) <= 4090 for call in bot.messages)


@pytest.mark.asyncio
async def test_send_markdown_falls_back_to_plain_text(monkeypatch):
    bot = BotStub()

    def fake_convert_with_segments(text):
        raise TypeError("boom")

    monkeypatch.setattr(
        "infra.channels.telegram_utils.convert_with_segments", fake_convert_with_segments
    )

    await send_markdown(bot, 456, "line1\nline2")

    assert bot.messages == [{"chat_id": 456, "text": "line1\nline2"}]


def test_render_telegram_preview_html_renders_markdown():
    html = render_telegram_preview_html("### 标题\n\n**重点**\n\n- 一\n- 二")
    assert "<b>标题</b>" in html
    assert "<b>重点</b>" in html
    assert "• 一" in html
    assert "• 二" in html


def test_render_telegram_preview_html_supports_links_strike_and_spoiler():
    html = render_telegram_preview_html("[官网](https://example.com) 和 ~~删除~~ 以及 ||隐藏||")
    assert '<a href="https://example.com">' in html
    assert "<s>删除</s>" in html
    assert "<tg-spoiler>隐藏</tg-spoiler>" in html


@pytest.mark.asyncio
async def test_stream_message_falls_back_to_plain_text_on_html_parse_error():
    bot = BotStub()

    async def broken_edit_message_text(**kwargs):
        if kwargs.get("parse_mode") == "HTML":
            raise RuntimeError("can't parse entities")
        bot.edits.append(kwargs)

    bot.edit_message_text = broken_edit_message_text
    stream = TelegramStreamMessage(bot, 123)
    await stream.push_delta("**hello**")
    await stream.finalize("**hello**\n\n- a\n- b")

    assert bot.messages[0]["parse_mode"] == "HTML"
    assert bot.edits[-1]["text"] == "**hello**\n\n- a\n- b"
