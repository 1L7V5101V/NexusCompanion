from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

from core.common.timekit import parse_iso as _parse_iso, utcnow as _utcnow
from infra.storage.interfaces import SessionStorage, TenantContext
from infra.storage.tenancy import assert_tenant_resolved

logger = logging.getLogger(__name__)


class PresenceStore:
    """跨 session 的用户心跳持久化，底层复用 session 存储（tenant-bound view）。

    持有 `store_for` 闭包按调用时的显式 tenant 解析 view；sqlite 下所有 tenant
    落到同一共享 store（显式 single-user），PG 下按 `WHERE tenant_id` 隔离。
    """

    def __init__(self, store_for: Callable[[TenantContext], SessionStorage]) -> None:
        self._store_for = store_for
        logger.info("[presence] 初始化完成 (tenant-scoped session view)")

    def _view(self, tenant_id: str) -> SessionStorage:
        return self._store_for(TenantContext(tenant_id=assert_tenant_resolved(tenant_id)))

    def record_user_message(
        self, tenant_id: str, session_key: str, now: datetime | None = None
    ) -> None:
        ts = (now or _utcnow()).isoformat()
        self._view(tenant_id).update_presence(session_key, last_user_at=ts)
        logger.debug("[presence] 心跳更新 tenant=%s session=%s ts=%s", tenant_id, session_key, ts)

    def record_proactive_sent(
        self, tenant_id: str, session_key: str, now: datetime | None = None
    ) -> None:
        ts = (now or _utcnow()).isoformat()
        self._view(tenant_id).update_presence(session_key, last_proactive_at=ts)
        logger.debug("[presence] 主动消息记录 tenant=%s session=%s ts=%s", tenant_id, session_key, ts)

    def get_last_user_at(self, tenant_id: str, session_key: str) -> datetime | None:
        row = self._view(tenant_id).get_presence(session_key) or {}
        return _parse_iso(row.get("last_user_at"))

    def get_last_proactive_at(self, tenant_id: str, session_key: str) -> datetime | None:
        row = self._view(tenant_id).get_presence(session_key) or {}
        return _parse_iso(row.get("last_proactive_at"))

    def most_recent_user_at(self, tenant_id: str) -> datetime | None:
        return _parse_iso(self._view(tenant_id).most_recent_user_at())

    def get_all_sessions(
        self, tenant_id: str
    ) -> dict[str, dict[str, datetime | None]]:
        rows = self._view(tenant_id).list_presence()
        return {
            key: {
                "last_user_at": _parse_iso(item.get("last_user_at")),
                "last_proactive_at": _parse_iso(item.get("last_proactive_at")),
            }
            for key, item in rows.items()
        }
