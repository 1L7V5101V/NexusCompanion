"""
FeedStore — 订阅信息的 JSON 持久化存储。
设计对标 JobStore（agent/scheduler.py）。
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from feeds.base import FeedSubscription

logger = logging.getLogger(__name__)


class FeedStore:
    """JSON 文件持久化，读写 FeedSubscription 列表。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> list[FeedSubscription]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return [self._from_dict(d) for d in raw]
        except Exception as e:
            logger.warning(f"FeedStore load failed: {e}")
            return []

    def save(self, subs: dict[str, FeedSubscription]) -> None:
        data = [self._to_dict(s) for s in subs.values()]
        self.path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def add(self, sub: FeedSubscription) -> None:
        subs = {s.id: s for s in self.load()}
        subs[sub.id] = sub
        self.save(subs)

    def remove(self, sub_id: str) -> bool:
        subs = {s.id: s for s in self.load()}
        if sub_id not in subs:
            return False
        del subs[sub_id]
        self.save(subs)
        return True

    def list_enabled(self) -> list[FeedSubscription]:
        return [s for s in self.load() if s.enabled]

    def find_by_name(self, name: str) -> list[FeedSubscription]:
        name_lower = name.lower()
        return [s for s in self.load() if name_lower in s.name.lower()]

    # ── private ──

    def _to_dict(self, sub: FeedSubscription) -> dict[str, Any]:
        d = asdict(sub)
        d["added_at"] = sub.added_at.isoformat()
        return d

    def _from_dict(self, d: dict[str, Any]) -> FeedSubscription:
        d = dict(d)
        if "added_at" in d:
            dt = datetime.fromisoformat(d["added_at"])
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            d["added_at"] = dt
        return FeedSubscription(**d)
