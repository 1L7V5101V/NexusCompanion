"""Shared fixtures and test bootstrap helpers."""

import asyncio
import os
import sys
import types
from datetime import datetime, timezone, timedelta
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# Provide a lightweight openai stub in test env so imports do not fail
# when optional runtime dependency is absent.
if "openai" not in sys.modules:
    openai_stub = types.ModuleType("openai")

    class _DummyChatCompletions:
        async def create(self, *args, **kwargs):
            raise RuntimeError(
                "openai stub: AsyncOpenAI.chat.completions.create not mocked"
            )

    class _DummyChat:
        def __init__(self):
            self.completions = _DummyChatCompletions()

    class AsyncOpenAI:
        def __init__(self, *args, **kwargs):
            self.chat = _DummyChat()
            self._init_kwargs = kwargs

    openai_stub.AsyncOpenAI = AsyncOpenAI
    sys.modules["openai"] = openai_stub

# Provide lightweight telegram stubs so optional messaging deps do not block
# unrelated test collection.
if "telegram" not in sys.modules:
    telegram_stub = types.ModuleType("telegram")
    telegram_error_stub = types.ModuleType("telegram.error")

    class Bot:
        async def edit_message_text(self, *args, **kwargs):
            return True

    class TelegramMessageEntity:
        def __init__(self, *, type, offset, length):
            self.type = type
            self.offset = offset
            self.length = length

    class RetryAfter(Exception):
        def __init__(self, retry_after=1.0):
            super().__init__(f"retry after {retry_after}")
            self.retry_after = retry_after

    class NetworkError(Exception):
        pass

    class BadRequest(Exception):
        pass

    class TimedOut(Exception):
        pass

    telegram_stub.Bot = Bot
    telegram_stub.MessageEntity = TelegramMessageEntity
    telegram_error_stub.BadRequest = BadRequest
    telegram_error_stub.RetryAfter = RetryAfter
    telegram_error_stub.NetworkError = NetworkError
    telegram_error_stub.TimedOut = TimedOut
    sys.modules["telegram"] = telegram_stub
    sys.modules["telegram.error"] = telegram_error_stub

if "telegramify_markdown.converter" not in sys.modules:
    telegramify_stub = types.ModuleType("telegramify_markdown")
    converter_stub = types.ModuleType("telegramify_markdown.converter")
    entity_stub = types.ModuleType("telegramify_markdown.entity")

    class MessageEntity:
        def __init__(
            self,
            *,
            type,
            offset,
            length,
            url=None,
            language=None,
            custom_emoji_id=None,
        ):
            self.type = type
            self.offset = offset
            self.length = length
            self.url = url
            self.language = language
            self.custom_emoji_id = custom_emoji_id

        def to_dict(self):
            data = {
                "type": self.type,
                "offset": self.offset,
                "length": self.length,
            }
            if self.url is not None:
                data["url"] = self.url
            if self.language is not None:
                data["language"] = self.language
            if self.custom_emoji_id is not None:
                data["custom_emoji_id"] = self.custom_emoji_id
            return data

    def convert_with_segments(text):
        if text.startswith("```") and text.endswith("```"):
            first_newline = text.find("\n")
            code = text[first_newline + 1 : -3] if first_newline != -1 else ""
            entity = MessageEntity(type="pre", offset=0, length=len(code))
            return code, [entity], []
        return text, [], []

    def split_entities(text, entities, limit):
        chunks = []
        start = 0
        while start < len(text):
            end = min(start + limit, len(text))
            chunk_text = text[start:end]
            chunk_entities = []
            for entity in entities:
                entity_start = entity.offset
                entity_end = entity.offset + entity.length
                overlap_start = max(start, entity_start)
                overlap_end = min(end, entity_end)
                if overlap_end <= overlap_start:
                    continue
                chunk_entities.append(
                    MessageEntity(
                        type=entity.type,
                        offset=overlap_start - start,
                        length=overlap_end - overlap_start,
                        url=entity.url,
                        language=entity.language,
                        custom_emoji_id=entity.custom_emoji_id,
                    )
                )
            chunks.append((chunk_text, chunk_entities))
            start = end
        return chunks or [("", [])]

    converter_stub.convert_with_segments = convert_with_segments
    entity_stub.MessageEntity = MessageEntity
    entity_stub.split_entities = split_entities
    sys.modules["telegramify_markdown"] = telegramify_stub
    sys.modules["telegramify_markdown.converter"] = converter_stub
    sys.modules["telegramify_markdown.entity"] = entity_stub

from agent.scheduler import LatencyTracker, SchedulerService, ScheduledJob


def make_job(
    trigger="at",
    tier="instant",
    fire_at=None,
    channel="telegram",
    chat_id="123",
    message: str | None = "hello",
    prompt=None,
    name=None,
    interval_seconds=None,
    cron_expr=None,
    timezone_="UTC",
) -> ScheduledJob:
    if fire_at is None:
        fire_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    return ScheduledJob(
        trigger=trigger,
        tier=tier,
        fire_at=fire_at,
        channel=channel,
        chat_id=chat_id,
        message=message,
        prompt=prompt,
        name=name,
        interval_seconds=interval_seconds,
        cron_expr=cron_expr,
        timezone=timezone_,
    )


@pytest.fixture
def mock_push():
    m = AsyncMock()
    m.execute = AsyncMock(return_value="文本已发送")
    return m


@pytest.fixture
def mock_loop():
    m = AsyncMock()
    m.process_direct = AsyncMock(return_value="AI response")
    return m


@pytest.fixture
def fixed_now():
    return datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture
def store_path(tmp_path) -> Path:
    return tmp_path / "schedules.json"


@pytest.fixture
def tracker():
    return LatencyTracker(default=25.0, window=20)


@pytest.fixture
def service(store_path, mock_push, mock_loop, fixed_now, tracker):
    return SchedulerService(
        store_path=store_path,
        push_tool=mock_push,
        agent_loop=mock_loop,
        tracker=tracker,
        _now_fn=lambda: fixed_now,
    )


async def drain_tasks():
    """Let all pending asyncio tasks finish."""
    pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
    if pending:
        done, still_pending = await asyncio.wait(pending, timeout=1.0)
        if still_pending:
            for task in still_pending:
                task.cancel()
            await asyncio.gather(*still_pending, return_exceptions=True)
        if done:
            await asyncio.gather(*done, return_exceptions=True)


# ---------------------------------------------------------------------------
# 回归环境守卫：NEXUS_REQUIRE_PG=1 时禁止集成测试静默跳过
# ---------------------------------------------------------------------------

_DEFAULT_TEST_PG_URL = "postgresql://nexus:nexus_dev@localhost:5433/nexus"


def test_pg_url() -> str:
    """集成测试使用的 PG 连接串（与各 conftest 的 ``NEXUS_TEST_PG_URL`` 同源）。"""
    return os.environ.get("NEXUS_TEST_PG_URL", _DEFAULT_TEST_PG_URL)


def test_pg_reachable() -> bool:
    """本地集成测试 PG 是否可达（2s 超时，不抛）。"""
    import psycopg

    try:
        conn = psycopg.connect(test_pg_url(), connect_timeout=2)
    except Exception:
        return False
    conn.close()
    return True


def pytest_configure(config: pytest.Config) -> None:
    """``NEXUS_REQUIRE_PG=1`` 时，PG 不可达直接 fail 而非静默跳过。

    背景：canonical identity / control plane / migration / storage 集成测试在 PG
    不可用时整组 ``skip``（近十处 ``pytest.skip("本地 PG 不可用")`` 与三个
    conftest 的 session fixture）。于是一次看似「全绿」的回归实际可能漏掉上百条
    durability 断言——2026-09-20 的 main 回归即为 ``166 skipped``，其中 117 项是
    ``postgres`` marker。

    需要产出**可信**回归证据时，用::

        NEXUS_REQUIRE_PG=1 pytest -q -W error tests/

    此时 PG 不可达会以 usage error 中止（而非跑完再让人误读为通过）。

    默认（未设该变量）行为完全不变，保持无 docker / 无本地 PG 的开发环境可用。
    """
    if os.environ.get("NEXUS_REQUIRE_PG") != "1":
        return
    if test_pg_reachable():
        return
    raise pytest.UsageError(
        "NEXUS_REQUIRE_PG=1 但本地 PostgreSQL 不可达："
        f"{test_pg_url()}\n"
        "请先起 pgvector 开发库：\n"
        "  docker compose -f docker/debug/docker-compose.yml up -d postgres\n"
        "或用 NEXUS_TEST_PG_URL 指向可用的 pgvector 实例。\n"
        "若确实要在无 PG 环境跑（结果不可作为回归证据），去掉 NEXUS_REQUIRE_PG。"
    )
