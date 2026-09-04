"""Canonical identity resolver（C1 seam）。

冻结语义（PILOT_ROADMAP §5.9.2）：服务端身份映射是**可信查表过程**——上游
入口（C5 auth session、C10 Telegram binding 等）先验证 principal，再用本模块
按已验证标识查 `test_accounts → tenant_id → canonical_conversations`，产出
服务端 `WorkEnvelope` 使用的归属三元组。客户端提交的 tenant/chat/session 字段
永远只是待校验输入，不能成为本 resolver 的授权来源。

fail-closed：任何查表落空（未知账号/未知 tenant/空标识/账号无会话）一律抛
:class:`IdentityResolutionError`，绝不返回默认租户或任何猜测值——本模块刻意
不引用任何默认租户常量（由静态契约测试强制，见
tests/test_canonical_identity_contract.py）。
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.repository.canonical_repo import CanonicalIdentityRepository

__all__ = [
    "CanonicalIdentity",
    "CanonicalIdentityResolver",
    "IdentityResolutionError",
]

_RESOLVE_FAILED = "identity resolution failed: no binding for principal"


@dataclass(frozen=True)
class CanonicalIdentity:
    """服务端派生的归属三元组（account ↔ tenant ↔ canonical conversation 1:1）。"""

    account_id: str
    tenant_id: str
    conversation_id: str


class IdentityResolutionError(Exception):
    """无 binding / principal 未登记时的 fail-closed 错误（统一措辞，不泄露存在性）。"""


class CanonicalIdentityResolver:
    """按已验证 principal 查表的唯一合法派生入口（ADR-1）。

    C1 阶段支持 account_id / tenant_id 两种已验证标识；C5/C10 就绪后由其
    adapter 验证完 principal 再调用，本契约不变。
    """

    def __init__(
        self,
        session_factory: async_sessionmaker | None = None,
        *,
        repo: CanonicalIdentityRepository | None = None,
    ):
        if repo is None:
            if session_factory is None:
                raise ValueError(
                    "CanonicalIdentityResolver 需要 session_factory 或 repo 之一"
                )
            repo = CanonicalIdentityRepository(session_factory)
        self._repo = repo

    async def resolve_by_account(self, account_id: str) -> CanonicalIdentity:
        """已验证 account_id → 三元组。落空即 fail-closed。"""
        account = await self._require_principal(
            account_id, lambda: self._repo.get_account(account_id)
        )
        conversation = await self._require_conversation(account["tenant_id"])
        return CanonicalIdentity(
            account_id=account["id"],
            tenant_id=account["tenant_id"],
            conversation_id=conversation["id"],
        )

    async def resolve_by_tenant(self, tenant_id: str) -> CanonicalIdentity:
        """已验证 tenant_id → 三元组（服务端内部使用；非客户端输入）。"""
        account = await self._require_principal(
            tenant_id, lambda: self._repo.get_account_by_tenant(tenant_id)
        )
        conversation = await self._require_conversation(account["tenant_id"])
        return CanonicalIdentity(
            account_id=account["id"],
            tenant_id=account["tenant_id"],
            conversation_id=conversation["id"],
        )

    async def _require_principal(self, principal: str, lookup):
        if not str(principal).strip():
            raise IdentityResolutionError(_RESOLVE_FAILED)
        account = await lookup()
        if account is None:
            raise IdentityResolutionError(_RESOLVE_FAILED)
        return account

    async def _require_conversation(self, tenant_id: str) -> dict:
        conversation = await self._repo.get_conversation_by_tenant(tenant_id)
        if conversation is None:
            # 1:1 约束下不应发生；发生即数据被外部破坏，仍 fail-closed。
            raise IdentityResolutionError(_RESOLVE_FAILED)
        return conversation
