"""Canonical identity / message repository（C1 + account→N tenant 扩展）。

数据边界见 openspec/changes/2026-09-05-c1-account-multi-tenant/：
`test_accounts`（登录主体，自身不带 tenant）/ `canonical_conversations`（每一行即
一个 agent / tenant 资源域：tenant_id 全局唯一、account_id 归属某账号，账号可
拥有多行）/ `canonical_messages`。sequence 分配冻结语义（PILOT_ROADMAP
§5.9.2）：per-conversation 0-based BIGINT，取号与写消息在**同一个 PostgreSQL
事务**内完成——`UPDATE canonical_conversations SET next_sequence =
next_sequence + 1 ... RETURNING next_sequence - 1` 的行锁串行化同会话并发，
禁止「应用内读号加一再单独提交」。本模块没有任何旧单体存储回退路径
（§10 DECIDED：不 fallback、不双写、不反向同步）。
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from bootstrap.db.models.canonical import (
    CanonicalConversationModel,
    CanonicalMessageModel,
    TestAccountModel,
)

__all__ = [
    "CanonicalConversationNotFoundError",
    "CanonicalIdentityRepository",
    "CanonicalMessageRepository",
    "TenantAlreadyBoundError",
]


class CanonicalConversationNotFoundError(LookupError):
    """目标规范会话不存在或 tenant 不匹配（fail-closed：调用方必须拒绝，不落默认值）。"""


class TenantAlreadyBoundError(LookupError):
    """tenant 已绑定到其它账号：cross-account 占用 fail-closed，拒绝改写既有行。"""


def _new_uuid() -> uuid.UUID:
    return uuid.uuid4()


class CanonicalIdentityRepository:
    """账号 / 规范会话（agent）的最小读写 seam（provisioning 与 resolver 共用）。"""

    def __init__(self, session_factory: async_sessionmaker):
        self._sf = session_factory

    async def create_account(
        self,
        *,
        account_id: uuid.UUID | str | None = None,
        display_name: str = "",
        status: str = "provisioning",
    ) -> dict:
        """幂等创建账号（按账号 id；DO NOTHING 后回查）。

        provisioning retry 用同一外部账号 id 调用时返回现有行，不产生第二个账号。
        """
        acc_id = _coerce_uuid(account_id) or _new_uuid()
        async with self._sf() as sess, sess.begin():
            await sess.execute(
                _insert(TestAccountModel)
                .values(id=acc_id, status=status, display_name=display_name)
                .on_conflict_do_nothing()
            )
            account = await sess.get(TestAccountModel, acc_id)
            if account is None:  # 仅当并发删除等外力场景，正常流程不可达。
                raise CanonicalConversationNotFoundError(
                    f"account 创建失败: account_id={acc_id!r}"
                )
            return _account_to_dict(account)

    async def create_agent(
        self,
        account_id: uuid.UUID | str,
        tenant_id: str,
        *,
        conversation_id: uuid.UUID | str | None = None,
        status: str = "active",
    ) -> dict:
        """为账号建一个 agent（= 一条 canonical conversation，tenant 全局唯一）。

        按 tenant 幂等：同账号 retry 返回现有会话（不产生第二个 agent）；
        tenant 已属**另一个**账号时抛 :class:`TenantAlreadyBoundError`（跨账号
        占用 fail-closed）。账号不存在时 FK 拒绝（IntegrityError 上抛）。
        返回该 agent 会话 dict。
        """
        acc_id = _coerce_uuid(account_id)
        if acc_id is None:
            raise ValueError("create_agent 需要有效 account_id")
        conv_id = _coerce_uuid(conversation_id) or _new_uuid()
        async with self._sf() as sess, sess.begin():
            await sess.execute(
                _insert(CanonicalConversationModel)
                .values(
                    id=conv_id,
                    tenant_id=tenant_id,
                    account_id=acc_id,
                    status=status,
                )
                .on_conflict_do_nothing()
            )
            conversation = (
                await sess.execute(
                    select(CanonicalConversationModel).where(
                        CanonicalConversationModel.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if conversation is None:  # 正常流程不可达：插入或既有两者必有其一。
                raise CanonicalConversationNotFoundError(
                    f"canonical conversation 不存在: tenant_id={tenant_id!r}"
                )
            if conversation.account_id != acc_id:
                raise TenantAlreadyBoundError(
                    f"tenant {tenant_id!r} 已绑定到其它账号"
                )
            return _conversation_to_dict(conversation)

    async def provision_account_with_agent(
        self,
        tenant_id: str,
        *,
        account_id: uuid.UUID | str | None = None,
        display_name: str = "",
        status: str = "provisioning",
        conversation_id: uuid.UUID | str | None = None,
    ) -> dict:
        """单事务 = 创建账号 + 其首个 agent（会话），provisioning retry 安全。

        tenant 已存在时不改写（DO NOTHING）；同 tenant 再次 provision 且属同一
        账号时返回现有状态，属其它账号时抛 :class:`TenantAlreadyBoundError` 并整体
        回滚（不产生孤儿账号）。返回 ``{"account": ..., "conversation": ...}``。
        """
        acc_id = _coerce_uuid(account_id) or _new_uuid()
        conv_id = _coerce_uuid(conversation_id) or _new_uuid()
        async with self._sf() as sess, sess.begin():
            await sess.execute(
                _insert(TestAccountModel)
                .values(id=acc_id, status=status, display_name=display_name)
                .on_conflict_do_nothing()
            )
            account = await sess.get(TestAccountModel, acc_id)
            if account is None:  # 仅当并发删除等外力场景，正常流程不可达。
                raise CanonicalConversationNotFoundError(
                    f"account 创建失败: account_id={acc_id!r}"
                )
            await sess.execute(
                _insert(CanonicalConversationModel)
                .values(
                    id=conv_id,
                    tenant_id=tenant_id,
                    account_id=acc_id,
                    status="active",
                )
                .on_conflict_do_nothing()
            )
            conversation = (
                await sess.execute(
                    select(CanonicalConversationModel).where(
                        CanonicalConversationModel.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            if conversation is None:  # 正常流程不可达：插入或既有两者必有其一。
                raise CanonicalConversationNotFoundError(
                    f"canonical conversation 不存在: tenant_id={tenant_id!r}"
                )
            if conversation.account_id != acc_id:
                raise TenantAlreadyBoundError(
                    f"tenant {tenant_id!r} 已绑定到其它账号"
                )
            return {
                "account": _account_to_dict(account),
                "conversation": _conversation_to_dict(conversation),
            }

    async def get_account(self, account_id: uuid.UUID | str) -> dict | None:
        async with self._sf() as sess:
            row = await sess.get(TestAccountModel, _coerce_uuid(account_id))
            return _account_to_dict(row) if row else None

    async def get_conversation_by_tenant(self, tenant_id: str) -> dict | None:
        async with self._sf() as sess:
            row = (
                await sess.execute(
                    select(CanonicalConversationModel).where(
                        CanonicalConversationModel.tenant_id == tenant_id
                    )
                )
            ).scalar_one_or_none()
            return _conversation_to_dict(row) if row else None

    async def get_conversation(self, conversation_id: uuid.UUID | str) -> dict | None:
        async with self._sf() as sess:
            row = await sess.get(
                CanonicalConversationModel, _coerce_uuid(conversation_id)
            )
            return _conversation_to_dict(row) if row else None

    async def list_conversations_by_account(
        self, account_id: uuid.UUID | str
    ) -> list[dict]:
        """账号拥有的 agent 会话（tenant 资源域）列表，created_at 升序（确定性枚举）。

        账号可能拥有 0..N 个 agent；空列表是合法状态（账号尚无 agent），不是错误。
        """
        acc_id = _coerce_uuid(account_id)
        if acc_id is None:
            return []
        async with self._sf() as sess:
            rows = (
                await sess.execute(
                    select(CanonicalConversationModel)
                    .where(CanonicalConversationModel.account_id == acc_id)
                    .order_by(
                        CanonicalConversationModel.created_at.asc(),
                        CanonicalConversationModel.id.asc(),
                    )
                )
            ).scalars().all()
            return [_conversation_to_dict(r) for r in rows]


class CanonicalMessageRepository:
    """canonical message stream 写入与按序读回（重放/断线补拉 seam）。"""

    def __init__(self, session_factory: async_sessionmaker):
        self._sf = session_factory

    async def append_message(
        self,
        tenant_id: str,
        conversation_id: uuid.UUID | str,
        *,
        role: str,
        content: str | None = None,
        source_channel: str | None = None,
        source_identity_id: str | None = None,
        source_message_id: str | None = None,
        client_message_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        message_id: uuid.UUID | str | None = None,
    ) -> dict:
        """在单个事务内原子分配 sequence 并写入 canonical message。

        会话不存在 / tenant 不匹配时抛 :class:`CanonicalConversationNotFoundError`
        并整体回滚（零写入、计数器无空洞）；唯一约束/CHECK 冲突的 IntegrityError
        原样上抛，同样由事务回滚保证不留空洞。
        """
        conv_id = _coerce_uuid(conversation_id)
        if not conv_id:
            raise CanonicalConversationNotFoundError(
                f"canonical conversation 不存在: conversation_id={conversation_id!r} tenant_id={tenant_id!r}"
            )
        msg_id = _coerce_uuid(message_id) or _new_uuid()
        async with self._sf() as sess, sess.begin():
            allocated = (
                await sess.execute(
                    update(CanonicalConversationModel)
                    .where(
                        CanonicalConversationModel.id == conv_id,
                        CanonicalConversationModel.tenant_id == tenant_id,
                    )
                    .values(next_sequence=CanonicalConversationModel.next_sequence + 1)
                    .returning(CanonicalConversationModel.next_sequence - 1)
                )
            ).scalar_one_or_none()
            if allocated is None:
                # 覆盖「会话不存在」与「tenant 不符」两种情况，不区分泄露归属信息。
                raise CanonicalConversationNotFoundError(
                    f"canonical conversation 不存在: conversation_id={conversation_id!r} tenant_id={tenant_id!r}"
                )
            row = CanonicalMessageModel(
                id=msg_id,
                tenant_id=tenant_id,
                conversation_id=conv_id,
                sequence=allocated,
                role=role,
                content=content,
                source_channel=source_channel,
                source_identity_id=source_identity_id,
                source_message_id=source_message_id,
                client_message_id=client_message_id,
                metadata_json=_to_json(metadata),
            )
            sess.add(row)
            await sess.flush()
            return _message_to_dict(row)

    async def fetch_messages(
        self,
        tenant_id: str,
        conversation_id: uuid.UUID | str,
        *,
        after_sequence: int | None = None,
        limit: int | None = None,
    ) -> list[dict]:
        """按 sequence 升序读回；`after_sequence` 即断线补拉游标（最后确认序号）。

        跨 tenant 查询其它 tenant 的会话返回空列表（租户隔离，不泄露存在性）。
        """
        conv_id = _coerce_uuid(conversation_id)
        if conv_id is None:
            return []
        stmt = (
            select(CanonicalMessageModel)
            .where(
                CanonicalMessageModel.tenant_id == tenant_id,
                CanonicalMessageModel.conversation_id == conv_id,
            )
            .order_by(CanonicalMessageModel.sequence.asc())
        )
        if after_sequence is not None:
            stmt = stmt.where(CanonicalMessageModel.sequence > after_sequence)
        if limit is not None:
            stmt = stmt.limit(limit)
        async with self._sf() as sess:
            rows = (await sess.execute(stmt)).scalars().all()
            return [_message_to_dict(r) for r in rows]

    async def latest_sequence(
        self, tenant_id: str, conversation_id: uuid.UUID | str
    ) -> int:
        """会话当前已持久化的最大序号；空会话返回 -1（下一序号即 0）。

        会话不存在 / tenant 不符时 fail-closed 抛错——该值用于 hello/游标，
        宁可失败也不给猜测的默认值。
        """
        conv_id = _coerce_uuid(conversation_id)
        if conv_id is None:
            raise CanonicalConversationNotFoundError(
                f"canonical conversation 不存在: conversation_id={conversation_id!r}"
            )
        async with self._sf() as sess:
            conversation = await sess.get(CanonicalConversationModel, conv_id)
            if conversation is None or conversation.tenant_id != tenant_id:
                raise CanonicalConversationNotFoundError(
                    f"canonical conversation 不存在: conversation_id={conversation_id!r} tenant_id={tenant_id!r}"
                )
            return conversation.next_sequence - 1


# ── helpers ─────────────────────────────────────────────────


def _insert(model):  # noqa: ANN001, ANN202
    """PG 方言 INSERT（配合 `.on_conflict_do_nothing()` 实现幂等 seed / retry）。"""
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    return pg_insert(model)


def _coerce_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


def _account_to_dict(row: TestAccountModel) -> dict:
    return {
        "id": str(row.id),
        "status": row.status,
        "display_name": row.display_name,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
    }


def _conversation_to_dict(row: CanonicalConversationModel) -> dict:
    return {
        "id": str(row.id),
        "tenant_id": row.tenant_id,
        "account_id": str(row.account_id),
        "status": row.status,
        "next_sequence": row.next_sequence,
        "created_at": row.created_at.isoformat() if row.created_at else "",
        "updated_at": row.updated_at.isoformat() if row.updated_at else "",
    }


def _message_to_dict(row: CanonicalMessageModel) -> dict:
    return {
        "id": str(row.id),
        "tenant_id": row.tenant_id,
        "conversation_id": str(row.conversation_id),
        "sequence": row.sequence,
        "role": row.role,
        "content": row.content,
        "source_channel": row.source_channel,
        "source_identity_id": row.source_identity_id,
        "source_message_id": row.source_message_id,
        "client_message_id": row.client_message_id,
        "metadata": _from_json(row.metadata_json) or {},
        "created_at": row.created_at.isoformat() if row.created_at else "",
    }


def _to_json(data: Any) -> str | None:
    return json.dumps(data, ensure_ascii=False) if data is not None else None


def _from_json(s: str | None) -> Any:
    if not s:
        return None
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return None
