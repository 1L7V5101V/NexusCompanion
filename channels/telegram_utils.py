"""
Telegram Markdown 发送工具

将 Markdown 文本通过 telegramify-markdown 转换后发送：
- 自动分段（超出 4096 字符时）
- 长代码块以文件形式发送
- 转换失败时降级为纯文本
"""
import asyncio
import logging

from telegram import Bot
from telegram.error import NetworkError, RetryAfter, TimedOut
from telegramify_markdown import telegramify
from telegramify_markdown.content import ContentType

logger = logging.getLogger(__name__)


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


async def send_markdown(bot: Bot, chat_id: int | str, text: str) -> None:
    cid = int(chat_id)
    try:
        items = await telegramify(text, max_message_length=4090)
        for item in items:
            if item.content_type == ContentType.TEXT:
                await _send_with_retry(
                    lambda: bot.send_message(
                        chat_id=cid,
                        text=item.text,
                        entities=[e.to_dict() for e in item.entities],
                    ),
                    label="send_message(markdown)",
                )
            elif item.content_type == ContentType.FILE:
                await _send_with_retry(
                    lambda: bot.send_document(
                        chat_id=cid,
                        document=(item.file_name, item.file_data),
                    ),
                    label="send_document(markdown)",
                )
            elif item.content_type == ContentType.PHOTO:
                await _send_with_retry(
                    lambda: bot.send_photo(
                        chat_id=cid,
                        photo=(item.file_name, item.file_data),
                    ),
                    label="send_photo(markdown)",
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
