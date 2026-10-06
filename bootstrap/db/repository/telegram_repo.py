"""Telegram binding 仓储（C10：telegram-binding-sync）。

数据边界见 openspec/changes/c10-telegram-binding-sync/design.md ADR-1/ADR-2：
active 绑定双重唯一由部分唯一索引（``uq_tib_account_active`` /
``uq_tib_identity_active``）兜底，应用层先查后插只为友好错误语义，竞态一律
以 IntegrityError 回滚为准；绑定码兑换是**单事务**（码行 FOR UPDATE → 校验
未消费/未过期 → 插 active 绑定 → 置 consumed_at → 审计），任一步失败整体回滚
（码不被消耗、不产生半绑定）。审计复用 admin_audit_events，禁止写入明文码。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.models.auth import AdminAuditEventModel
from bootstrap.db.models.canonical import CanonicalConversationModel
from bootstrap.db.models.telegram import (
    TelegramBindingCodeModel,
    TelegramIdentityBindingModel,
)

_UTC_NOW = lambda: datetime.now(UTC)


class TelegramBindingError(Exception):
    """Telegram binding 域错误基类。"""


class TelegramBindingConflictError(TelegramBindingError):
    """双重唯一约束冲突：账号或 Telegram 身份已有 active 绑定。"""


class TelegramCodeRejectedError(TelegramBindingError):
    """绑定码不可用（未知/已消费/已过期统一语义，不泄露区分信息）。"""


class TelegramCodeLimitError(TelegramBindingError):
    """该账号在途（未消费）绑定码数超上限。"""


class TelegramBindingRepository:
    """绑定行 / 绑定码 / 审计的最小读写 seam。"""

    def __init__(self, session_factory: async_sessionmaker):
        self._sf = session_factory

    # ── 绑定行 ──────────────────────────────────────────────────

    async def create_binding(
        self,
        *,
        account_id: uuid.UUID | str,
        tenant_id: str,
        telegram_user_id: str,
        telegram_chat_id: str,
        bound_via: str,
        bound_by: str = "",
        note: str = "",
    ) -> dict:
        """创建 active 绑定；双重唯一冲突抛 :class:`TelegramBindingConflictError`。"""
        acc_id = _require_uuid(account_id)
        user_id = (telegram_user_id or "").strip()
        chat_id = (telegram_chat_id or "").strip()
        if not user_id or not chat_id or not tenant_id.strip():
            raise ValueError("tenant_id/telegram_user_id/telegram_chat_id 均必填")
        async with self._sf() as sess, sess.begin():
            row = TelegramIdentityBindingModel(
                account_id=acc_id,
                tenant_id=tenant_id.strip(),
                telegram_user_id=user_id,
                telegram_chat_id=chat_id,
                status="active",
                bound_via=bound_via,
                bound_by=bound_by[:64],
                note=note[:255],
            )
            sess.add(row)
            try:
                await sess.flush()
            except IntegrityError:
                raise TelegramBindingConflictError(
                    "telegram binding conflict"
                ) from None
            await self._audit(
                sess,
                actor=bound_by[:64] or "system",
                action="telegram_binding.prebind" if bound_via == "admin" else "telegram_binding.redeem",
                target_type="account",
                target_id=str(acc_id),
                detail={"telegram_user_id": user_id, "tenant_id": tenant_id.strip(), "via": bound_via},
            )
            return _binding_to_dict(row)

    async def redeem_code(
        self,
        *,
        code_digest: str,
        telegram_user_id: str,
        telegram_chat_id: str,
    ) -> dict:
        """单事务兑换：校验 → 建绑定 → 消费码 → 审计；失败整体回滚。

        未知 / 已消费 / 已过期统一抛 :class:`TelegramCodeRejectedError`；
        唯一冲突（并发同身份/同账号）抛 :class:`TelegramBindingConflictError`
        且码保持未消费（回滚）。
        """
        digest = (code_digest or "").strip().lower()
        user_id = (telegram_user_id or "").strip()
        chat_id = (telegram_chat_id or "").strip()
        if not digest or not user_id:
            raise TelegramCodeRejectedError("code rejected")
        now = _UTC_NOW()
        async with self._sf() as sess, sess.begin():
            code = (
                await sess.execute(
                    select(TelegramBindingCodeModel)
                    .where(TelegramBindingCodeModel.code_digest == digest)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if code is None or code.consumed_at is not None or code.expires_at <= now:
                raise TelegramCodeRejectedError("code rejected")
            await _reject_existing_bindings(sess, code.account_id, user_id)
            row = TelegramIdentityBindingModel(
                account_id=code.account_id,
                tenant_id=code.tenant_id,
                telegram_user_id=user_id,
                telegram_chat_id=chat_id[:64] or user_id,
                status="active",
                bound_via="code",
                bound_by=(code.issued_by or "")[:64],
                note=code.note[:255],
            )
            sess.add(row)
            try:
                await sess.flush()
            except IntegrityError:
                # 并发竞态兜底：部分唯一索引拒绝，整体回滚（码不被消耗）。
                raise TelegramBindingConflictError(
                    "telegram binding conflict"
                ) from None
            code.consumed_at = now
            await self._audit(
                sess,
                actor=f"telegram:{user_id[:56]}",
                action="telegram_binding.redeem",
                target_type="account",
                target_id=str(code.account_id),
                detail={
                    "telegram_user_id": user_id,
                    "tenant_id": code.tenant_id,
                    "via": "code",
                },
            )
            return _binding_to_dict(row)

    async def get_active_binding_by_identity(
        self, telegram_user_id: str
    ) -> dict | None:
        user_id = (telegram_user_id or "").strip()
        if not user_id:
            return None
        async with self._sf() as sess:
            row = (
                await sess.execute(
                    select(TelegramIdentityBindingModel).where(
                        TelegramIdentityBindingModel.telegram_user_id == user_id,
                        TelegramIdentityBindingModel.status == "active",
                    )
                )
            ).scalar_one_or_none()
            return _binding_to_dict(row) if row else None

    async def get_active_binding_by_account(
        self, account_id: uuid.UUID | str
    ) -> dict | None:
        acc_id = _coerce_uuid(account_id)
        if acc_id is None:
            return None
        async with self._sf() as sess:
            row = (
                await sess.execute(
                    select(TelegramIdentityBindingModel).where(
                        TelegramIdentityBindingModel.account_id == acc_id,
                        TelegramIdentityBindingModel.status == "active",
                    )
                )
            ).scalar_one_or_none()
            return _binding_to_dict(row) if row else None

    async def get_binding(self, binding_id: uuid.UUID | str) -> dict | None:
        bid = _coerce_uuid(binding_id)
        if bid is None:
            return None
        async with self._sf() as sess:
            row = await sess.get(TelegramIdentityBindingModel, bid)
            return _binding_to_dict(row) if row else None

    async def list_bindings(self, *, active_only: bool = False) -> list[dict]:
        stmt = select(TelegramIdentityBindingModel).order_by(
            TelegramIdentityBindingModel.created_at.asc(),
            TelegramIdentityBindingModel.id.asc(),
        )
        if active_only:
            stmt = stmt.where(TelegramIdentityBindingModel.status == "active")
        async with self._sf() as sess:
            rows = (await sess.execute(stmt)).scalars().all()
            return [_binding_to_dict(r) for r in rows]

    async def unbind(
        self, binding_id: uuid.UUID | str, *, unbound_by: str
    ) -> dict | None:
        """active → unbound（幂等：已解绑返回原行不重复写审计）。"""
        bid = _coerce_uuid(binding_id)
        if bid is None:
            return None
        async with self._sf() as sess, sess.begin():
            row = await sess.get(TelegramIdentityBindingModel, bid, with_for_update=True)
            if row is None:
                return None
            if row.status == "active":
                row.status = "unbound"
                row.unbound_at = _UTC_NOW()
                row.unbound_by = (unbound_by or "")[:64]
                await self._audit(
                    sess,
                    actor=(unbound_by or "system")[:64],
                    action="telegram_binding.unbind",
                    target_type="account",
                    target_id=str(row.account_id),
                    detail={
                        "telegram_user_id": row.telegram_user_id,
                        "tenant_id": row.tenant_id,
                    },
                )
            return _binding_to_dict(row)

    # ── 绑定码 ──────────────────────────────────────────────────

    async def insert_code(
        self,
        *,
        account_id: uuid.UUID | str,
        tenant_id: str,
        code_digest: str,
        issued_by: str,
        note: str = "",
        expires_at: datetime,
    ) -> dict:
        acc_id = _require_uuid(account_id)
        digest = (code_digest or "").strip().lower()
        if len(digest) != 64:
            raise ValueError("code_digest 必须是 sha256 hex")
        async with self._sf() as sess, sess.begin():
            row = TelegramBindingCodeModel(
                account_id=acc_id,
                tenant_id=tenant_id.strip(),
                code_digest=digest,
                issued_by=issued_by[:64],
                note=note[:255],
                expires_at=expires_at,
            )
            sess.add(row)
            await self._audit(
                sess,
                actor=issued_by[:64] or "system",
                action="telegram_binding.issue",
                target_type="account",
                target_id=str(acc_id),
                detail={"tenant_id": tenant_id.strip(), "expires_at": expires_at.isoformat()},
            )
            return _code_to_dict(row)

    async def count_open_codes(self, account_id: uuid.UUID | str) -> int:
        """在途（未消费且未过期）码数——过期码不计入签发上限。"""
        acc_id = _coerce_uuid(account_id)
        if acc_id is None:
            return 0
        async with self._sf() as sess:
            value = await sess.scalar(
                select(func.count())
                .select_from(TelegramBindingCodeModel)
                .where(
                    TelegramBindingCodeModel.account_id == acc_id,
                    TelegramBindingCodeModel.consumed_at.is_(None),
                    TelegramBindingCodeModel.expires_at > _UTC_NOW(),
                )
            )
            return int(value or 0)

    async def find_code_by_digest(self, code_digest: str) -> dict | None:
        digest = (code_digest or "").strip().lower()
        if not digest:
            return None
        async with self._sf() as sess:
            row = (
                await sess.execute(
                    select(TelegramBindingCodeModel).where(
                        TelegramBindingCodeModel.code_digest == digest
                    )
                )
            ).scalar_one_or_none()
            return _code_to_dict(row) if row else None

    async def first_conversation_tenant(
        self, account_id: uuid.UUID | str
    ) -> str | None:
        """账号 created_at 首个 canonical 会话的 tenant（绑定目标，resolve 同规则）。"""
        acc_id = _coerce_uuid(account_id)
        if acc_id is None:
            return None
        async with self._sf() as sess:
            row = (
                await sess.execute(
                    select(CanonicalConversationModel)
                    .where(CanonicalConversationModel.account_id == acc_id)
                    .order_by(
                        CanonicalConversationModel.created_at.asc(),
                        CanonicalConversationModel.id.asc(),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            return str(row.tenant_id) if row else None

    # ── 审计 ────────────────────────────────────────────────────

    async def _audit(self, sess: Any, *, actor: str, action: str, target_type: str, target_id: str, detail: dict | None) -> None:
        sess.add(
            AdminAuditEventModel(
                actor=actor[:64],
                action=action[:64],
                target_type=target_type[:32],
                target_id=target_id[:64],
                detail=detail,
            )
        )


async def _reject_existing_bindings(sess: Any, account_id: uuid.UUID, telegram_user_id: str) -> None:
    """友好语义的既有绑定检查；并发竞态由部分唯一索引兜底（redeem 事务内调用）。"""
    identity_row = (
        await sess.execute(
            select(TelegramIdentityBindingModel).where(
                TelegramIdentityBindingModel.telegram_user_id == telegram_user_id,
                TelegramIdentityBindingModel.status == "active",
            )
        )
    ).scalar_one_or_none()
    if identity_row is not None:
        raise TelegramBindingConflictError("telegram identity already bound")
    account_row = (
        await sess.execute(
            select(TelegramIdentityBindingModel).where(
                TelegramIdentityBindingModel.account_id == account_id,
                TelegramIdentityBindingModel.status == "active",
            )
        )
    ).scalar_one_or_none()
    if account_row is not None:
        raise TelegramBindingConflictError("account already bound")


def _binding_to_dict(row: TelegramIdentityBindingModel) -> dict:
    return {
        "id": str(row.id),
        "account_id": str(row.account_id),
        "tenant_id": row.tenant_id,
        "telegram_user_id": row.telegram_user_id,
        "telegram_chat_id": row.telegram_chat_id,
        "status": row.status,
        "bound_via": row.bound_via,
        "bound_by": row.bound_by,
        "note": row.note,
        "bound_at": row.bound_at.isoformat() if row.bound_at else "",
        "unbound_at": row.unbound_at.isoformat() if row.unbound_at else None,
        "unbound_by": row.unbound_by,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


def _code_to_dict(row: TelegramBindingCodeModel) -> dict:
    return {
        "id": str(row.id),
        "account_id": str(row.account_id),
        "tenant_id": row.tenant_id,
        "digest_version": row.digest_version,
        "issued_by": row.issued_by,
        "note": row.note,
        "expires_at": row.expires_at.isoformat() if row.expires_at else "",
        "consumed_at": row.consumed_at.isoformat() if row.consumed_at else None,
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


def _coerce_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _require_uuid(value: uuid.UUID | str | None) -> uuid.UUID:
    coerced = _coerce_uuid(value)
    if coerced is None:
        raise ValueError(f"需要有效 UUID: {value!r}")
    return coerced
