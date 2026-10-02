"""C6 AttachmentBlobStore 两段式测试（design ADR-3，纯文件系统层）。

覆盖：staging→commit 原子 rename、storage_key 一致性、崩溃窗口 orphan/missing
形态、逃逸拒绝（嵌套/越根）、staging 超龄清理幂等、孤儿 blob 清理、/tmp 立场
（root 由调用方传入，本模块不引入 /tmp fallback）。
"""

from __future__ import annotations

import time
import uuid

import pytest

from bootstrap.attachments.blob_store import (
    AttachmentBlobStore,
    AttachmentStorageError,
    build_storage_key,
    parse_storage_key,
)


@pytest.fixture
def store(tmp_path) -> AttachmentBlobStore:
    return AttachmentBlobStore(tmp_path / "attachments")


def test_stage_then_commit_roundtrip(store: AttachmentBlobStore) -> None:
    att_id = uuid.uuid4()
    stage = store.stage_bytes(att_id, b"fake-image-bytes")
    assert stage.is_file()
    final = store.commit(att_id, ".png")
    assert final.is_file()
    assert final.read_bytes() == b"fake-image-bytes"
    # storage_key 与磁盘一致
    key = build_storage_key(att_id, ".png")
    assert (store.root / key).resolve() == final.resolve()
    assert store.read_bytes(key) == b"fake-image-bytes"


def test_commit_missing_staging_raises(store: AttachmentBlobStore) -> None:
    with pytest.raises(AttachmentStorageError):
        store.commit(uuid.uuid4(), ".png")


def test_commit_rejects_non_allowlist_ext(store: AttachmentBlobStore) -> None:
    att_id = uuid.uuid4()
    store.stage_bytes(att_id, b"x")
    with pytest.raises(AttachmentStorageError):
        store.commit(att_id, ".exe")


def test_crash_window_produces_orphan(store: AttachmentBlobStore) -> None:
    """崩溃窗口 = rename 完成但 metadata 未提交 → blob 成孤儿（reconciliation 清理）。"""
    att_id = uuid.uuid4()
    store.stage_bytes(att_id, b"x")
    store.commit(att_id, ".png")
    orphans = store.list_orphan_blobs()
    assert build_storage_key(att_id, ".png") in orphans
    # cleanup_orphans 对未知 key 删除
    removed = store.cleanup_orphans(known_keys=[])
    assert removed == 1
    assert not store.blob_exists(build_storage_key(att_id, ".png"))
    # 第二次幂等
    assert store.cleanup_orphans(known_keys=[]) == 0


def test_cleanup_orphans_keeps_known(store: AttachmentBlobStore) -> None:
    k1 = uuid.uuid4()
    k2 = uuid.uuid4()
    store.stage_bytes(k1, b"a")
    store.stage_bytes(k2, b"b")
    store.commit(k1, ".png")
    store.commit(k2, ".jpg")
    known = {build_storage_key(k1, ".png")}
    removed = store.cleanup_orphans(known_keys=known)
    assert removed == 1
    assert store.blob_exists(build_storage_key(k1, ".png"))
    assert not store.blob_exists(build_storage_key(k2, ".jpg"))


def test_staging_cleanup_old(store: AttachmentBlobStore) -> None:
    att_id = uuid.uuid4()
    store.stage_bytes(att_id, b"x")
    # 手动把 mtime 拨到过去
    import os

    for f in store.staging_files():
        os.utime(f, (time.time() - 3600, time.time() - 3600))
    removed = store.cleanup_staging(older_than=600)
    assert removed >= 1
    assert store.cleanup_staging(older_than=600) == 0  # 幂等


def test_escape_rejected(store: AttachmentBlobStore) -> None:
    with pytest.raises(AttachmentStorageError):
        store.resolve_blob("../../etc/passwd")
    with pytest.raises(AttachmentStorageError):
        store.resolve_blob("nested/x.png")
    with pytest.raises(AttachmentStorageError):
        store.resolve_blob("x.exe")  # ext 不在 allowlist


def test_parse_build_storage_key_roundtrip() -> None:
    att_id = uuid.uuid4()
    key = build_storage_key(att_id, ".jpg")
    raw, ext = parse_storage_key(key)
    assert raw == att_id.hex
    assert ext == "jpg"
    with pytest.raises(AttachmentStorageError):
        parse_storage_key("../evil.png")


def test_no_tmp_fallback_semantics(store: AttachmentBlobStore) -> None:
    """blob root 恒在调用方传入的 workspace 内；/tmp 不参与任何路径。"""
    assert not str(store.root).startswith("/tmp")
    assert "nexus_uploads" not in str(store.root)