"""Telegram 绑定生命周期服务（C10：telegram-binding-sync）。

组合 `TelegramBindingRepository`（绑定行/绑定码/审计）与 `CanonicalIdentityRepository`
（账号/会话读取），对外提供：

- ``issue_code``：账号侧签发一次性绑定码（≥128bit 随机、sha256 入库、默认 10min
  过期、每账号在途上限、明文只在响应出现一次）；
- ``redeem``：Telegram 侧兑换（原子事务，见 repo；失败统一语义）；
- ``prebind`` / ``unbind``：管理员路径；
- ``resolve_identity``：source identity → account → tenant → canonical conversation
  的可信查表（§5.9.2）：无 active 绑定 / 账号非 active / 会话不存在一律 fail-closed，
  不落默认租户。

绑定目标 agent（tenant）在签发/预绑定时点冻结为账号 ``created_at`` 首个 canonical
会话（与 ``resolve_webchat_identity`` 同规则）。
"""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.repository.canonical_repo import CanonicalIdentityRepository
from bootstrap.db.repository.telegram_repo import (
    TelegramBindingConflictError,
    TelegramBindingRepository,
    TelegramCodeLimitError,
)

__all__ = [
    "DEFAULT_CODE_TTL",
    "MAX_OPEN_CODES_PER_ACCOUNT",
    "TelegramAccountNotReadyError",
    "TelegramBindingService",
    "TelegramIdentity",
    "normalize_code",
]

DEFAULT_CODE_TTL = timedelta(minutes=10)
"""绑定码默认有效期（§10 DECIDED：10 分钟过期）。"""

MAX_OPEN_CODES_PER_ACCOUNT = 5
"""每账号在途（未消费）绑定码上限（防刷；spec 无冻结值，此为 Pilot 初始值）。"""

_CODE_RANDOM_BYTES = 16
"""128 bit 随机（design「码空间 ≥ 128bit」）；hex 展示，兑换时归一化。"""


class TelegramAccountNotReadyError(RuntimeError):
    """账号不存在/非 active 或尚无 canonical 会话，不可签发或绑定。"""


def normalize_code(raw: str) -> str:
    """兑换侧码归一化：去分隔符/空白、小写，只保留 hex 字符。"""
    return "".join(ch for ch in (raw or "").lower() if ch in "0123456789abcdef")


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def _format_code(code: str) -> str:
    return "-".join(code[i : i + 8] for i in range(0, len(code), 8))


@dataclass(frozen=True)
class TelegramIdentity:
    """绑定解析出的服务端身份三元组（+ Telegram 路由信息）。"""

    account_id: str
    tenant_id: str
    conversation_id: str
    telegram_user_id: str
    telegram_chat_id: str

    @property
    def session_key(self) -> str:
        """turn 执行的会话路由键：与 WebChat 完全一致（ADR-3，共享派生视图）。"""
        return f"chat:{self.tenant_id}"


class TelegramBindingService:
    """绑定码签发/兑换、预绑定/解绑、身份解析（C10 服务层唯一入口）。"""

    def __init__(self, session_factory: async_sessionmaker):
        self._sf = session_factory
        self._repo = TelegramBindingRepository(session_factory)
        self._canonical = CanonicalIdentityRepository(session_factory)

    # ── 绑定码 ──────────────────────────────────────────────────

    async def issue_code(
        self,
        *,
        account_id: str,
        issued_by: str,
        note: str = "",
        ttl: timedelta = DEFAULT_CODE_TTL,
    ) -> dict:
        """为本账号签发绑定码；返回 ``{"code": 明文, "expires_at": iso, ...}``。

        账号尚无 canonical 会话（provisioning 未完成）→ 拒绝；在途码超上限 →
        :class:`TelegramCodeLimitError`。明文码只在本返回值出现一次。
        """
        tenant_id = await self._repo.first_conversation_tenant(account_id)
        if not tenant_id:
            raise TelegramAccountNotReadyError("account has no canonical conversation")
        account = await self._canonical.get_account(account_id)
        if account is None or str(account.get("status")) != "active":
            raise TelegramAccountNotReadyError("account is not active")
        open_count = await self._repo.count_open_codes(account_id)
        if open_count >= MAX_OPEN_CODES_PER_ACCOUNT:
            raise TelegramCodeLimitError("too many open binding codes")
        code = secrets.token_hex(_CODE_RANDOM_BYTES)
        expires_at = datetime.now(UTC) + ttl
        record = await self._repo.insert_code(
            account_id=account_id,
            tenant_id=tenant_id,
            code_digest=_digest(code),
            issued_by=issued_by,
            note=note,
            expires_at=expires_at,
        )
        return {**record, "code": _format_code(code), "tenant_id": tenant_id}

    async def redeem(
        self, *, code_text: str, telegram_user_id: str, telegram_chat_id: str
    ) -> dict:
        """兑换绑定码并创建 active 绑定（原子语义见 repo）。"""
        return await self._repo.redeem_code(
            code_digest=_digest(normalize_code(code_text)),
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
        )

    # ── 管理员路径 ──────────────────────────────────────────────

    async def prebind(
        self,
        *,
        account_id: str,
        telegram_user_id: str,
        telegram_chat_id: str,
        admin_actor: str,
        note: str = "",
    ) -> dict:
        """管理员预绑定（目标 agent 规则同签发；冲突 fail-closed）。"""
        tenant_id = await self._repo.first_conversation_tenant(account_id)
        if not tenant_id:
            raise TelegramAccountNotReadyError("account has no canonical conversation")
        return await self._repo.create_binding(
            account_id=account_id,
            tenant_id=tenant_id,
            telegram_user_id=telegram_user_id,
            telegram_chat_id=telegram_chat_id,
            bound_via="admin",
            bound_by=admin_actor,
            note=note,
        )

    async def unbind(self, *, binding_id: str, admin_actor: str) -> dict | None:
        return await self._repo.unbind(binding_id, unbound_by=admin_actor)

    async def list_bindings(self, *, active_only: bool = False) -> list[dict]:
        return await self._repo.list_bindings(active_only=active_only)

    # ── 身份解析（入站门禁） ────────────────────────────────────

    async def resolve_identity(
        self, telegram_user_id: str, telegram_chat_id: str = ""
    ) -> TelegramIdentity | None:
        """source identity → 三元组；无 active 绑定返回 None（调用方拒绝）。"""
        binding = await self._repo.get_active_binding_by_identity(telegram_user_id)
        if binding is None:
            return None
        account = await self._canonical.get_account(binding["account_id"])
        if account is None or str(account.get("status")) != "active":
            return None
        conversation = await self._canonical.get_conversation_by_tenant(
            str(binding["tenant_id"])
        )
        if conversation is None:
            return None
        return TelegramIdentity(
            account_id=str(binding["account_id"]),
            tenant_id=str(binding["tenant_id"]),
            conversation_id=str(conversation["id"]),
            telegram_user_id=str(binding["telegram_user_id"]),
            telegram_chat_id=telegram_chat_id or str(binding["telegram_chat_id"]),
        )
