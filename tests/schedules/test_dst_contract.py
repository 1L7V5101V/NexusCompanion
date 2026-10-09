"""6.8 IANA 时区与 DST contract（纯计算，不连 PG）。

固化 §5.9.14 / design ADR-6 的三条判据：cron 与 at 按**本地墙上时间**解析、interval
按**绝对时间**推进、跳时/重复时刻的行为可预期。America/New_York 2026 的两个边界：

- spring-forward 2026-03-08：本地 02:00→03:00，02:00–02:59 这一段墙上钟面不存在；
- fall-back 2026-11-01：本地 01:00–01:59 出现两次（先 EDT 后 EST）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from agent.scheduler import (
    compute_fire_at,
    next_cron_fire,
    parse_when_at,
    resolve_local_wall,
)

NY = "America/New_York"
SH = "Asia/Shanghai"
SPRING_GAP_START = datetime(2026, 3, 8, 7, 0, tzinfo=UTC)  # 本地 03:00 EDT


def _ny(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=ZoneInfo(NY))


def _local(dt: datetime) -> str:
    # 经 UTC 中转再回到本地：对同一个 ZoneInfo 对象调用 astimezone 是空操作，
    # 缺失钟面（02:30）会原样留着，掩盖掉真正要断言的墙上时间。
    return dt.astimezone(UTC).astimezone(ZoneInfo(NY)).strftime("%Y-%m-%d %H:%M %Z")


# ── spring-forward：缺失时刻前进到下一有效本地时刻 ──────────────────────────


def test_at_on_missing_local_time_lands_on_gap_start() -> None:
    """本地 02:30 不存在 → 触发在跳变瞬时本身（03:00 EDT），不是 03:30 也不是隔一天。"""
    fired = parse_when_at("02:30", NY, lambda: _ny("2026-03-08T00:30"))
    assert fired.astimezone(UTC) == SPRING_GAP_START
    assert _local(fired) == "2026-03-08 03:00 EDT"


def test_cron_on_missing_local_time_lands_on_gap_start() -> None:
    assert next_cron_fire("30 2 * * *", NY, _ny("2026-03-08T00:30")).astimezone(
        UTC
    ) == SPRING_GAP_START
    assert _local(next_cron_fire("30 2 * * *", NY, _ny("2026-03-07T12:00"))) == (
        "2026-03-08 03:00 EDT"
    )


def test_spring_forward_day_triggers_exactly_once() -> None:
    """缺失时刻当天只触发一次：下一次回到 03-09 的本地 02:30。"""
    first = next_cron_fire("30 2 * * *", NY, _ny("2026-03-08T00:30"))
    second = next_cron_fire("30 2 * * *", NY, first + timedelta(seconds=1))
    assert _local(second) == "2026-03-09 02:30 EDT"
    assert second.astimezone(UTC) - first.astimezone(UTC) == timedelta(
        hours=23, minutes=30
    )


# ── fall-back：重复时刻取首次出现 ──────────────────────────────────────────


def test_at_on_repeated_local_time_uses_first_occurrence() -> None:
    """本地 01:30 当天出现两次 → 取首次（EDT，05:30Z），不双触发。"""
    fired = parse_when_at("01:30", NY, lambda: _ny("2026-11-01T00:30"))
    assert fired.astimezone(UTC) == datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
    assert fired.utcoffset() == timedelta(hours=-4)


def test_repeated_local_time_is_idempotent_not_shifted() -> None:
    """同一墙上时刻重复解析得到同一个瞬时（fold=0 稳定，不随调用次数漂移）。"""
    again = parse_when_at("01:30", NY, lambda: _ny("2026-11-01T00:45"))
    assert again == parse_when_at("01:30", NY, lambda: _ny("2026-11-01T00:30"))


# ── 对照：无 DST 时区与 interval 绝对推进 ──────────────────────────────────


def test_no_dst_tenant_zone_is_plain_wall_clock() -> None:
    """Asia/Shanghai 无跳变：02:30 就是本地 02:30，与 spring 日同瞬时无需修正。"""
    fired = parse_when_at(
        "02:30", SH, lambda: datetime(2026, 3, 8, 0, 30, tzinfo=ZoneInfo(SH))
    )
    assert fired.utcoffset() == timedelta(hours=8)
    assert fired.astimezone(UTC) == datetime(2026, 3, 7, 18, 30, tzinfo=UTC)


def test_utc_zone_control() -> None:
    fired = parse_when_at("02:30", "UTC", lambda: datetime(2026, 3, 8, 0, 30, tzinfo=UTC))
    assert fired.astimezone(UTC) == datetime(2026, 3, 8, 2, 30, tzinfo=UTC)


def test_interval_advance_is_absolute_across_spring_forward() -> None:
    """interval 走绝对时长：调度态以 UTC 存算，跨跳变日每次恰好 +1h。

    必须按瞬时断言。同一个 tzinfo 下 Python 的 `+ timedelta` 与 `-` 都按**墙上钟面**
    算（相减时两侧 tzinfo 相同会被直接忽略）：本地 00:30 加 2h 落在不存在的 02:30、
    加 3h 落在 03:30，两者其实是同一个瞬时——看着「每次都 +1h」却完全不是。
    durable 路径存 TIMESTAMPTZ、比较用 UTC，推进发生在瞬时空间，DST 吃不掉一小时。
    """
    base = datetime(2026, 3, 8, 5, 30, tzinfo=UTC)  # 本地 00:30 EST
    steps = [base + timedelta(hours=i) for i in (1, 2, 3)]
    boundaries = [base, *steps]
    assert all(
        (b - a) == timedelta(hours=1) for a, b in zip(boundaries, boundaries[1:])
    )
    # 墙上钟面跳过不存在的 02:30：01:30 → 03:30 → 04:30。
    walls = [s.astimezone(ZoneInfo(NY)).strftime("%H:%M") for s in steps]
    assert walls == ["01:30", "03:30", "04:30"]

    fired = compute_fire_at(
        "every", "1h", NY, _now_fn=lambda: _ny("2026-03-08T00:30")
    )
    assert fired.astimezone(UTC) - base == timedelta(hours=1)


@pytest.mark.parametrize(
    "wall,expected_utc,note",
    [
        ("2026-03-08 02:30", SPRING_GAP_START, "缺失时刻→跳变瞬时"),
        ("2026-03-08 03:30", datetime(2026, 3, 8, 7, 30, tzinfo=UTC), "跳变后正常时刻"),
        ("2026-03-08 01:30", datetime(2026, 3, 8, 6, 30, tzinfo=UTC), "跳变前正常时刻"),
        ("2026-06-01 12:00", datetime(2026, 6, 1, 16, 0, tzinfo=UTC), "夏令时期间"),
        ("2026-01-01 12:00", datetime(2026, 1, 1, 17, 0, tzinfo=UTC), "冬令时期间"),
    ],
)
def test_resolve_local_wall_table(wall: str, expected_utc: datetime, note: str) -> None:
    """墙上时刻 → 瞬时的映射表：只有真正缺失的那一段被前移，其余逐字保留。"""
    naive = datetime.fromisoformat(wall)
    resolved = resolve_local_wall(naive, NY)
    assert resolved.astimezone(UTC) == expected_utc, note
    # 非缺失时刻必须原样保持墙上钟面（不能被归一化改动）。
    if "02:30" not in wall:
        assert resolved.strftime("%H:%M") == naive.strftime("%H:%M")
