import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from agent.looping.consolidation import (
    ConsolidationService,
    _select_recent_history_entries,
)


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content


def test_consolidation_service_archive_all_and_profile_extract():
    """consolidation 触发两次并行 LLM 调用：一次提取 event，一次提取 profile/preference/procedure。"""
    memory = SimpleNamespace(
        read_long_term=MagicMock(return_value="MEM"),
        read_history=MagicMock(
            return_value=(
                "[2026-03-15 09:00] 用户确认 Zigbee 需求\n\n"
                "[2026-03-15 09:30] 用户对本地控制方案感兴趣\n\n"
                "[2026-03-15 09:45] 用户准备下单 Zigbee 网关"
            )
        ),
        append_history_once=MagicMock(return_value=True),
        append_pending_once=MagicMock(return_value=True),
        save_from_consolidation=AsyncMock(),
        save_item=AsyncMock(return_value="new:profile-1"),
        save_item_with_supersede=AsyncMock(return_value="new:profile-1"),
    )
    provider = SimpleNamespace(
        chat=AsyncMock(side_effect=[
            # Call 1: event extraction
            _Resp('{"history_entries":["[2026-03-15 10:00] 用户聊了 Zigbee 方案"],"pending_items":[]}'),
            # Call 2: combined long-term extraction (profile + preference + procedure)
            _Resp('{"profile":[{"summary":"用户买了 Zigbee 网关","category":"purchase","happened_at":"2026-03-15"}],"preference":[],"procedure":[]}'),
        ])
    )

    service = ConsolidationService(
        memory_port=memory,
        provider=provider,
        model="m",
        keep_count=20,
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

    asyncio.run(service.consolidate(session, archive_all=True))

    memory.append_history_once.assert_called_once()
    memory.save_from_consolidation.assert_awaited_once()
    # 两次 LLM 调用：event + 长期记忆
    assert provider.chat.await_count == 2
    # event call 的 prompt 包含 history context
    event_prompt = provider.chat.await_args_list[0].kwargs["messages"][1]["content"]
    assert "## 最近三次 consolidation event" in event_prompt
    assert "用户准备下单 Zigbee 网关" in event_prompt
    assert "不能作为人物身份、说话人归属、关系判断或具体事实归属的直接证据" in event_prompt
    # 长期记忆 call 保存了 profile（走 save_item_with_supersede）
    memory.save_item_with_supersede.assert_awaited_once()
    assert session.last_consolidated == 0


def test_consolidation_service_uses_profile_maint_for_file_side_io():
    memory_port = SimpleNamespace(
        save_from_consolidation=AsyncMock(),
        save_item=AsyncMock(return_value="new:profile-1"),
    )
    profile_maint = SimpleNamespace(
        read_long_term=MagicMock(return_value="MEM"),
        read_history=MagicMock(return_value=""),
        append_history_once=MagicMock(return_value=True),
        append_pending_once=MagicMock(return_value=True),
    )
    provider = SimpleNamespace(
        chat=AsyncMock(
            return_value=_Resp(
                '{"history_entries":["[2026-03-15 10:00] 用户聊了 Zigbee 方案"],"pending_items":[]}'
            )
        )
    )
    service = ConsolidationService(
        memory_port=memory_port,
        profile_maint=profile_maint,
        provider=provider,
        model="m",
        keep_count=20,
        profile_extractor=None,
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

    asyncio.run(service.consolidate(session, archive_all=True))

    profile_maint.read_long_term.assert_called_once()
    profile_maint.append_history_once.assert_called_once()
    memory_port.save_from_consolidation.assert_awaited_once()


def test_consolidation_event_failure_does_not_write_implicit_long_term():
    """当 event 提取返回空响应时，隐式长期记忆（profile/preference/procedure）不应写库。
    验证 Issue-1 修复：即使 implicit_task 已先于 event 完成，也不会在 event 失败时写入，
    从而保留同窗口重跑的幂等语义。
    """
    memory = SimpleNamespace(
        read_long_term=MagicMock(return_value="MEM"),
        read_history=MagicMock(return_value=""),
        append_history_once=MagicMock(return_value=True),
        append_pending_once=MagicMock(return_value=True),
        save_from_consolidation=AsyncMock(),
        save_item=AsyncMock(return_value="new:profile-1"),
        save_item_with_supersede=AsyncMock(return_value="new:profile-1"),
    )

    # implicit LLM 调用返回合法 profile，event LLM 调用返回空（失败路径）。
    # 用 asyncio.Event 强制让 implicit 调用比 event 调用更早完成，模拟竞态。
    implicit_done = asyncio.Event()

    async def _chat_side_effect(**kwargs):
        messages = kwargs.get("messages", [])
        content = (messages[0].get("content") or "") if messages else ""
        if "procedure" in content or "preference" in content or "profile" in content:
            # implicit 调用：立即返回合法结果
            implicit_done.set()
            return _Resp('{"profile":[{"summary":"用户买了 Zigbee 网关","category":"purchase","happened_at":"2026-03-15"}],"preference":[],"procedure":[]}')
        else:
            # event 调用：等 implicit 完成后再返回空响应，确保竞态场景
            await implicit_done.wait()
            return _Resp("")

    provider = SimpleNamespace(chat=AsyncMock(side_effect=_chat_side_effect))

    service = ConsolidationService(
        memory_port=memory,
        provider=provider,
        model="m",
        keep_count=20,
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

    asyncio.run(service.consolidate(session, archive_all=True))

    # event 失败 → last_consolidated 不推进，隐式结果不写库
    assert session.last_consolidated == 0
    memory.save_item_with_supersede.assert_not_awaited()
    memory.append_history_once.assert_not_called()


def test_select_recent_history_entries_returns_last_three_chunks():
    history = (
        "[2026-03-15 09:00] A\n\n"
        "[2026-03-15 09:10] B\n\n"
        "[2026-03-15 09:20] C\n\n"
        "[2026-03-15 09:30] D"
    )
    assert _select_recent_history_entries(history, limit=3) == [
        "[2026-03-15 09:10] B",
        "[2026-03-15 09:20] C",
        "[2026-03-15 09:30] D",
    ]
