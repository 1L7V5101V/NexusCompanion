"""Attachment metadata 仓储（C6，openspec/changes/c6-attachment/design.md ADR-4/ADR-5）。

全部查询与推进以 ``(account_id, tenant_id)`` 双维度过滤（owner + tenant 隔离，
§5.9.15）：跨租户/跨账号访问返回空或 404（不泄露存在性）。引用语义：
``add_reference`` 在 message_attachments 追加 (message_id, attachment_id) 并更新
referencing_count / last_referenced_at / retention_deadline；``remove_reference``
移除后若 count=0 按同理重算 deadline。``create_attachment`` 以 staged 状态写入，
``commit_attachment`` 在 blob rename 后推进 committed（ADR-3 两段式）。
``mark_missing`` 供 reconciliation 把 committed→missing（blob 丢失）。
清理仅按 retention_deadline 且 referencing_count=0 硬条件推进（ADR-5 幂等）。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from bootstrap.db.models.attachment import (
    AttachmentModel,
    MessageAttachmentModel,
)
from bootstrap.db.models.canonical import CanonicalMessageModel


class AttachmentNotFoundError(LookupError):
    """按 (account, tenant, attachment_id) 找不到或越权（404 语义）。"""


class AttachmentNotOwnedError(PermissionError):
    """attachment 存在但不属于该 account/tenant（403 语义，不泄露存在性）。"""


class AttachmentAlreadyCommittedError(RuntimeError):
    """staged→committed 推进时状态已非 staged（CAS 前置失败）。"""


@dataclass(frozen=True)
class AttachmentRecord:
    id: uuid.UUID
    account_id: uuid.UUID
    tenant_id: str
    size_bytes: int
    detected_mime: str
    server_ext: str
    filename_display: str
    checksum_sha256: str
    storage_key: str
    status: str
    referencing_count: int
    last_referenced_at: datetime | None
    retention_deadline: datetime
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_row(cls, row: Any) -> "AttachmentRecord":
        return cls(
            id=uuid.UUID(str(row.id)),
            account_id=uuid.UUID(str(row.account_id)),
            tenant_id=str(row.tenant_id),
            size_bytes=int(row.size_bytes),
            detected_mime=str(row.detected_mime),
            server_ext=str(row.server_ext),
            filename_display=str(row.filename_display),
            checksum_sha256=str(row.checksum_sha256),
            storage_key=str(row.storage_key),
            status=str(row.status),
            referencing_count=int(row.referencing_count),
            last_referenced_at=row.last_referenced_at,
            retention_deadline=row.retention_deadline,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class AttachmentRepository:
    def __init__(self, session_factory: async_sessionmaker):
        self._sf = session_factory
        self._now = lambda: datetime.now(UTC)

    async def create_attachment(
        self,
        *,
        account_id: uuid.UUID | str,
        tenant_id: str,
        size_bytes: int,
        detected_mime: str,
        server_ext: str,
        filename_display: str,
        checksum_sha256: str,
        storage_key: str,
        temp_ttl_hours: int,
        attachment_id: uuid.UUID | str | None = None,
    ) -> AttachmentRecord:
        """staged 行写入（blob 在 staging 区；blob rename 前不视为已提交）。"""
        att_id = (
            uuid.UUID(str(attachment_id)) if attachment_id is not None else uuid.uuid4()
        )
        deadline = self._now() + timedelta(hours=temp_ttl_hours)
        async with self._sf() as sess, sess.begin():
            row = AttachmentModel(
                id=att_id,
                account_id=uuid.UUID(str(account_id)),
                tenant_id=tenant_id,
                size_bytes=size_bytes,
                detected_mime=detected_mime,
                server_ext=server_ext,
                filename_display=filename_display,
                checksum_sha256=checksum_sha256,
                storage_key=storage_key,
                status="staged",
                retention_deadline=deadline,
            )
            sess.add(row)
            await sess.flush()
            return AttachmentRecord.from_row(row)

    async def commit_attachment(
        self,
        *,
        account_id: uuid.UUID | str,
        tenant_id: str,
        attachment_id: uuid.UUID | str,
        referenced_ttl_days: int,
        message_id: uuid.UUID | str | None = None,
    ) -> AttachmentRecord | None:
        """staged→committed CAS 推进（blob 已 rename）。

        首次引用随 commit 一并登记（调用方已把 blob rename 到最终路径）；
        无 message_id 时仅推进状态（引用后续 add_reference 补登）。
        """
        att_id = uuid.UUID(str(attachment_id))
        now = self._now()
        async with self._sf() as sess, sess.begin():
            record = await self._owned_for_update(sess, account_id, tenant_id, att_id)
            if record is None:
                return None
            if record.status != "staged":
                raise AttachmentAlreadyCommittedError(
                    f"attachment 状态非 staged 无法 commit: {record.status}"
                )
            deadline = now + timedelta(days=referenced_ttl_days)
            if message_id is not None:
                msg_id = uuid.UUID(str(message_id))
                sess.add(
                    MessageAttachmentModel(
                        message_id=msg_id, attachment_id=att_id
                    )
                )
                ref_count = record.referencing_count + 1
                last_ref = now
            else:
                ref_count = record.referencing_count
                last_ref = record.last_referenced_at
            await sess.execute(
                update(AttachmentModel)
                .where(AttachmentModel.id == att_id)
                .values(
                    status="committed",
                    referencing_count=ref_count,
                    last_referenced_at=last_ref,
                    retention_deadline=deadline,
                    updated_at=now,
                )
            )
            fresh = await self._by_id(sess, att_id)
            return AttachmentRecord.from_row(fresh) if fresh is not None else None

    async def get_owned(
        self,
        *,
        account_id: uuid.UUID | str,
        tenant_id: str,
        attachment_id: uuid.UUID | str,
    ) -> AttachmentRecord | None:
        """按 (account, tenant, id) 查询；跨租户/跨账号返回 None（404 语义）。"""
        att_id = uuid.UUID(str(attachment_id))
        async with self._sf() as sess:
            row = await sess.execute(
                select(AttachmentModel)
                .where(
                    AttachmentModel.id == att_id,
                    AttachmentModel.account_id == uuid.UUID(str(account_id)),
                    AttachmentModel.tenant_id == tenant_id,
                )
            )
            row = row.scalar_one_or_none()
            return AttachmentRecord.from_row(row) if row is not None else None

    async def add_reference(
        self,
        *,
        account_id: uuid.UUID | str,
        tenant_id: str,
        attachment_id: uuid.UUID | str,
        message_id: uuid.UUID | str,
        referenced_ttl_days: int,
    ) -> AttachmentRecord | None:
        """引用追加（转发）：message_attachments insert + refcount/deadline 更新。"""
        att_id = uuid.UUID(str(attachment_id))
        msg_id = uuid.UUID(str(message_id))
        now = self._now()
        async with self._sf() as sess, sess.begin():
            record = await self._owned_for_update(sess, account_id, tenant_id, att_id)
            if record is None:
                return None
            sess.add(MessageAttachmentModel(message_id=msg_id, attachment_id=att_id))
            await sess.execute(
                update(AttachmentModel)
                .where(AttachmentModel.id == att_id)
                .values(
                    referencing_count=record.referencing_count + 1,
                    last_referenced_at=now,
                    retention_deadline=now + timedelta(days=referenced_ttl_days),
                    updated_at=now,
                )
            )
            fresh = await self._by_id(sess, att_id)
            return AttachmentRecord.from_row(fresh) if fresh is not None else None

    async def remove_reference(
        self,
        *,
        tenant_id: str,
        attachment_id: uuid.UUID | str,
        message_id: uuid.UUID | str,
        referenced_ttl_days: int,
    ) -> AttachmentRecord | None:
        """解除引用：删除关联行并重算 refcount/deadline（refcount=0 时按解除时刻 +30d）。"""
        att_id = uuid.UUID(str(attachment_id))
        msg_id = uuid.UUID(str(message_id))
        now = self._now()
        async with self._sf() as sess, sess.begin():
            record = await self._tenant_for_update(sess, tenant_id, att_id)
            if record is None:
                return None
            await sess.execute(
                delete(MessageAttachmentModel).where(
                    MessageAttachmentModel.message_id == msg_id,
                    MessageAttachmentModel.attachment_id == att_id,
                )
            )
            remaining = max(record.referencing_count - 1, 0)
            await sess.execute(
                update(AttachmentModel)
                .where(AttachmentModel.id == att_id)
                .values(
                    referencing_count=remaining,
                    last_referenced_at=now if remaining > 0 else record.last_referenced_at,
                    retention_deadline=(
                        now + timedelta(days=referenced_ttl_days)
                        if remaining == 0
                        else record.retention_deadline
                    ),
                    updated_at=now,
                )
            )
            fresh = await self._by_id(sess, att_id)
            return AttachmentRecord.from_row(fresh) if fresh is not None else None

    async def mark_missing(
        self,
        *,
        tenant_id: str,
        attachment_id: uuid.UUID | str,
    ) -> AttachmentRecord | None:
        """reconciliation：committed 但 blob 缺失 → status=missing（读取返回 404）。"""
        att_id = uuid.UUID(str(attachment_id))
        async with self._sf() as sess, sess.begin():
            record = await self._tenant_for_update(sess, tenant_id, att_id)
            if record is None:
                return None
            if record.status != "committed":
                return record
            await sess.execute(
                update(AttachmentModel)
                .where(AttachmentModel.id == att_id)
                .values(status="missing", updated_at=self._now())
            )
            fresh = await self._by_id(sess, att_id)
            return AttachmentRecord.from_row(fresh) if fresh is not None else record

    async def list_expired(
        self, *, tenant_filter: str | None = None, limit: int = 500
    ) -> list[AttachmentRecord]:
        """到期扫描（ADR-5 清理任务用）：staged 超 24h，或 committed refcount=0 且
        retention_deadline 过期。返回记录供调用方删 blob + 删 metadata（幂等）。"""
        now = self._now()
        async with self._sf() as sess:
            stmt = select(AttachmentModel).where(
                AttachmentModel.status.in_(("staged", "committed", "missing"))
            )
            if tenant_filter is not None:
                stmt = stmt.where(AttachmentModel.tenant_id == tenant_filter)
            rows = (await sess.execute(stmt)).scalars().all()
            out: list[AttachmentRecord] = []
            for row in rows:
                record = AttachmentRecord.from_row(row)
                if record.status == "staged":
                    if record.retention_deadline <= now:
                        out.append(record)
                elif record.referencing_count == 0 and record.retention_deadline <= now:
                    out.append(record)
                if len(out) >= limit:
                    break
            return out

    async def delete_attachment(
        self,
        *,
        tenant_id: str,
        attachment_id: uuid.UUID | str,
    ) -> bool:
        """删除 metadata（清理任务幂等；调用方须先删除 blob）。"""
        att_id = uuid.UUID(str(attachment_id))
        async with self._sf() as sess, sess.begin():
            result = await sess.execute(
                delete(AttachmentModel).where(
                    AttachmentModel.id == att_id,
                    AttachmentModel.tenant_id == tenant_id,
                )
            )
            return bool(result.rowcount)

    async def list_committed_by_tenant(self, tenant_id: str) -> list[AttachmentRecord]:
        """reconciliation：committed 记录枚举（校验 blob 存在性用）。"""
        async with self._sf() as sess:
            rows = (
                await sess.execute(
                    select(AttachmentModel)
                    .where(
                        AttachmentModel.tenant_id == tenant_id,
                        AttachmentModel.status.in_(("committed", "missing")),
                    )
                    .order_by(AttachmentModel.created_at)
                )
            ).scalars().all()
            return [AttachmentRecord.from_row(row) for row in rows]

    async def _owned_for_update(
        self,
        sess: AsyncSession,
        account_id: uuid.UUID | str,
        tenant_id: str,
        att_id: uuid.UUID,
    ) -> AttachmentRecord | None:
        row = await sess.execute(
            select(AttachmentModel)
            .where(
                AttachmentModel.id == att_id,
                AttachmentModel.account_id == uuid.UUID(str(account_id)),
                AttachmentModel.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        row = row.scalar_one_or_none()
        return AttachmentRecord.from_row(row) if row is not None else None

    async def _tenant_for_update(
        self,
        sess: AsyncSession,
        tenant_id: str,
        att_id: uuid.UUID,
    ) -> AttachmentRecord | None:
        row = await sess.execute(
            select(AttachmentModel)
            .where(
                AttachmentModel.id == att_id,
                AttachmentModel.tenant_id == tenant_id,
            )
            .with_for_update()
        )
        row = row.scalar_one_or_none()
        return AttachmentRecord.from_row(row) if row is not None else None

    async def _by_id(
        self, sess: AsyncSession, att_id: uuid.UUID
    ) -> Any | None:
        row = await sess.execute(
            select(AttachmentModel).where(AttachmentModel.id == att_id)
        )
        return row.scalar_one_or_none()


__all__ = [
    "AttachmentAlreadyCommittedError",
    "AttachmentNotFoundError",
    "AttachmentNotOwnedError",
    "AttachmentRecord",
    "AttachmentRepository",
]