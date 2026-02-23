"""
ProactiveStateStore — 主动消息流的去重状态持久化。

状态文件包含两类信息：
1) seen_items: 每个 source 下已处理过的 item_id
2) deliveries: 每个 session 下已发送过的 delivery_key
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


class ProactiveStateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load()
        logger.info(
            "[proactive.state] 初始化完成 path=%s seen_sources=%d delivery_sessions=%d",
            self.path,
            len(self._state["seen_items"]),
            len(self._state["deliveries"]),
        )

    def is_item_seen(
        self,
        source_key: str,
        item_id: str,
        ttl_hours: int,
        now: datetime | None = None,
    ) -> bool:
        now = now or _utcnow()
        source_map = self._state["seen_items"].get(source_key, {})
        ts = _parse_iso(source_map.get(item_id))
        if ts is None:
            return False
        if ts < now - timedelta(hours=max(ttl_hours, 1)):
            logger.info(
                "[proactive.state] item 过期，视为未见 source=%s item_id=%s ts=%s ttl_hours=%d",
                source_key,
                item_id[:16],
                source_map.get(item_id),
                ttl_hours,
            )
            return False
        return True

    def mark_items_seen(
        self,
        entries: list[tuple[str, str]],
        now: datetime | None = None,
    ) -> None:
        if not entries:
            logger.info("[proactive.state] mark_items_seen: entries 为空，跳过")
            return
        now = now or _utcnow()
        ts = now.isoformat()
        added = 0
        for source_key, item_id in entries:
            source_map = self._state["seen_items"].setdefault(source_key, {})
            if item_id not in source_map:
                added += 1
            source_map[item_id] = ts
        self._save()
        logger.info(
            "[proactive.state] 已记录已见条目 count=%d newly_added=%d ts=%s",
            len(entries),
            added,
            ts,
        )

    def is_delivery_duplicate(
        self,
        session_key: str,
        delivery_key: str,
        window_hours: int,
        now: datetime | None = None,
    ) -> bool:
        now = now or _utcnow()
        sess = self._state["deliveries"].get(session_key, {})
        ts = _parse_iso(sess.get(delivery_key))
        if ts is None:
            return False
        if ts >= now - timedelta(hours=max(window_hours, 1)):
            logger.info(
                "[proactive.state] 命中发送去重 session=%s delivery_key=%s ts=%s window_hours=%d",
                session_key,
                delivery_key[:16],
                sess.get(delivery_key),
                window_hours,
            )
            return True
        return False

    def mark_delivery(
        self,
        session_key: str,
        delivery_key: str,
        now: datetime | None = None,
    ) -> None:
        now = now or _utcnow()
        ts = now.isoformat()
        sess = self._state["deliveries"].setdefault(session_key, {})
        sess[delivery_key] = ts
        self._save()
        logger.info(
            "[proactive.state] 已记录发送 session=%s delivery_key=%s ts=%s",
            session_key,
            delivery_key[:16],
            ts,
        )

    def cleanup(self, seen_ttl_hours: int, delivery_ttl_hours: int) -> None:
        now = _utcnow()
        seen_cutoff = now - timedelta(hours=max(seen_ttl_hours, 1))
        delivery_cutoff = now - timedelta(hours=max(delivery_ttl_hours, 1))

        removed_seen = 0
        for source_key in list(self._state["seen_items"].keys()):
            source_map = self._state["seen_items"][source_key]
            for item_id in list(source_map.keys()):
                ts = _parse_iso(source_map[item_id])
                if ts is None or ts < seen_cutoff:
                    del source_map[item_id]
                    removed_seen += 1
            if not source_map:
                del self._state["seen_items"][source_key]

        removed_delivery = 0
        for session_key in list(self._state["deliveries"].keys()):
            sess = self._state["deliveries"][session_key]
            for delivery_key in list(sess.keys()):
                ts = _parse_iso(sess[delivery_key])
                if ts is None or ts < delivery_cutoff:
                    del sess[delivery_key]
                    removed_delivery += 1
            if not sess:
                del self._state["deliveries"][session_key]

        self._save()
        logger.info(
            "[proactive.state] cleanup 完成 removed_seen=%d removed_delivery=%d",
            removed_seen,
            removed_delivery,
        )

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "seen_items": {}, "deliveries": {}}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            state = {
                "version": int(raw.get("version", 1)),
                "seen_items": dict(raw.get("seen_items", {})),
                "deliveries": dict(raw.get("deliveries", {})),
            }
            logger.info("[proactive.state] 从磁盘加载状态成功 path=%s", self.path)
            return state
        except Exception as e:
            logger.warning("[proactive.state] 加载失败，回退空状态: %s", e)
            return {"version": 1, "seen_items": {}, "deliveries": {}}

    def _save(self) -> None:
        self.path.write_text(
            json.dumps(self._state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        logger.info("[proactive.state] 状态已保存 path=%s", self.path)
