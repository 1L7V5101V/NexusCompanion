"""C6 附件 blob 存储（openspec/changes/archive/2026-10-02-c6-attachment/design.md ADR-3）。

blob root **按租户命名空间**解析，与 C7 ``TenantPathResolver._category_root`` 同一棵树：
多租户模式 ``<workspace>/tenants/<tenant_dirname>/attachments``，单机模式
``<workspace>/attachments``。``storage_key`` = ``{attachment_id_hex}.{server_ext}``
（相对该租户根）。

每个公开方法都要求 ``tenant_id``——租户目录之外物理不可见，因此 orphan 扫描与
staging 清理的作用域天然等于单个租户，不存在「A 的对账删掉 B 的 blob」这类路径。

两段式：``stage_bytes`` 落租户根下 ``.staging/<attachment_id>/`` → ``commit`` 同文件
系统原子 ``os.replace`` 到最终路径（rename 成功后 metadata 由调用方提交）。崩溃窗口
只产生「blob 在最终路径但无 metadata」的孤儿，由 reconciliation 收敛；``grace_seconds``
让刚 rename、metadata 尚未提交的在途上传不被误删。``/tmp`` fallback 已移除
（§5.9.12：/tmp 不属于 durable backup 范围）。路径一律服务端派生，不接收客户端路径。
"""

from __future__ import annotations

import contextlib
import os
import re
import time
import uuid
from pathlib import Path
from typing import Iterable

from agent.tools.path_resolver import tenant_dirname
from bootstrap.attachments.validation import SERVER_EXT_BY_MIME

# storage_key 形态: {uuid4.hex}.{ext}（服务端派生，相对租户根的一层文件）
_STORAGE_KEY_RE = re.compile(r"^[0-9a-f]{32}\.[a-z0-9]+$")
_SERVER_EXTS = frozenset(SERVER_EXT_BY_MIME.values())
_STAGING_DIR = ".staging"


class AttachmentStorageError(RuntimeError):
    """blob 存储失败（staging/rename/读取/越界）。"""


def build_storage_key(attachment_id: uuid.UUID | str, server_ext: str) -> str:
    """服务端派生 storage_key（相对租户 attachments_root）。"""
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
    """blob 本体读写（metadata 归 AttachmentRepository；本类只管文件）。

    构造参数是 **workspace 根**而非 blob 根：blob 根由 ``tenant_id`` 派生，
    调用方无法把 store 指向别人的目录。
    """

    def __init__(self, workspace_root: Path, *, multi_tenant: bool = True) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self._multi_tenant = multi_tenant

    # ── 租户根 ──────────────────────────────────────────────────────

    def tenant_root(self, tenant_id: str) -> Path:
        """租户 attachments_root（按需创建）；tenant_id 缺失 fail-closed。"""
        if not tenant_id:
            raise AttachmentStorageError("tenant_id 缺失，拒绝解析 blob root")
        if self._multi_tenant:
            root = (
                self.workspace_root
                / "tenants"
                / tenant_dirname(tenant_id)
                / "attachments"
            )
        else:
            root = self.workspace_root / "attachments"
        root.mkdir(parents=True, exist_ok=True)
        return root

    # ── staging（两次写，ADR-3） ──────────────────────────────────────

    def stage_bytes(self, tenant_id: str, attachment_id: uuid.UUID | str, data: bytes) -> Path:
        """blob 写入该租户的 staging 区，返回 staging 路径（随后 ``commit`` 移至最终路径）。"""
        target = self._staging_dir(tenant_id, attachment_id) / f"{attachment_id}.bin"
        target.write_bytes(data)
        return target

    def staged_path(self, tenant_id: str, attachment_id: uuid.UUID | str, name: str) -> Path:
        return self._staging_dir(tenant_id, attachment_id) / name

    def commit(
        self,
        tenant_id: str,
        attachment_id: uuid.UUID | str,
        server_ext: str,
        *,
        staging_name: str | None = None,
    ) -> Path:
        """staging → 最终路径（同文件系统原子 rename）。

        ``server_ext`` 必须为冻结 allowlist 映射；返回最终路径（调用方转存
        storage_key 并同事务提交 metadata）。
        """
        if server_ext not in _SERVER_EXTS:
            raise AttachmentStorageError(f"非法 server_ext: {server_ext!r}")
        root = self.tenant_root(tenant_id)
        stage = self.staged_path(tenant_id, attachment_id, staging_name or f"{attachment_id}.bin")
        if not stage.is_file():
            raise AttachmentStorageError(f"staging 文件缺失: {stage}")
        final = root / f"{uuid.UUID(str(attachment_id)).hex}.{server_ext.lstrip('.')}"
        try:
            os.replace(stage, final)
        except OSError as exc:
            raise AttachmentStorageError(f"rename 失败: {stage} -> {final}: {exc}") from exc
        return final

    # ── 读取 / 删除（ownership 由调用方在 metadata 层判定） ─────────────

    def resolve_blob(self, tenant_id: str, storage_key: str) -> Path:
        """storage_key → 该租户根下的最终路径（拒绝逃逸与嵌套）。"""
        parse_storage_key(storage_key)
        root = self.tenant_root(tenant_id)
        path = (root / storage_key).resolve()
        if path.parent != root:
            raise AttachmentStorageError(f"storage_key 越出租户 blob root: {storage_key!r}")
        if not path.is_file():
            raise AttachmentStorageError(f"blob 不存在: {storage_key!r}")
        return path

    def read_bytes(self, tenant_id: str, storage_key: str) -> bytes:
        return self.resolve_blob(tenant_id, storage_key).read_bytes()

    def delete_blob(self, tenant_id: str, storage_key: str) -> bool:
        """删除最终 blob（清理幂等：不存在返回 False）。"""
        try:
            path = self.resolve_blob(tenant_id, storage_key)
        except AttachmentStorageError:
            return False
        try:
            path.unlink()
        except FileNotFoundError:
            return False
        return True

    def blob_exists(self, tenant_id: str, storage_key: str) -> bool:
        try:
            parse_storage_key(storage_key)
        except AttachmentStorageError:
            return False
        return (self.tenant_root(tenant_id) / storage_key).is_file()

    # ── reconciliation 枚举（作用域 = 单个租户目录） ────────────────────

    def list_blobs(self, tenant_id: str) -> list[str]:
        """该租户根下所有合法形态的 blob 文件名（staging 区不在内）。"""
        root = self.tenant_root(tenant_id)
        if not root.is_dir():
            return []
        return [
            entry.name
            for entry in root.iterdir()
            if entry.is_file() and _STORAGE_KEY_RE.match(entry.name)
        ]

    def find_orphan_keys(
        self,
        tenant_id: str,
        known_keys: Iterable[str],
        *,
        grace_seconds: float = 0.0,
    ) -> list[str]:
        """无 metadata 对应的 blob（孤儿）。

        ``grace_seconds`` 内的新写入不算孤儿——覆盖「blob 已 rename、metadata 尚未
        提交」的在途上传窗口。
        """
        known = set(known_keys)
        root = self.tenant_root(tenant_id)
        cutoff = time.time() - grace_seconds if grace_seconds > 0 else None
        out: list[str] = []
        for key in self.list_blobs(tenant_id):
            if key in known:
                continue
            if cutoff is not None:
                try:
                    if (root / key).stat().st_mtime > cutoff:
                        continue
                except FileNotFoundError:
                    continue
            out.append(key)
        return out

    def delete_keys(self, tenant_id: str, keys: Iterable[str]) -> int:
        """删除给定 blob 文件名（幂等）；返回实际删除数。"""
        root = self.tenant_root(tenant_id)
        removed = 0
        for key in keys:
            try:
                (root / key).unlink(missing_ok=True)
            except FileNotFoundError:
                continue
            except OSError:
                continue
            removed += 1
        return removed

    def cleanup_orphans(
        self,
        tenant_id: str,
        known_keys: Iterable[str],
        *,
        grace_seconds: float = 0.0,
    ) -> int:
        """删除该租户根下的孤儿 blob；返回删除数。"""
        return self.delete_keys(
            tenant_id,
            self.find_orphan_keys(tenant_id, known_keys, grace_seconds=grace_seconds),
        )

    def staging_files(self, tenant_id: str) -> list[Path]:
        """该租户 staging 区内的所有文件（超龄由 24h 清理任务删除）。"""
        stage_root = self.tenant_root(tenant_id) / _STAGING_DIR
        if not stage_root.is_dir():
            return []
        out: list[Path] = []
        for session_dir in stage_root.iterdir():
            if session_dir.is_dir():
                out.extend(p for p in session_dir.iterdir() if p.is_file())
        return out

    def find_stale_staging(self, tenant_id: str, *, older_than: float) -> list[Path]:
        """超过 ``older_than`` 秒的 staging 文件（不删除）。"""
        cutoff = time.time() - older_than
        out: list[Path] = []
        for file in self.staging_files(tenant_id):
            try:
                if file.stat().st_mtime < cutoff:
                    out.append(file)
            except FileNotFoundError:
                continue
        return out

    def cleanup_staging(self, tenant_id: str, *, older_than: float) -> int:
        """删除超龄 staging 文件并回收空目录；返回删除文件数。"""
        root = self.tenant_root(tenant_id)
        stage_root = root / _STAGING_DIR
        if not stage_root.is_dir():
            return 0
        removed = 0
        for file in self.find_stale_staging(tenant_id, older_than=older_than):
            try:
                file.unlink(missing_ok=True)
            except FileNotFoundError:
                continue
            removed += 1
        for session_dir in list(stage_root.iterdir()):
            if session_dir.is_dir() and not any(session_dir.iterdir()):
                with contextlib.suppress(OSError):
                    session_dir.rmdir()
        if not any(stage_root.iterdir()):
            with contextlib.suppress(OSError):
                stage_root.rmdir()
        return removed

    def _staging_dir(self, tenant_id: str, attachment_id: uuid.UUID | str) -> Path:
        dir_ = self.tenant_root(tenant_id) / _STAGING_DIR / str(attachment_id)
        dir_.mkdir(parents=True, exist_ok=True)
        return dir_


__all__ = [
    "AttachmentBlobStore",
    "AttachmentStorageError",
    "build_storage_key",
    "parse_storage_key",
]
