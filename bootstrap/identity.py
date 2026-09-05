"""Canonical identity resolver（C1 + account→N tenant 扩展 seam）。

冻结语义（PILOT_ROADMAP §5.9.2 + openspec/changes/2026-09-05-c1-account-multi-tenant/）：
服务端身份映射是**可信查表过程**——上游入口（C5 auth session、C10 Telegram
binding 等）先验证 principal，再用本模块按已验证标识查表，产出服务端
`WorkEnvelope` 使用的归属三元组。客户端提交的 tenant/chat/session 字段永远只是
待校验输入，不能成为本 resolver 的授权来源。

account→N 模型：账号是登录主体，可拥有多个 tenant（每个 tenant = 一个 agent =
恰好一条 canonical conversation，各自独立的记忆/persona 域）。因此身份解析的
单元是 **tenant**——`resolve_by_tenant` 恒产出单一三元组；账号级 `list_agents`
产出该账号拥有的 agent（tenant）列表（0..N），选定 agent 后仍按 tenant 解析
（C5/C10 绑定负责「账号 + 选中 agent」→ tenant）。

fail-closed：未知账号/未知 tenant/空标识一律抛 :class:`IdentityResolutionError`；
已知账号但尚无 agent 时 `list_agents` 返回空列表（合法状态），绝不返回默认租户
或任何猜测值——本模块刻意不引用任何默认租户常量（由静态契约测试强制，见
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
    """服务端派生的归属三元组（账号 ↔ tenant ↔ canonical conversation）。

    账号可拥有多个 tenant；tenant 与 canonical conversation 1:1，所以三元组中
    tenant_id 唯一锚定一个 agent（资源域），conversation_id 是该 agent 的规范会话。
    """

    account_id: str
    tenant_id: str
    conversation_id: str


class IdentityResolutionError(Exception):
    """无 binding / principal 未登记时的 fail-closed 错误（统一措辞，不泄露存在性）。"""


def _is_blank(value: str) -> bool:
    return not str(value).strip()


class CanonicalIdentityResolver:
    """按已验证 principal 查表的唯一合法派生入口（ADR-1，account→N 扩展后）。

    解析单元是 tenant（每 agent 一个）；账号级只做归属枚举。C5/C10 就绪后由其
    adapter 验证完 principal 并选定 agent 再落到 tenant 调用，本契约不变。
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

    async def resolve_by_tenant(self, tenant_id: str) -> CanonicalIdentity:
        """已验证 tenant_id → 单一归属三元组（per-Work / 工具授权的可信入口）。"""
        if _is_blank(tenant_id):
            raise IdentityResolutionError(_RESOLVE_FAILED)
        conversation = await self._repo.get_conversation_by_tenant(tenant_id)
        if conversation is None:
            raise IdentityResolutionError(_RESOLVE_FAILED)
        account = await self._repo.get_account(conversation["account_id"])
        if account is None:  # FK 下不可达，防御外部破坏仍 fail-closed。
            raise IdentityResolutionError(_RESOLVE_FAILED)
        return CanonicalIdentity(
            account_id=account["id"],
            tenant_id=conversation["tenant_id"],
            conversation_id=conversation["id"],
        )

    async def list_agents(self, account_id: str) -> list[CanonicalIdentity]:
        """账号拥有的 agent（tenant）列表，created_at 升序（确定性枚举）。

        未知 / 空账号 fail-closed 抛错；已知账号尚无 agent 返回空列表（合法）。
        """
        if _is_blank(account_id):
            raise IdentityResolutionError(_RESOLVE_FAILED)
        account = await self._repo.get_account(account_id)
        if account is None:
            raise IdentityResolutionError(_RESOLVE_FAILED)
        conversations = await self._repo.list_conversations_by_account(
            account["id"]
        )
        return [
            CanonicalIdentity(
                account_id=account["id"],
                tenant_id=c["tenant_id"],
                conversation_id=c["id"],
            )
            for c in conversations
        ]
