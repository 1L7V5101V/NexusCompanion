"""Tests for RemindTool — 日程提醒工具"""

from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from agent.scheduler import LatencyTracker, SchedulerService
from agent.tools.remind import RemindTool

_NOW = datetime(2025, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
_NOW_FN = lambda: _NOW  # noqa: E731


def make_svc(tmp_path):
    return SchedulerService(
        store_path=tmp_path / "jobs.json",
        push_tool=AsyncMock(),
        agent_loop=AsyncMock(),
        tracker=LatencyTracker(default=25.0),
        _now_fn=_NOW_FN,
    )


# ── validation ──────────────────────────────────────────────────


async def test_missing_when_returns_error(tmp_path):
    svc = make_svc(tmp_path)
    tool = RemindTool(svc)
    result = await tool.execute(
        description="组会", channel="tg", chat_id="1"
    )
    assert "错误" in result
    assert "when" in result


async def test_missing_description_returns_error(tmp_path):
    svc = make_svc(tmp_path)
    tool = RemindTool(svc)
    result = await tool.execute(
        when="14:30", channel="tg", chat_id="1"
    )
    assert "错误" in result
    assert "description" in result


async def test_missing_channel_returns_error(tmp_path):
    svc = make_svc(tmp_path)
    tool = RemindTool(svc)
    result = await tool.execute(
        when="14:30", description="组会", chat_id="1"
    )
    assert "错误" in result


async def test_invalid_when_returns_error(tmp_path):
    svc = make_svc(tmp_path)
    tool = RemindTool(svc)
    result = await tool.execute(
        when="blah", description="组会", channel="tg", chat_id="1"
    )
    assert "错误" in result


async def test_invalid_tz_returns_error(tmp_path):
    svc = make_svc(tmp_path)
    tool = RemindTool(svc)
    result = await tool.execute(
        when="14:30",
        description="组会",
        channel="tg",
        chat_id="1",
        timezone="Nowhere/Zone",
    )
    assert "错误" in result
    assert "时区" in result


# ── basic remind (no travel, no location) ──────────────────────


async def test_basic_remind_creates_three_jobs(tmp_path):
    svc = make_svc(tmp_path)
    tool = RemindTool(svc, default_tz="UTC")
    result = await tool.execute(
        when="2025-06-01T14:00:00",
        description="组会",
        channel="telegram",
        chat_id="123",
    )
    assert "错误" not in result
    assert "3 条提醒" in result or "3 条" in result
    assert len(svc._jobs) == 3

    # fire_at should be 13:30, 13:45, 13:59 (14:00 - 30/15/1 min)
    times = sorted(j.fire_at for j in svc._jobs.values())
    assert times[0].hour == 13 and times[0].minute == 30
    assert times[1].hour == 13 and times[1].minute == 45
    assert times[2].hour == 13 and times[2].minute == 59


async def test_basic_remind_all_same_name(tmp_path):
    svc = make_svc(tmp_path)
    tool = RemindTool(svc, default_tz="UTC")
    await tool.execute(
        when="2025-06-01T14:00:00",
        description="组会",
        channel="telegram",
        chat_id="123",
    )
    names = {j.name for j in svc._jobs.values()}
    assert names == {"remind:组会"}


async def test_basic_remind_messages_contain_description(tmp_path):
    svc = make_svc(tmp_path)
    tool = RemindTool(svc, default_tz="UTC")
    await tool.execute(
        when="2025-06-01T14:00:00",
        description="组会",
        channel="telegram",
        chat_id="123",
    )
    for j in svc._jobs.values():
        assert "组会" in j.message
        assert "提醒" in j.message


# ── with location ──────────────────────────────────────────────


async def test_remind_with_location(tmp_path):
    svc = make_svc(tmp_path)
    tool = RemindTool(svc, default_tz="UTC")
    await tool.execute(
        when="2025-06-01T14:00:00",
        description="座谈会",
        location="4211",
        channel="telegram",
        chat_id="123",
    )
    assert len(svc._jobs) == 3
    for j in svc._jobs.values():
        assert "4211" in j.message
        assert "座谈会" in j.message


# ── with travel_minutes (departure reminder) ──────────────────


async def test_remind_with_travel_shifts_base_time(tmp_path):
    """travel_minutes=50 at 12:00 → departure at 11:10 → reminders at 10:40, 10:55, 11:09"""
    svc = make_svc(tmp_path)
    tool = RemindTool(svc, default_tz="UTC")
    await tool.execute(
        when="2025-06-01T12:00:00",
        description="2241开会",
        location="2241",
        travel_minutes=50,
        channel="telegram",
        chat_id="123",
    )
    assert len(svc._jobs) == 3
    times = sorted(j.fire_at for j in svc._jobs.values())
    # departure = 12:00 - 50min = 11:10
    # offset 30min → 10:40, offset 15min → 10:55, offset 1min → 11:09
    assert times[0].hour == 10 and times[0].minute == 40
    assert times[1].hour == 10 and times[1].minute == 55
    assert times[2].hour == 11 and times[2].minute == 9


async def test_travel_remind_messages_are_departure_style(tmp_path):
    svc = make_svc(tmp_path)
    tool = RemindTool(svc, default_tz="UTC")
    await tool.execute(
        when="2025-06-01T12:00:00",
        description="2241开会",
        travel_minutes=50,
        channel="telegram",
        chat_id="123",
    )
    for j in svc._jobs.values():
        assert "出门提醒" in j.message
        if j.fire_at.minute == 9:  # 1min offset
            assert "该出发了" in j.message
        else:
            assert "分钟出发" in j.message


# ── edge cases ────────────────────────────────────────────────


async def test_negative_travel_returns_error(tmp_path):
    svc = make_svc(tmp_path)
    tool = RemindTool(svc, default_tz="UTC")
    result = await tool.execute(
        when="2025-06-01T14:00:00",
        description="组会",
        travel_minutes=-5,
        channel="tg",
        chat_id="1",
    )
    assert "错误" in result
    assert "正整数" in result


async def test_past_event_still_creates_jobs(tmp_path):
    """即便事件时间在过去，工具仍会创建任务（由调度器处理过期判断）"""
    svc = make_svc(tmp_path)
    tool = RemindTool(svc, default_tz="UTC")
    result = await tool.execute(
        when="2025-01-01T00:00:00",
        description="过期事件",
        channel="tg",
        chat_id="1",
    )
    # 仍然创建了3条，但调度器启动时会丢弃
    assert "错误" not in result
    assert len(svc._jobs) == 3


async def test_hhmm_format_auto_resolves(tmp_path):
    """HH:MM 格式应该被 parse_when_at 正确解析"""
    svc = make_svc(tmp_path)
    tool = RemindTool(svc, default_tz="UTC")
    result = await tool.execute(
        when="14:30",
        description="下午茶",
        channel="telegram",
        chat_id="123",
    )
    # _NOW is 12:00 UTC, 14:30 is in the future today
    assert "错误" not in result
    assert len(svc._jobs) == 3


async def test_response_format(tmp_path):
    svc = make_svc(tmp_path)
    tool = RemindTool(svc, default_tz="UTC")
    result = await tool.execute(
        when="2025-06-01T14:00:00",
        description="周会",
        channel="telegram",
        chat_id="123",
    )
    assert "错误" not in result
    assert "周会" in result
    assert "3 条" in result
    assert "cancel_schedule" in result
