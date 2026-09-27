"""C8 tenant secret 静态加密测试（task-08 2.3，§5.9.16）。

覆盖：AES-256-GCM 加密往返、sealed 值内嵌 key_id 与版本、明文不泄漏、
rotation（新 key 加密 + 旧值可解）、撤销（旧 key 删除后解密失败）、
篡改检测（nonce/密文任一字节）、keyring 目录加载与 ACTIVE_KEY 指针、
fail-closed（空密钥环/指向缺失 key）。
"""

from __future__ import annotations

import pytest

from core.crypto.secret_box import (
    SecretBox,
    SecretBoxError,
    _b64encode,
    key_id_for,
)


def _make_box(rotated: bool = False) -> SecretBox:
    k1_id, k1 = SecretBox.generate_key()
    box = SecretBox({k1_id: k1}, active_key_id=k1_id)
    if rotated:
        k2_id, k2 = SecretBox.generate_key()
        box = SecretBox({k1_id: k1, k2_id: k2}, active_key_id=k2_id)
    return box


def test_encrypt_decrypt_roundtrip() -> None:
    box = _make_box()
    sealed = box.encrypt("tenant-api-key: sk-abc123")
    assert box.decrypt(sealed) == "tenant-api-key: sk-abc123"


def test_sealed_value_embeds_version_and_key_id_and_no_plaintext() -> None:
    box = _make_box()
    secret = "super-secret-plaintext"
    sealed = box.encrypt(secret)
    version, key_id, nonce, ct = sealed.split(":")
    assert version == "v1"
    assert key_id == box.active_key_id
    assert key_id == key_id_for(box._keys[key_id])
    # 明文与其任何子串都不得出现在 sealed 值里
    assert secret not in sealed
    assert secret[:4] not in sealed
    assert secret[-4:] not in sealed
    assert nonce and ct


def test_same_plaintext_two_ciphertexts_random_nonce() -> None:
    box = _make_box()
    sealed_a = box.encrypt("same")
    sealed_b = box.encrypt("same")
    assert sealed_a != sealed_b


def test_rotation_new_key_encrypts_old_still_decryptable() -> None:
    k1_id, k1 = SecretBox.generate_key()
    old_box = SecretBox({k1_id: k1}, active_key_id=k1_id)
    old_sealed = old_box.encrypt("rotate-me")

    k2_id, k2 = SecretBox.generate_key()
    rotated_box = SecretBox({k1_id: k1, k2_id: k2}, active_key_id=k2_id)
    new_sealed = rotated_box.encrypt("rotate-me-again")

    assert rotated_box.active_key_id == k2_id
    assert new_sealed.split(":")[1] == k2_id
    # rotation 后：新数据用新 key、旧 sealed 值仍可解
    assert rotated_box.decrypt(old_sealed) == "rotate-me"
    assert rotated_box.decrypt(new_sealed) == "rotate-me-again"


def test_revoked_key_sealed_value_fails() -> None:
    k1_id, k1 = SecretBox.generate_key()
    k2_id, k2 = SecretBox.generate_key()
    box = SecretBox({k1_id: k1, k2_id: k2}, active_key_id=k1_id)
    old_sealed = box.encrypt("will-be-revoked")

    revoked_box = SecretBox({k2_id: k2}, active_key_id=k2_id)
    with pytest.raises(SecretBoxError, match=k1_id):
        revoked_box.decrypt(old_sealed)


def test_tampered_nonce_or_ciphertext_rejected() -> None:
    box = _make_box()
    sealed = box.encrypt("tamper-check")
    version, key_id, nonce_b64, ct_b64 = sealed.split(":")

    def _flip_first_char(text: str) -> str:
        # 翻转首字符：base64 尾字符的低比特可能只是 padding（翻转不解码出
        # 不同的字节），首字符的 6 比特必然有效。urlsafe_b64 输出字母表为
        # A-Za-z0-9-_，替换字符与原字符必不相同，解码字节必变化。
        replacement = "A" if text[0] != "A" else "B"
        return replacement + text[1:]

    with pytest.raises(SecretBoxError):
        box.decrypt(":".join([version, key_id, _flip_first_char(nonce_b64), ct_b64]))
    with pytest.raises(SecretBoxError):
        box.decrypt(":".join([version, key_id, nonce_b64, _flip_first_char(ct_b64)]))
    with pytest.raises(SecretBoxError, match="格式非法"):
        box.decrypt("garbage")
    with pytest.raises(SecretBoxError, match="格式非法"):
        box.decrypt("v9:x:y:z")


def test_keyring_dir_roundtrip_and_active_pointer(tmp_path) -> None:
    box = _make_box()
    keys_dir = tmp_path / "keys"
    box.write_keyring_dir(keys_dir)

    loaded = SecretBox.from_keyring_dir(keys_dir)
    assert loaded.active_key_id == box.active_key_id
    assert set(loaded.key_ids) == set(box.key_ids)
    sealed = loaded.encrypt("persisted")
    assert loaded.decrypt(sealed) == "persisted"
    assert box.decrypt(sealed) == "persisted"


def test_keyring_rotation_via_dir(tmp_path) -> None:
    k1_id, k1 = SecretBox.generate_key()
    SecretBox({k1_id: k1}, active_key_id=k1_id).write_keyring_dir(tmp_path)
    old_sealed = SecretBox.from_keyring_dir(tmp_path).encrypt("legacy")

    # rotation：新增 key 文件 + 更新 ACTIVE_KEY 指针
    k2_id, k2 = SecretBox.generate_key()
    (tmp_path / f"secret_key_{k2_id}").write_text(_b64encode(k2), encoding="ascii")
    (tmp_path / "ACTIVE_KEY").write_text(k2_id, encoding="ascii")

    rotated = SecretBox.from_keyring_dir(tmp_path)
    assert rotated.active_key_id == k2_id
    assert rotated.decrypt(old_sealed) == "legacy"

    # 撤销：删除 k1 文件后其 sealed 值不可解
    (tmp_path / f"secret_key_{k1_id}").unlink()
    revoked = SecretBox.from_keyring_dir(tmp_path)
    with pytest.raises(SecretBoxError):
        revoked.decrypt(old_sealed)


def test_keyring_fail_closed_cases(tmp_path) -> None:
    with pytest.raises(SecretBoxError, match="不存在"):
        SecretBox.from_keyring_dir(tmp_path / "missing")

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(SecretBoxError, match="没有密钥文件"):
        SecretBox.from_keyring_dir(empty)

    # ACTIVE_KEY 指向不存在的 key → 启动失败（不做静默降级）
    k1_id, k1 = SecretBox.generate_key()
    ring = tmp_path / "ring"
    SecretBox({k1_id: k1}, active_key_id=k1_id).write_keyring_dir(ring)
    (ring / "ACTIVE_KEY").write_text("deadbeef0000", encoding="ascii")
    with pytest.raises(SecretBoxError, match="ACTIVE_KEY"):
        SecretBox.from_keyring_dir(ring)

    # 密钥材料长度不对 / key_id 与内容不一致 → 拒绝
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "secret_key_aaaaaaaaaaaa").write_text("AAAA", encoding="ascii")
    with pytest.raises(SecretBoxError):
        SecretBox.from_keyring_dir(bad)


def test_box_rejects_bad_construction() -> None:
    with pytest.raises(SecretBoxError):
        SecretBox({}, active_key_id="x")
    key_id, key = SecretBox.generate_key()
    with pytest.raises(SecretBoxError, match="长度"):
        SecretBox({key_id: key[:16]}, active_key_id=key_id)
    with pytest.raises(SecretBoxError, match="不在密钥环"):
        SecretBox({key_id: key}, active_key_id="other")
