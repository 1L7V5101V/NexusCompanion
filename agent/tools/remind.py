"""
日程提醒工具：RemindTool

用户说"提醒我XXX"时自动调用，一次创建三条提醒（提前30/15/1分钟）。
支持地点信息、路程时间推导出门提醒。
"""

import logging
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from agent.scheduler import (
    ScheduledJob,
    SchedulerService,
    compute_fire_at,
)
from agent.tools.base import Tool

logger = logging.getLogger(__name__)

_ADVANCE_OFFSETS = [30, 15, 1]
"""默认提前量（分钟）"""


class RemindTool(Tool):
    name = "remind"
    description = (
        "设置日程提醒。用户提到'提醒我'、'设个提醒'、'帮我记着'等时调用此工具。\n"
        "自动创建提前30分钟、15分钟、1分钟三条提醒消息。\n"
        "支持填写地点（location）和路程时间（travel_minutes）。\n"
        "如有路程时间，会自动推算出发时间并发出门提醒。\n"
        "多条提醒共用同一任务名，可用 cancel_schedule 按名称一次取消全部。\n\n"
        "示例（AI应将自然语言时间转为ISO格式再调用）：\n"
        "  用户说'周三12:30组会提醒' → "
        "remind(when='2025-06-04T12:30', description='组会')\n"
        "  用户说'明天12:00在2241有会，路上50分钟' → "
        "remind(when='2025-06-02T12:00', description='2241开会', travel_minutes=50)"
    )
    parameters = {
        "type": "object",
        "properties": {
            "when": {
                "type": "string",
                "description": (
                    "事件时间。支持格式：\n"
                    "  HH:MM — 如 '14:30'，自动判断今天/明天\n"
                    "  ISO — 如 '2025-06-01T09:00'"
                ),
            },
            "description": {
                "type": "string",
                "description": "事件描述，如 '组会'、'座谈会'、'2241开会'",
            },
            "location": {
                "type": "string",
                "description": "地点（可选），如 '4211'、'3楼会议室'",
            },
            "travel_minutes": {
                "type": "integer",
                "description": "路上所需分钟数（可选）。填了则推算出发时间，发出门提醒",
            },
            "channel": {
                "type": "string",
                "description": "目标渠道，如 telegram、qq",
            },
            "chat_id": {
                "type": "string",
                "description": "目标会话 ID",
            },
            "timezone": {
                "type": "string",
                "description": "时区，如 Asia/Shanghai，默认使用系统配置",
            },
        },
        "required": ["when", "description", "channel", "chat_id"],
    }

    def __init__(self, service: SchedulerService, default_tz: str = "UTC") -> None:
        self._service = service
        self._default_tz = default_tz

    async def execute(self, **kwargs: Any) -> str:
        when = kwargs.get("when", "")
        description = kwargs.get("description", "")
        location = kwargs.get("location") or ""
        travel_minutes = kwargs.get("travel_minutes")
        channel = kwargs.get("channel", "")
        chat_id = str(kwargs.get("chat_id", ""))
        tz = kwargs.get("timezone") or self._default_tz

        # ── validation ──
        if not when:
            return "错误：when（事件时间）为必填项"
        if not description:
            return "错误：description（事件描述）为必填项"
        if not channel or not chat_id:
            return "错误：channel 和 chat_id 为必填项"

        try:
            ZoneInfo(tz)
        except ZoneInfoNotFoundError:
            return f"错误：无效的时区 {tz!r}"

        # ── compute event time ──
        try:
            event_time = compute_fire_at("at", when, tz)
        except ValueError as e:
            return f"错误：{e}"

        # ── determine base time and reminder type ──
        is_departure_reminder = travel_minutes is not None
        if is_departure_reminder:
            try:
                travel_m = int(travel_minutes)
            except (TypeError, ValueError):
                return f"错误：travel_minutes 须为整数，收到 {travel_minutes!r}"
            if travel_m <= 0:
                return f"错误：travel_minutes 须为正整数，收到 {travel_m}"
            base_time = event_time - timedelta(minutes=travel_m)
        else:
            base_time = event_time

        # ── build context parts ──
        loc_part = f"（{location}）" if location else ""
        travel_part = f"，路上{travel_minutes}分钟" if is_departure_reminder else ""

        # ── create 3 schedule jobs ──
        created: list[dict[str, Any]] = []
        for offset in _ADVANCE_OFFSETS:
            fire_at = base_time - timedelta(minutes=offset)

            # build contextual message
            if is_departure_reminder:
                if offset == 1:
                    msg = (
                        f"⏰ 出门提醒：{description}{loc_part}（{self._format_time(event_time, tz)}）"
                        f"{travel_part}，该出发了！"
                    )
                else:
                    msg = (
                        f"⏰ 出门提醒：{description}{loc_part}（{self._format_time(event_time, tz)}）"
                        f"还有{offset}分钟出发{travel_part}"
                    )
            else:
                if offset == 1:
                    msg = (
                        f"⏰ 提醒：{description}{loc_part}（{self._format_time(event_time, tz)}）"
                        f"马上开始了！"
                    )
                elif offset == 15:
                    msg = (
                        f"⏰ 提醒：{description}{loc_part}（{self._format_time(event_time, tz)}）"
                        f"还有{offset}分钟开始"
                    )
                else:
                    msg = (
                        f"⏰ 提醒：{description}{loc_part}（{self._format_time(event_time, tz)}）"
                        f"还有{offset}分钟开始"
                    )

            job_name = f"remind:{description}"
            job = ScheduledJob(
                trigger="at",
                tier="instant",
                fire_at=fire_at,
                channel=channel,
                chat_id=chat_id,
                message=msg,
                name=job_name,
                timezone=tz,
            )
            self._service.add_job(job)
            created.append({"offset": offset, "fire_at": fire_at})

        if not created:
            return "错误：所有提醒时间均已过期，请设置未来的时间"

        # ── build response ──
        label = f"「{description}」"
        lines = [f"已为{label}创建 {len(created)} 条提醒："]
        reminder_type = "出门" if is_departure_reminder else "日程"
        for item in created:
            try:
                local = item["fire_at"].astimezone(ZoneInfo(tz))
                time_str = local.strftime("%H:%M")
            except Exception:
                time_str = item["fire_at"].strftime("%H:%M")
            offset = item["offset"]
            lines.append(f"  • {time_str}  — {reminder_type}提醒（提前{offset}分钟）")

        base_str = self._format_time(base_time, tz)
        if is_departure_reminder:
            lines.append(f"出发时间：{base_str}，事件时间：{self._format_time(event_time, tz)}")
        lines.append(f"可用 cancel_schedule(name=\"{job_name}\") 一键取消全部")
        return "\n".join(lines)

    @staticmethod
    def _format_time(dt: datetime, tz: str) -> str:
        try:
            return dt.astimezone(ZoneInfo(tz)).strftime("%H:%M")
        except Exception:
            return dt.strftime("%H:%M")
