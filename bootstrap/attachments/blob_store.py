"""C6 附件 blob 存储（openspec/changes/c6-attachment/design.md ADR-3）。

blob root = C7 ``TenantPathResolver.attachments_root``（租户命名空间物理根，与工具
读取同一棵树）；``storage_key`` = ``{attachment_id}.{server_ext}``（相对该根）。
两段式：``stage_bytes`` 落 ``.staging/<session>/`` → ``commit`` 同文件系统原子
``os.rename`` 到最终路径（rename 成功后 metadata 由调用方同事务提交）；崩溃窗口
只产生 orphan/missing 一态（staging 超龄由清理、最终路径有 blob 无 metadata 由
reconciliation 收敛）。``/tmp`` fallback 已移除（§5.9.12：/tmp 不属于 durable
backup 范围）。路径一律服务端派生，不接收客户端路径。
"""

from __future__ import annotations

import contextlib
import os
import re
import uuid
from pathlib import Path
from typing import Iterable

from bootstrap.attachments.validation import SERVER_EXT_BY_MIME, AttachmentError

# storage_key 形态: {uuid4}.{ext}（服务端派生）
_STORAGE_KEY_RE = re.compile(r"^[0-9a-f]{32}\.[a-z0-9]+$")
_SERVER_EXTS = frozenset(SERVER_EXT_BY_MIME.values())


class AttachmentStorageError(RuntimeError):
    """blob 存储失败（staging/rename/读取）。"""


def build_storage_key(attachment_id: uuid.UUID | str, server_ext: str) -> str:
    """服务端派生 storage_key（相对 attachments_root）。"""
    if server_ext not in _SERVER_EXTS:
        raise AttachmentStorageError(f"非法 server_ext: {server_ext!r}")
    return f"{uuid.UUID(str(attachment_id)).hex}.{server_ext.lstrip('.')}"


def parse_storage_key(storage_key: str) -> tuple[str, str]:
    """storage_key → (attachment_id_hex, ext)；非法形态拒绝。"""
    if not _STORAGE_KEY_RE.match(storage_key):
        raise AttachmentStorageError(f"非法 storage_key: {storage_key!r}")
    raw_id, ext = storage_key.rsplit(".", 1)
    if f".{ext}" not in _SERVER_EXTS:
        raise AttachmentStorageError(f"storage_key 扩展名非法: {ext!r}")
    return raw_id, ext


class AttachmentBlobStore:
    """blob 本体读写（metadata 归 AttachmentRepository；本类只管文件）。"""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # ── staging（两次写，ADR-3） ──────────────────────────────────────

    def stage_bytes(self, attachment_id: uuid.UUID | str, data: bytes) -> Path:
        """blob 写入 staging（未校验前/校验通过后都可先落此）。

        返回 staging 路径；调用方后续 ``commit`` 移至最终路径。
        """
        name = f"{attachment_id}.bin"
        stage_dir = self._staging_dir(attachment_id)
        target = stage_dir / name
        target.write_bytes(data)
        return target

    def staged_path(self, attachment_id: uuid.UUID | str, name: str) -> Path:
        return self._staging_dir(attachment_id) / name

    def commit(
        self,
        attachment_id: uuid.UUID | str,
        server_ext: str,
        *,
        staging_name: str | None = None,
    ) -> Path:
        """staging → 最终路径（同文件系统原子 rename）。

        ``server_ext`` 必须为冻结 allowlist 映射；返回最终路径（调用方转存
        storage_key 并同事务提交 metadata）。
        """
        ext = server_ext.lstrip(".")
        if server_ext not in _SERVER_EXTS:
            raise AttachmentStorageError(f"非法 server_ext: {server_ext!r}")
        stage = self.staged_path(attachment_id, staging_name or f"{attachment_id}.bin")
        if not stage.is_file():
            raise AttachmentStorageError(f"staging 文件缺失: {stage}")
        final = self.root / f"{uuid.UUID(str(attachment_id)).hex}.{ext}"
        final.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.replace(stage, final)
        except OSError as exc:
            raise AttachmentStorageError(f"rename 失败: {stage} -> {final}: {exc}") from exc
        return final

    # ── 读取 / 校验（ownership 由调用方在 metadata 层判定） ─────────────────

    def resolve_blob(self, storage_key: str) -> Path:
        """storage_key → 最终路径（拒绝逃逸：必须恰在 root 内一层的合法形态）。"""
        raw_id, ext = parse_storage_key(storage_key)
        path = (self.root / storage_key).resolve()
        if not path.is_relative_to(self.root):
            raise AttachmentStorageError(f"storage_key 越出 blob root: {storage_key!r}")
        if path.parent != self.root:
            raise AttachmentStorageError(f"storage_key 嵌套非法: {storage_key!r}")
        if not (path.is_file() or path.exists()):
            raise AttachmentStorageError(f"blob 不存在: {storage_key!r}")
        _ = raw_id  # 形态已由 regex 校验
        return path

    def read_bytes(self, storage_key: str) -> bytes:
        path = self.resolve_blob(storage_key)
        return path.read_bytes()

    def delete_blob(self, storage_key: str) -> bool:
        """删除最终 blob（清理幂等：不存在返回 False）。"""
        try:
            path = self.resolve_blob(storage_key)
        except AttachmentStorageError:
            return False
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    def blob_exists(self, storage_key: str) -> bool:
        try:
            path = self.resolve_blob(storage_key)
        except AttachmentStorageError:
            return False
        return path.is_file()

    # ── reconciliation 枚举 ─────────────────────────────────────────

    def list_orphan_blobs(self) -> list[str]:
        """root 下无 metadata 的 blob storage_key（含 staging 目录内文件，不含 .staging）。"""
        out: list[str] = []
        if not self.root.is_dir():
            return out
        for entry in self.root.iterdir():
            if not entry.is_file():
                continue
            if not _STORAGE_KEY_RE.match(entry.name):
                continue
            out.append(entry.name)
        return out

    def staging_files(self) -> list[Path]:
        """所有 staging 文件（超龄由 24h 清理任务删除）。"""
        stage_root = self.root / ".staging"
        if not stage_root.is_dir():
            return []
        out: list[Path] = []
        for session_dir in stage_root.iterdir():
            if session_dir.is_dir():
                out.extend(p for p in session_dir.iterdir() if p.is_file())
        return out

    def cleanup_staging(self, *, older_than: float) -> int:
        """删除超过 ``older_than`` 秒的 staging 文件与其空父目录；返回删除文件数。"""
        import time

        cutoff = time.time() - older_than
        removed = 0
        stage_root = self.root / ".staging"
        if not stage_root.is_dir():
            return 0
        for session_dir in list(stage_root.iterdir()):
            if not session_dir.is_dir():
                continue
            for file in list(session_dir.iterdir()):
                try:
                    if file.stat().st_mtime < cutoff:
                        file.unlink(missing_ok=True)
                        removed += 1
                except FileNotFoundError:
                    continue
            # 空目录回收（幂等：第二次不再命中文件）
            if not any(session_dir.iterdir()):
                with contextlib.suppress(OSError):
                    session_dir.rmdir()
        if not any(stage_root.iterdir()):
            with contextlib.suppress(OSError):
                stage_root.rmdir()
        return removed

    def cleanup_orphans(self, known_keys: Iterable[str]) -> int:
        """删除 metadata 之外的孤儿 blob（root 下文件且不在 known_keys）；返回删除数。"""
        known = set(known_keys)
        removed = 0
        for key in self.list_orphan_blobs():
            if key in known:
                continue
            try:
                (self.root / key).unlink(missing_ok=True)
                removed += 1
            except FileNotFoundError:
                continue
        return removed

    def _staging_dir(self, attachment_id: uuid.UUID | str) -> Path:
        dir_ = self.root / ".staging" / str(attachment_id)
        dir_.mkdir(parents=True, exist_ok=True)
        return dir_


__all__ = [
    "AttachmentBlobStore",
    "AttachmentStorageError",
    "build_storage_key",
    "parse_storage_key",
]