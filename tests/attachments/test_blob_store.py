"""C6 AttachmentBlobStore 两段式测试（design ADR-3，纯文件系统层）。

覆盖：租户命名空间 root、staging→commit 原子 rename、storage_key 一致性、崩溃窗口
orphan 形态与年龄宽限、**跨租户物理不可见**、staging 超龄清理幂等、逃逸拒绝、
/tmp 立场（root 由 workspace + tenant_id 派生，本模块不引入 /tmp fallback）。
"""

from __future__ import annotations

import os
import time
import uuid

import pytest

from bootstrap.attachments.blob_store import (
    AttachmentBlobStore,
    AttachmentStorageError,
    build_storage_key,
    parse_storage_key,
)

TENANT = "tenant-a"


@pytest.fixture
def store(tmp_path) -> AttachmentBlobStore:
    return AttachmentBlobStore(tmp_path, multi_tenant=True)


def test_blob_root_is_tenant_namespace(store: AttachmentBlobStore) -> None:
    """spec「每个附件落在该 tenant 目录内」：物理路径必须含 tenants/<dirname>/attachments。"""
    att_id = uuid.uuid4()
    store.stage_bytes(TENANT, att_id, b"x")
    final = store.commit(TENANT, att_id, ".png")
    parts = final.parts
    assert "tenants" in parts and "attachments" in parts
    assert final.parent == store.tenant_root(TENANT)


def test_missing_tenant_id_fails_closed(store: AttachmentBlobStore) -> None:
    with pytest.raises(AttachmentStorageError):
        store.tenant_root("")
    with pytest.raises(AttachmentStorageError):
        store.stage_bytes("", uuid.uuid4(), b"x")


def test_stage_then_commit_roundtrip(store: AttachmentBlobStore) -> None:
    att_id = uuid.uuid4()
    stage = store.stage_bytes(TENANT, att_id, b"fake-image-bytes")
    assert stage.is_file()
    final = store.commit(TENANT, att_id, ".png")
    assert final.is_file()
    assert final.read_bytes() == b"fake-image-bytes"
    key = build_storage_key(att_id, ".png")
    assert (store.tenant_root(TENANT) / key).resolve() == final.resolve()
    assert store.read_bytes(TENANT, key) == b"fake-image-bytes"


def test_commit_missing_staging_raises(store: AttachmentBlobStore) -> None:
    with pytest.raises(AttachmentStorageError):
        store.commit(TENANT, uuid.uuid4(), ".png")


def test_commit_rejects_non_allowlist_ext(store: AttachmentBlobStore) -> None:
    att_id = uuid.uuid4()
    store.stage_bytes(TENANT, att_id, b"x")
    with pytest.raises(AttachmentStorageError):
        store.commit(TENANT, att_id, ".exe")


def test_crash_window_produces_orphan(store: AttachmentBlobStore) -> None:
    """崩溃窗口 = rename 完成但 metadata 未提交 → blob 成孤儿（reconciliation 清理）。"""
    att_id = uuid.uuid4()
    store.stage_bytes(TENANT, att_id, b"x")
    store.commit(TENANT, att_id, ".png")
    key = build_storage_key(att_id, ".png")
    assert key in store.find_orphan_keys(TENANT, set())
    assert store.cleanup_orphans(TENANT, set()) == 1
    assert not store.blob_exists(TENANT, key)
    assert store.cleanup_orphans(TENANT, set()) == 0  # 幂等


def test_orphan_grace_spares_in_flight_upload(store: AttachmentBlobStore) -> None:
    """rename 刚完成、metadata 尚未提交的文件在宽限期内不判孤儿。"""
    att_id = uuid.uuid4()
    store.stage_bytes(TENANT, att_id, b"x")
    store.commit(TENANT, att_id, ".png")
    key = build_storage_key(att_id, ".png")
    assert store.find_orphan_keys(TENANT, set(), grace_seconds=900) == []
    assert store.cleanup_orphans(TENANT, set(), grace_seconds=900) == 0
    # 超过宽限期后才收敛为孤儿
    stale = time.time() - 3600
    os.utime(store.tenant_root(TENANT) / key, (stale, stale))
    assert store.find_orphan_keys(TENANT, set(), grace_seconds=900) == [key]


def test_cleanup_orphans_keeps_known(store: AttachmentBlobStore) -> None:
    k1 = uuid.uuid4()
    k2 = uuid.uuid4()
    store.stage_bytes(TENANT, k1, b"a")
    store.stage_bytes(TENANT, k2, b"b")
    store.commit(TENANT, k1, ".png")
    store.commit(TENANT, k2, ".jpg")
    known = {build_storage_key(k1, ".png")}
    assert store.cleanup_orphans(TENANT, known) == 1
    assert store.blob_exists(TENANT, build_storage_key(k1, ".png"))
    assert not store.blob_exists(TENANT, build_storage_key(k2, ".jpg"))


def test_tenants_are_physically_disjoint(store: AttachmentBlobStore) -> None:
    """A 的对账物理接触不到 B 的目录——跨租户误删在 root 层面就被排除。"""
    a = uuid.uuid4()
    b = uuid.uuid4()
    store.stage_bytes("tenant-a", a, b"a")
    store.commit("tenant-a", a, ".png")
    store.stage_bytes("tenant-b", b, b"b")
    store.commit("tenant-b", b, ".png")
    key_a = build_storage_key(a, ".png")
    key_b = build_storage_key(b, ".png")

    assert store.tenant_root("tenant-a") != store.tenant_root("tenant-b")
    assert store.list_blobs("tenant-a") == [key_a]
    assert store.list_blobs("tenant-b") == [key_b]
    # B 的 blob 不在 A 的孤儿集合里，且 A 的清理不触碰 B
    assert store.find_orphan_keys("tenant-a", set()) == [key_a]
    assert key_b not in store.find_orphan_keys("tenant-a", set())
    assert store.cleanup_orphans("tenant-a", set()) == 1
    assert store.blob_exists("tenant-b", key_b)
    assert store.delete_blob("tenant-a", key_b) is False


def test_staging_cleanup_old(store: AttachmentBlobStore) -> None:
    att_id = uuid.uuid4()
    store.stage_bytes(TENANT, att_id, b"x")
    for f in store.staging_files(TENANT):
        stale = time.time() - 3600
        os.utime(f, (stale, stale))
    assert store.cleanup_staging(TENANT, older_than=600) >= 1
    assert store.cleanup_staging(TENANT, older_than=600) == 0  # 幂等


def test_escape_rejected(store: AttachmentBlobStore) -> None:
    with pytest.raises(AttachmentStorageError):
        store.resolve_blob(TENANT, "../../etc/passwd")
    with pytest.raises(AttachmentStorageError):
        store.resolve_blob(TENANT, "nested/x.png")
    with pytest.raises(AttachmentStorageError):
        store.resolve_blob(TENANT, "x.exe")  # ext 不在 allowlist


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
    root = str(store.tenant_root(TENANT))
    assert not root.startswith("/tmp")
    assert "nexus_uploads" not in root
