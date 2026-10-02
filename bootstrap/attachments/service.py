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
from pathlib import Path
from typing import TYPE_CHECKING

from bootstrap.attachments.blob_store import AttachmentBlobStore, build_storage_key
from bootstrap.attachments.validation import AttachmentError, ValidatedAttachment, validate_upload
from bootstrap.db.repository.attachment_repo import (
    AttachmentNotFoundError,
    AttachmentRecord,
    AttachmentRepository,
)
from agent.config_models import AttachmentConfig

if TYPE_CHECKING:
    from bootstrap.attachments.telemetry import AttachmentTelemetry

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
        telemetry: "AttachmentTelemetry | None" = None,
    ) -> None:
        self._repo = repo
        self._blobs = blob_store
        self._config = config
        self._telemetry = telemetry

    async def upload(
        self,
        *,
        account_id: uuid.UUID | str,
        tenant_id: str,
        filename: str,
        data: bytes,
    ) -> UploadResult:
        """校验 → staged → committed（metadata 与 blob rename 编排）。"""
        try:
            return await self._upload_inner(
                account_id=account_id, tenant_id=tenant_id,
                filename=filename, data=data,
            )
        except Exception as exc:
            self._emit_upload_error(tenant_id, exc)
            raise

    async def _upload_inner(
        self,
        *,
        account_id: uuid.UUID | str,
        tenant_id: str,
        filename: str,
        data: bytes,
    ) -> UploadResult:
        validated: ValidatedAttachment = await validate_upload(data, filename, self._config)
        att_id = uuid.uuid4()
        # 1. staging 落盘（blob 先于 metadata 存在；崩溃只产生 orphan）
        self._blobs.stage_bytes(tenant_id, att_id, data)
        # 2. rename 到该租户 blob root 下的最终路径（同一文件系统原子操作）
        self._blobs.commit(
            tenant_id, att_id, validated.server_ext, staging_name=f"{att_id}.bin"
        )
        storage_key = build_storage_key(att_id, validated.server_ext)
        # 3. metadata 落库（status=staged）：staged 在此表示「已入库、尚无 message 引用」，
        #    按 §5.9.15 冻结值享 24h 未引用清理；协议帧接入引用后由 commit_attachment
        #    推进 committed 并按 referenced_ttl_days 重算 deadline（C6 Non-Goal：v0 帧
        #    不带附件字段）。因此不可在此 commit——那会把未引用附件的保留期变成 30d。
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
        self._emit_upload_success(tenant_id, size_bytes=validated.size_bytes)
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
    ) -> tuple[AttachmentRecord, Path]:
        """按 id + ownership 读取，返回 (record, blob 路径) 供调用方流式响应。

        跨租户/不存在/blob 缺失 → ``AttachmentNotFoundError``（HTTP 层统一 404，
        不区分三者以免泄露存在性）；blob 缺失同时标记 missing。
        """
        record = await self._repo.get_owned(
            account_id=account_id,
            tenant_id=tenant_id,
            attachment_id=attachment_id,
        )
        if record is None:
            exc = AttachmentNotFoundError("attachment not found or not owned")
            self._emit_fetch_error(tenant_id, exc)
            raise exc
        try:
            path = self._blobs.resolve_blob(tenant_id, record.storage_key)
            actual_size = path.stat().st_size
        except Exception:
            # blob 缺失：标记 missing（reconciliation 语义），读取按 404 处理
            await self._repo.mark_missing(tenant_id=tenant_id, attachment_id=attachment_id)
            exc = AttachmentNotFoundError("attachment blob missing")
            self._emit_fetch_error(tenant_id, exc)
            raise exc from None
        if actual_size != record.size_bytes:
            # 字节数与 metadata 不符 = blob 被截断或替换：不服务可疑内容，一律 404。
            # mark_missing 只对 committed 行生效（规格里 missing = 「已提交但 blob 缺失」），
            # staged 行留给 24h 清理收敛。读取时全量 sha256 复算与流式响应互斥，
            # 热路径只做 size 校验；checksum 正确性由契约用例保证读出字节可复算比对。
            await self._repo.mark_missing(tenant_id=tenant_id, attachment_id=attachment_id)
            exc = AttachmentNotFoundError("attachment blob size mismatch")
            self._emit_fetch_error(tenant_id, exc)
            raise exc from None
        self._emit_fetch_success(tenant_id)
        return record, path

    @property
    def config(self) -> AttachmentConfig:
        return self._config

    def _emit_upload_success(self, tenant_id: str, *, size_bytes: int) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.upload_finished(
                tenant_id=tenant_id, status="succeeded", size_bytes=size_bytes
            )
        except Exception:
            logger.exception("attachment upload telemetry 异常（不阻断主流程）")

    def _emit_upload_error(self, tenant_id: str, exc: Exception) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.upload_finished(
                tenant_id=tenant_id,
                status="failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
        except Exception:
            logger.exception("attachment upload telemetry 异常（不阻断主流程）")

    def _emit_fetch_success(self, tenant_id: str) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.fetch_finished(tenant_id=tenant_id, status="succeeded")
        except Exception:
            logger.exception("attachment fetch telemetry 异常（不阻断主流程）")

    def _emit_fetch_error(self, tenant_id: str, exc: Exception) -> None:
        if self._telemetry is None:
            return
        try:
            self._telemetry.fetch_finished(
                tenant_id=tenant_id,
                status="failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
        except Exception:
            logger.exception("attachment fetch telemetry 异常（不阻断主流程）")


__all__ = ["AttachmentService", "UploadResult"]