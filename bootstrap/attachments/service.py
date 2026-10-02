"""C6 上传/读取服务编排（HTTP 端点的业务面，design ADR-1/ADR-3/ADR-7）。

职责边界：
- HTTP 层只解析请求结构、映射错误码；本模块做校验、staging、rename、metadata
  提交的编排，并保证「blob rename 成功 + metadata 同会话提交」；
- 身份已由调用方派生为 (account_id, tenant_id)（与 WS 同一派生点）；
- 上传成功只返回 immutable ``attachment_id`` + 按 id 构造的 url，绝不外泄
  storage_key / 本地路径；
- 读取按 (account, tenant, id) 复校验归属（404 语义），blob 缺失 → missing 标记。

失败抛 ``AttachmentError``/``AttachmentStorageError``/``AttachmentNotFoundError``，
HTTP 层按错误码映射 400/404/413/415。
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from bootstrap.attachments.blob_store import AttachmentBlobStore, build_storage_key
from bootstrap.attachments.validation import AttachmentError, ValidatedAttachment, validate_upload
from bootstrap.db.repository.attachment_repo import (
    AttachmentNotFoundError,
    AttachmentRecord,
    AttachmentRepository,
)
from agent.config_models import AttachmentConfig

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UploadResult:
    attachment_id: str
    url: str
    storage_key: str  # 仅服务端使用（HTTP 返回不携带）


class AttachmentService:
    """附件服务（HTTP 端点业务编排；ownership 由调用方预派生）。"""

    def __init__(
        self,
        repo: AttachmentRepository,
        blob_store: AttachmentBlobStore,
        config: AttachmentConfig,
    ) -> None:
        self._repo = repo
        self._blobs = blob_store
        self._config = config

    async def upload(
        self,
        *,
        account_id: uuid.UUID | str,
        tenant_id: str,
        filename: str,
        data: bytes,
    ) -> UploadResult:
        """校验 → staged → committed（metadata 与 blob rename 编排）。"""
        validated: ValidatedAttachment = await validate_upload(data, filename, self._config)
        att_id = uuid.uuid4()
        # 1. staging 落盘（blob 先于 metadata 存在；崩溃只产生 orphan）
        self._blobs.stage_bytes(att_id, data)
        # 2. rename 到最终路径（同一文件系统原子操作）
        self._blobs.commit(att_id, validated.server_ext, staging_name=f"{att_id}.bin")
        storage_key = build_storage_key(att_id, validated.server_ext)
        # 3. metadata 提交（staged→committed；blob rename 后才视为可读）
        await self._repo.create_attachment(
            account_id=account_id,
            tenant_id=tenant_id,
            size_bytes=validated.size_bytes,
            detected_mime=validated.detected_mime,
            server_ext=validated.server_ext,
            filename_display=validated.filename_display,
            checksum_sha256=validated.checksum_sha256,
            storage_key=storage_key,
            temp_ttl_hours=self._config.temp_ttl_hours,
            attachment_id=att_id,
        )
        # C6 契约：URL 只按 attachment_id 构造，不回显 storage_key/本地路径。
        return UploadResult(
            attachment_id=str(att_id),
            url=f"/api/chat/media?attachment_id={att_id}",
            storage_key=storage_key,
        )

    async def fetch(
        self,
        *,
        account_id: uuid.UUID | str,
        tenant_id: str,
        attachment_id: uuid.UUID | str,
    ) -> tuple[AttachmentRecord, bytes]:
        """按 id + ownership 读取；跨租户/不存在 → None 语义（404）；blob 缺失标记 missing。

        返回 (record, bytes)。blob 缺失时标记 missing 并抛 AttachmentNotFoundError。
        """
        record = await self._repo.get_owned(
            account_id=account_id,
            tenant_id=tenant_id,
            attachment_id=attachment_id,
        )
        if record is None:
            raise AttachmentNotFoundError("attachment not found or not owned")
        try:
            data = self._blobs.read_bytes(record.storage_key)
        except Exception:
            # blob 缺失：标记 missing（reconciliation 语义），读取按 404 处理
            await self._repo.mark_missing(tenant_id=tenant_id, attachment_id=attachment_id)
            raise AttachmentNotFoundError("attachment blob missing") from None
        if not data:
            await self._repo.mark_missing(tenant_id=tenant_id, attachment_id=attachment_id)
            raise AttachmentNotFoundError("attachment blob missing") from None
        return record, data

    @property
    def config(self) -> AttachmentConfig:
        return self._config

    def emit(self, event_name: str, payload: dict[str, Any]) -> None:
        """telemetry 记录点钩子（C12 §8.1；task 6.1 接线）。当前为幂等 no-op。"""
        _ = event_name, payload

    def _emit_event(self, name: str, **fields: object) -> None:
        self.emit(name, dict(fields))


__all__ = ["AttachmentService", "UploadResult"]