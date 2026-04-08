"""
Telegram Markdown 发送工具

将 Markdown 文本转换成 Telegram text+entities 后发送：
- 自动分段（超出 4096 字符时）
- 长代码块拆成多条富文本消息
- 转换失败时降级为纯文本
"""

import asyncio
import html
import logging
import re
from html.parser import HTMLParser

import markdown as py_markdown
from telegram import Bot, MessageEntity as TgEntity
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegramify_markdown.converter import convert_with_segments
from telegramify_markdown.entity import MessageEntity, split_entities

logger = logging.getLogger(__name__)
_STREAM_CHUNK_STEP = 120
_STREAM_PUSH_MIN_INTERVAL_S = 1.2
_STREAM_PUSH_MIN_CHARS = 80
_PARSE_ERR_RE = re.compile(r"can't parse entities|parse entities|find end of the entity", re.I)
_SPOILER_RE = re.compile(r"\|\|(.+?)\|\|", re.S)
_STRIKE_RE = re.compile(r"~~(.+?)~~", re.S)


async def _send_with_retry(
    send_coro_factory,
    *,
    label: str,
    max_attempts: int = 3,
    base_delay: float = 0.8,
) -> None:
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            await send_coro_factory()
            return
        except RetryAfter as e:
            last_err = e
            if attempt >= max_attempts:
                break
            delay = max(float(getattr(e, "retry_after", 1.0) or 1.0), base_delay)
            logger.warning(
                "[telegram] %s 命中限流，准备重试 attempt=%d/%d delay=%.1fs err=%s",
                label,
                attempt,
                max_attempts,
                delay,
                e,
            )
            await asyncio.sleep(delay)
        except (TimedOut, NetworkError) as e:
            last_err = e
            if attempt >= max_attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "[telegram] %s 发送失败，准备重试 attempt=%d/%d delay=%.1fs err=%s",
                label,
                attempt,
                max_attempts,
                delay,
                e,
            )
            await asyncio.sleep(delay)
    if last_err is not None:
        raise last_err


def _serialize_entities(entities: list[MessageEntity]) -> list[dict] | None:
    return [entity.to_dict() for entity in entities] if entities else None


def _strip_chunk(
    text: str,
    entities: list[MessageEntity],
) -> tuple[str, list[MessageEntity]]:
    leading = len(text) - len(text.lstrip("\n"))
    trailing = len(text) - len(text.rstrip("\n"))
    if leading == 0 and trailing == 0:
        return text, entities

    end = len(text) - trailing if trailing else len(text)
    stripped = text[leading:end]
    if not stripped:
        return "", []

    stripped_utf16_len = len(stripped.encode("utf-16-le")) // 2
    adjusted: list[MessageEntity] = []
    for entity in entities:
        new_offset = entity.offset - leading
        new_end = new_offset + entity.length
        if new_end <= 0 or new_offset >= stripped_utf16_len:
            continue
        new_offset = max(0, new_offset)
        new_end = min(new_end, stripped_utf16_len)
        new_length = new_end - new_offset
        if new_length <= 0:
            continue
        adjusted.append(
            MessageEntity(
                type=entity.type,
                offset=new_offset,
                length=new_length,
                url=entity.url,
                language=entity.language,
                custom_emoji_id=entity.custom_emoji_id,
            )
        )
    return stripped, adjusted


async def send_markdown(bot: Bot, chat_id: int | str, text: str) -> None:
    cid = int(chat_id)
    try:
        rendered_text, entities, _segments = convert_with_segments(text)
        chunks = split_entities(rendered_text, entities, 4090)
        for chunk_text, chunk_entities in chunks:
            chunk_text, chunk_entities = _strip_chunk(chunk_text, chunk_entities)
            if not chunk_text:
                continue
            await _send_with_retry(
                lambda: bot.send_message(
                    chat_id=cid,
                    text=chunk_text,
                    entities=_serialize_entities(chunk_entities),
                ),
                label="send_message(markdown)",
            )
    except Exception as e:
        logger.warning(f"[telegram] Markdown 转换失败，降级纯文本: {e}")
        for chunk in _split_text(text, 4090):
            await _send_with_retry(
                lambda: bot.send_message(chat_id=cid, text=chunk),
                label="send_message(plain)",
            )


def _split_text(text: str, limit: int) -> list[str]:
    """按行切分文本，每段不超过 limit 字符。"""
    chunks, current = [], []
    current_len = 0
    for line in text.splitlines(keepends=True):
        if current_len + len(line) > limit and current:
            chunks.append("".join(current))
            current, current_len = [], 0
        # 单行本身超限时强制切断
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current.append(line)
        current_len += len(line)
    if current:
        chunks.append("".join(current))
    return chunks


async def send_thinking_block(bot: Bot, chat_id: int | str, thinking: str) -> None:
    """Send thinking content as an expandable blockquote message."""
    header = "💭 思考过程\n\n"
    content = header + thinking
    utf16_len = len(content.encode("utf-16-le")) // 2
    entity = TgEntity(type="expandable_blockquote", offset=0, length=utf16_len)
    try:
        await _send_with_retry(
            lambda: bot.send_message(
                chat_id=int(chat_id),
                text=content,
                entities=[entity],
            ),
            label="send_message(thinking_block)",
        )
        logger.info("[telegram] thinking block sent, length=%d", len(thinking))
    except Exception as e:
        logger.warning("[telegram] failed to send thinking block, skipping: %s", e)


async def send_stream_markdown(bot: Bot, chat_id: int | str, text: str) -> None:
    """主动推送场景的简化流式展示。"""
    cid = int(chat_id)
    stripped = text.strip()
    if not stripped:
        return

    if cid > 0:
        try:
            stream = TelegramStreamMessage(bot, cid)
            for chunk in _iter_stream_chunks(stripped):
                await stream.push_delta(chunk, force=True)
            await stream.finalize(text)
        except Exception as e:
            logger.warning("[telegram] stream edit 失败，降级普通发送: %s", e)

    else:
        await send_markdown(bot, cid, text)


class TelegramStreamMessage:
    def __init__(self, bot: Bot, chat_id: int) -> None:
        self._bot = bot
        self._chat_id = int(chat_id)
        self._message_id: int | None = None
        self._buffer = ""
        self._last_sent_text = ""
        self._last_sent_at = 0.0

    async def push_delta(self, delta: str, *, force: bool = False) -> None:
        if self._chat_id <= 0:
            return
        self._buffer += delta
        current = self._buffer.strip()
        if not current:
            return
        now = asyncio.get_running_loop().time()
        if not force:
            grown = len(current) - len(self._last_sent_text)
            if (
                self._last_sent_text
                and grown < _STREAM_PUSH_MIN_CHARS
                and now - self._last_sent_at < _STREAM_PUSH_MIN_INTERVAL_S
            ):
                return
        await self._push_text(current)
        self._last_sent_at = now

    async def finalize(self, text: str) -> None:
        current = text.strip()
        if not current:
            return
        await self._push_text(current)

    async def _push_text(self, text: str) -> None:
        if text == self._last_sent_text:
            return
        html_text = render_telegram_preview_html(text)
        if self._message_id is None:
            sent = await _send_with_retry_result(
                lambda: _send_preview_message(self._bot, self._chat_id, html_text, text),
                label="send_message(stream_start)",
            )
            self._message_id = int(getattr(sent, "message_id", 0) or 0) or None
        elif len(text) <= 4096:
            await _send_with_retry(
                lambda: _edit_preview_message(
                    self._bot,
                    self._chat_id,
                    self._message_id,
                    html_text,
                    text,
                ),
                label="edit_message_text(stream)",
            )
        else:
            await _send_with_retry(
                lambda: _edit_preview_message(
                    self._bot,
                    self._chat_id,
                    self._message_id,
                    render_telegram_preview_html(text[:4096]),
                    text[:4096],
                ),
                label="edit_message_text(stream_truncated)",
            )
        self._last_sent_text = text


async def _send_with_retry_result(
    send_coro_factory,
    *,
    label: str,
    max_attempts: int = 3,
    base_delay: float = 0.8,
):
    last_err: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await send_coro_factory()
        except RetryAfter as e:
            last_err = e
            if attempt >= max_attempts:
                break
            delay = max(float(getattr(e, "retry_after", 1.0) or 1.0), base_delay)
            logger.warning(
                "[telegram] %s 命中限流，准备重试 attempt=%d/%d delay=%.1fs err=%s",
                label,
                attempt,
                max_attempts,
                delay,
                e,
            )
            await asyncio.sleep(delay)
        except (TimedOut, NetworkError) as e:
            last_err = e
            if attempt >= max_attempts:
                break
            delay = base_delay * (2 ** (attempt - 1))
            logger.warning(
                "[telegram] %s 发送失败，准备重试 attempt=%d/%d delay=%.1fs err=%s",
                label,
                attempt,
                max_attempts,
                delay,
                e,
            )
            await asyncio.sleep(delay)
    if last_err is not None:
        raise last_err
    raise RuntimeError(f"{label} failed without exception")


def _iter_stream_chunks(text: str) -> list[str]:
    if len(text) <= _STREAM_CHUNK_STEP:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + _STREAM_CHUNK_STEP, len(text))
        if end < len(text):
            newline = text.rfind("\n", start, end)
            if newline > start:
                end = newline + 1
        chunks.append(text[start:end])
        start = end
    return chunks


async def _send_preview_message(bot: Bot, chat_id: int, html_text: str, plain_text: str):
    try:
        return await bot.send_message(
            chat_id=chat_id,
            text=html_text,
            parse_mode="HTML",
        )
    except Exception as e:
        if not _is_telegram_html_parse_error(e):
            raise
        logger.warning("[telegram] preview HTML 解析失败，降级纯文本: %s", e)
        return await bot.send_message(chat_id=chat_id, text=plain_text)


async def _edit_preview_message(
    bot: Bot,
    chat_id: int,
    message_id: int | None,
    html_text: str,
    plain_text: str,
) -> None:
    try:
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=html_text,
            parse_mode="HTML",
        )
    except Exception as e:
        if not _is_telegram_html_parse_error(e):
            raise
        logger.warning("[telegram] preview edit HTML 解析失败，降级纯文本: %s", e)
        await bot.edit_message_text(
            chat_id=chat_id,
            message_id=message_id,
            text=plain_text,
        )


def _is_telegram_html_parse_error(err: Exception) -> bool:
    return bool(_PARSE_ERR_RE.search(str(err)))


def render_telegram_preview_html(text: str) -> str:
    prepared = _prepare_preview_markdown(text or "")
    raw_html = py_markdown.markdown(
        prepared,
        extensions=["extra", "nl2br", "sane_lists"],
        output_format="html",
    )
    parser = _TelegramPreviewHtmlParser()
    parser.feed(raw_html)
    parser.close()
    return parser.render().strip() or html.escape(text or "")


def _prepare_preview_markdown(text: str) -> str:
    text = text.replace("\r\n", "\n")
    text = re.sub(r"(?m)^\s*([-*_])\1{2,}\s*$", "", text)
    text = _STRIKE_RE.sub(lambda m: f"<s>{html.escape(m.group(1))}</s>", text)
    text = _SPOILER_RE.sub(lambda m: f"<tg-spoiler>{html.escape(m.group(1))}</tg-spoiler>", text)
    return text


class _TelegramPreviewHtmlParser(HTMLParser):
    _INLINE_TAGS = {
        "strong": "b",
        "b": "b",
        "em": "i",
        "i": "i",
        "code": "code",
        "del": "s",
        "s": "s",
        "strike": "s",
        "tg-spoiler": "tg-spoiler",
    }
    _BLOCK_TAGS = {"p", "div", "ul", "ol", "blockquote", "pre"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._list_stack: list[str] = []
        self._in_pre = False
        self._href_stack: list[str | None] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        attr_map = dict(attrs)
        if tag in self._INLINE_TAGS:
            self._parts.append(f"<{self._INLINE_TAGS[tag]}>")
            return
        if tag == "a":
            href = html.escape(attr_map.get("href") or "", quote=True)
            self._href_stack.append(href or None)
            if href:
                self._parts.append(f'<a href="{href}">')
            return
        if tag == "br":
            self._parts.append("\n")
            return
        if tag == "hr":
            self._ensure_block_break()
            return
        if tag == "li":
            bullet = "• " if not self._list_stack or self._list_stack[-1] == "ul" else "1. "
            self._ensure_block_break()
            self._parts.append(bullet)
            return
        if tag in {"ul", "ol"}:
            self._list_stack.append(tag)
            self._ensure_block_break()
            return
        if tag == "pre":
            self._in_pre = True
            self._ensure_block_break()
            self._parts.append("<pre>")
            return
        if tag == "blockquote":
            self._ensure_block_break()
            self._parts.append("<blockquote>")
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._ensure_block_break()
            self._parts.append("<b>")
            return
        if tag in self._BLOCK_TAGS:
            self._ensure_block_break()

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._INLINE_TAGS:
            self._parts.append(f"</{self._INLINE_TAGS[tag]}>")
            return
        if tag == "a":
            href = self._href_stack.pop() if self._href_stack else None
            if href:
                self._parts.append("</a>")
            return
        if tag in {"ul", "ol"}:
            if self._list_stack:
                self._list_stack.pop()
            self._ensure_block_break()
            return
        if tag == "pre":
            self._parts.append("</pre>")
            self._in_pre = False
            self._ensure_block_break()
            return
        if tag == "blockquote":
            self._parts.append("</blockquote>")
            self._ensure_block_break()
            return
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._parts.append("</b>")
            self._ensure_block_break()
            return
        if tag in {"p", "div", "li"}:
            self._ensure_block_break()

    def handle_data(self, data: str) -> None:
        if not data:
            return
        if self._in_pre:
            self._parts.append(html.escape(data))
            return
        self._parts.append(html.escape(data))

    def render(self) -> str:
        text = "".join(self._parts)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _ensure_block_break(self) -> None:
        if not self._parts:
            return
        tail = "".join(self._parts[-2:])
        if tail.endswith("\n\n"):
            return
        if tail.endswith("\n"):
            self._parts.append("\n")
        else:
            self._parts.append("\n\n")
