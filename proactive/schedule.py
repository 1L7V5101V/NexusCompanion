"""
proactive/schedule.py — 用户作息配置的动态存储。

从独立的 schedule.json 读取 quiet_hours 设置，
与 config.json 解耦，允许随时修改而无需重启。

文件格式 (schedule.json):
{
  "quiet_hours_start": 23,
  "quiet_hours_end": 8,
  "quiet_hours_weight": 0.0
}

任意字段缺失时回退到 ProactiveConfig 中的默认值。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ScheduleStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        """每次调用都从磁盘读取，确保修改即时生效。"""
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning("[schedule] 读取失败，忽略: %s", e)
            return {}

    def quiet_hours_start(self, default: int) -> int:
        return int(self.load().get("quiet_hours_start", default))

    def quiet_hours_end(self, default: int) -> int:
        return int(self.load().get("quiet_hours_end", default))

    def quiet_hours_weight(self, default: float) -> float:
        return float(self.load().get("quiet_hours_weight", default))
