import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from agent.looping.consolidation import ConsolidationService


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


def test_consolidation_service_archive_all_and_profile_extract():
    memory = SimpleNamespace(
        read_long_term=MagicMock(return_value="MEM"),
        append_history_once=MagicMock(return_value=True),
        append_pending_once=MagicMock(return_value=True),
        save_from_consolidation=AsyncMock(),
        save_item=AsyncMock(return_value="new:profile-1"),
    )
    provider = SimpleNamespace(
        chat=AsyncMock(
            return_value=_Resp(
                '{"history_entries":["[2026-03-15 10:00] 用户聊了 Zigbee 方案"],"pending_items":[]}'
            )
        )
    )
    profile_extractor = SimpleNamespace(
        extract=AsyncMock(
            return_value=[
                SimpleNamespace(
                    summary="用户买了 Zigbee 网关",
                    category="device",
                    happened_at="2026-03-15T10:00:00",
                )
            ]
        )
    )

    service = ConsolidationService(
        memory_port=memory,
        provider=provider,
        model="m",
        memory_window=40,
        profile_extractor=profile_extractor,
    )
    session = SimpleNamespace(
        key="cli:1",
        messages=[
            {"role": "user", "content": "我买了 Zigbee 网关", "timestamp": "2026-03-15T10:00:00"},
            {"role": "assistant", "content": "记住了", "timestamp": "2026-03-15T10:01:00"},
        ],
        last_consolidated=0,
        _channel="cli",
        _chat_id="1",
    )

    asyncio.run(service.consolidate(session, archive_all=True, await_vector_store=True))

    memory.append_history_once.assert_called_once()
    memory.save_from_consolidation.assert_awaited_once()
    profile_extractor.extract.assert_awaited_once()
    memory.save_item.assert_awaited_once()
    assert session.last_consolidated == 0
