"""
PresenceStore — 跨 session 的用户心跳记录。

每当任意 session 收到用户消息，调用 record_user_message() 记录时间戳。
ProactiveLoop 通过此状态计算各 session 的电量/能量，决定是否主动触达。

文件格式 (presence.json):
{
  "version": 1,
  "sessions": {
    "telegram:123456": {
      "last_user_at": "2026-02-23T10:30:00+00:00",
      "last_proactive_at": "2026-02-22T15:00:00+00:00"
    }
  }
}
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.common.timekit import parse_iso as _parse_iso, utcnow as _utcnow
from infra.persistence.json_store import load_json, save_json

logger = logging.getLogger(__name__)


class PresenceStore:
    """跨 session 的用户心跳持久化。线程安全（asyncio 单线程模型下无竞争）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._state = self._load()
        logger.info(
            "[presence] 初始化完成 path=%s sessions=%d",
            self.path,
            len(self._state["sessions"]),
        )

    # ── 写入 ──────────────────────────────────────────────────────

    def record_user_message(
        self, session_key: str, now: datetime | None = None
    ) -> None:
        """用户发消息时调用，更新该 session 的最后心跳时间。"""
        now = now or _utcnow()
        sess = self._state["sessions"].setdefault(session_key, {})
        sess["last_user_at"] = now.isoformat()
        self._save()
        logger.debug(
            "[presence] 心跳更新 session=%s ts=%s", session_key, sess["last_user_at"]
        )

    def record_proactive_sent(
        self, session_key: str, now: datetime | None = None
    ) -> None:
        """主动消息发送成功时调用。"""
        now = now or _utcnow()
        sess = self._state["sessions"].setdefault(session_key, {})
        sess["last_proactive_at"] = now.isoformat()
        self._save()
        logger.debug(
            "[presence] 主动消息记录 session=%s ts=%s",
            session_key,
            sess["last_proactive_at"],
        )

    # ── 读取 ──────────────────────────────────────────────────────

    def get_last_user_at(self, session_key: str) -> datetime | None:
        """返回指定 session 最后一次用户消息时间，不存在则返回 None。"""
        sess = self._state["sessions"].get(session_key, {})
        return _parse_iso(sess.get("last_user_at"))

    def get_last_proactive_at(self, session_key: str) -> datetime | None:
        """返回指定 session 最后一次主动消息时间，不存在则返回 None。"""
        sess = self._state["sessions"].get(session_key, {})
        return _parse_iso(sess.get("last_proactive_at"))

    def most_recent_user_at(self) -> datetime | None:
        """所有 session 中最新的一次用户消息时间（全局活跃度参考）。"""
        best: datetime | None = None
        for sess in self._state["sessions"].values():
            dt = _parse_iso(sess.get("last_user_at"))
            if dt and (best is None or dt > best):
                best = dt
        return best

    def get_all_sessions(self) -> dict[str, dict[str, datetime | None]]:
        """返回所有 session 的状态快照，时间字段已解析为 datetime | None。"""
        result: dict[str, dict[str, datetime | None]] = {}
        for key, sess in self._state["sessions"].items():
            result[key] = {
                "last_user_at": _parse_iso(sess.get("last_user_at")),
                "last_proactive_at": _parse_iso(sess.get("last_proactive_at")),
            }
        return result

    # ── 内部 ──────────────────────────────────────────────────────

    def _load(self) -> dict[str, Any]:
        # 1. 读取磁盘数据
        raw = load_json(self.path, default=None, domain="presence")
        if raw is None:
            return {"version": 1, "sessions": {}}

        # 2. 规范化字段
        return {
            "version": int(raw.get("version", 1)),
            "sessions": dict(raw.get("sessions", {})),
        }

    def _save(self) -> None:
        save_json(self.path, self._state, domain="presence")
