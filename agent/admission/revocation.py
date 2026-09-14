"""RevocationGate: 副作用前 revocation recheck（PILOT_ROADMAP §5.9.16，ADR-3）。

语义（fail-closed）：
- ``TenantStatus`` 由 ``TenantStatusProvider`` 提供（真实账号状态源归 C5 接线）；
- ``REVOKED`` / ``SUSPENDED`` → 拒绝该副作用；
- ``ACTIVE`` → 放行；
- ``UNKNOWN`` 或 provider 抛异常 → 拒绝（任一无法判定时必须 fail-closed）；
- ``provider=None``（Pilot 未接账号库）→ 显式 dev-open：放行并记录结构化日志，
  不是静默降级；gate 存在且日志可观测，C5 接线后删除该分支即全域 fail-closed。

检查点全部在当前 recheck（读 provider 当前状态），不读 snapshot 捕获状态——
旧 snapshot lease 不能绕过 revocation。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import TypeAlias

logger = logging.getLogger(__name__)


class TenantStatus(StrEnum):
    ACTIVE = "ACTIVE"
    SUSPENDED = "SUSPENDED"
    REVOKED = "REVOKED"
    UNKNOWN = "UNKNOWN"


TenantStatusProvider: TypeAlias = Callable[[str], Awaitable[TenantStatus]]


class RevocationRejected(RuntimeError):
    """tenant 已吊销/挂起/状态不可判定，拒绝副作用（fail-closed）。

    携带 tenant_id / action / status 供调用方观测与测试断言。
    """

    def __init__(
        self,
        *,
        tenant_id: str,
        action: str,
        status: TenantStatus,
    ) -> None:
        self.tenant_id = tenant_id
        self.action = action
        self.status = status
        super().__init__(
            f"revocation rejected tenant={tenant_id} action={action} status={status}"
        )


class RevocationGate:
    """副作用前的当前账号状态 recheck 接缝。

    ``source`` 用于日志区分接线点（pilot/loop/proactive/scheduler/plugin_job），
    便于观测哪个入口命中了 dev-open 或拒绝路径。
    """

    def __init__(
        self,
        provider: TenantStatusProvider | None,
        *,
        source: str = "default",
    ) -> None:
        self._provider = provider
        self._source = source
        self._dev_open = provider is None

    @property
    def dev_open(self) -> bool:
        return self._dev_open

    async def check(self, tenant_id: str, *, action: str) -> None:
        """通过返回；否则抛出 :class:`RevocationRejected`。"""
        if self._dev_open:
            logger.info(
                "revocation_gate=dev_open tenant=%s action=%s source=%s",
                tenant_id,
                action,
                self._source,
            )
            return
        try:
            status = await self._provider(tenant_id)
        except Exception as exc:
            logger.error(
                "revocation_gate=fail_closed provider_error tenant=%s action=%s error=%s",
                tenant_id,
                action,
                exc,
            )
            raise RevocationRejected(
                tenant_id=tenant_id,
                action=action,
                status=TenantStatus.UNKNOWN,
            ) from exc
        if status in (TenantStatus.REVOKED, TenantStatus.SUSPENDED):
            logger.warning(
                "revocation_gate=denied tenant=%s action=%s status=%s",
                tenant_id,
                action,
                status,
            )
            raise RevocationRejected(
                tenant_id=tenant_id,
                action=action,
                status=status,
            )
        if status is TenantStatus.UNKNOWN:
            logger.error(
                "revocation_gate=fail_closed unknown_status tenant=%s action=%s",
                tenant_id,
                action,
            )
            raise RevocationRejected(
                tenant_id=tenant_id,
                action=action,
                status=TenantStatus.UNKNOWN,
            )
        # ACTIVE → 放行
        logger.debug(
            "revocation_gate=allowed tenant=%s action=%s status=%s",
            tenant_id,
            action,
            status,
        )