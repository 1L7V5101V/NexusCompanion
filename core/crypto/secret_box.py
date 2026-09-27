"""C8 tenant secret 静态加密原语（task-08 P2，§5.9.16 / §10 OPEN FOR P-1 SPEC）。

冻结决策：tenant secret 必须静态加密，不能进入 tool schema、模型可见参数、
普通日志、metrics label 或错误字符串；密钥轮换/撤销的生效边界必须有测试。

设计（ADR-7）：
- **算法**：AES-256-GCM（``cryptography`` AESGCM），96-bit 随机 nonce 每次加密
  新生成；GCM 认证标签保证完整性——密文/nonce 任一字节被篡改即解密失败。
- **sealed 格式**：``v1:<key_id>:<base64url(nonce)>:<base64url(ct+tag)>``，
  key_id 内嵌使解密端按值选钥；key_id = 密钥 SHA-256 前 12 hex。
- **key source**：workspace ``keys/`` 目录每 key 一个文件
  ``secret_key_<key_id>``（内容 base64url 32 字节）；``keys/ACTIVE_KEY`` 指针
  文件决定 active 加密钥（缺省取字典序最大 key_id）。
- **rotation**：新增 key 文件 + 更新 ACTIVE_KEY → 新数据用新 key 加密、旧
  sealed 值仍可解；**撤销** = 删除 key 文件，该 key 的 sealed 值解密失败。
- 密钥文件读取失败/缺失 → 启动失败（fail-closed），不做静默降级。

本模块只交付原语；tenant secret 的存储字段与消费组件（谁落盘、哪张表）归
C5/C14 接入时落地。``SecretBox`` 不提供把明文写入日志的通道；调用方保证不把
明文放进 tool schema/metrics label（观测侧白名单归 C12 label policy）。
"""

from __future__ import annotations

import base64
import hashlib
import logging
import os
import secrets
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

logger = logging.getLogger(__name__)

SEALED_FORMAT_VERSION = "v1"
_KEY_BYTES = 32
_NONCE_BYTES = 12
_KEY_FILE_PREFIX = "secret_key_"
_ACTIVE_KEY_FILE = "ACTIVE_KEY"


class SecretBoxError(RuntimeError):
    """密钥环缺失/损坏或 sealed 值解密失败（fail-closed，不返回部分明文）。"""


def _b64encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def key_id_for(key: bytes) -> str:
    """密钥指纹（SHA-256 前 12 hex），仅用于选钥与文件命名，不含密钥材料。"""
    return hashlib.sha256(key).hexdigest()[:12]


class SecretBox:
    """多 key AES-256-GCM 静态加密盒：active 加密、按 key_id 解密。"""

    def __init__(self, keys: dict[str, bytes], *, active_key_id: str) -> None:
        if not keys:
            raise SecretBoxError("SecretBox 需要至少一把密钥")
        for key_id, key in keys.items():
            if len(key) != _KEY_BYTES:
                raise SecretBoxError(f"密钥 {key_id} 长度必须为 {_KEY_BYTES} 字节")
        if active_key_id not in keys:
            raise SecretBoxError(f"active key {active_key_id} 不在密钥环中")
        self._keys = dict(keys)
        self._active_key_id = active_key_id

    @property
    def active_key_id(self) -> str:
        return self._active_key_id

    @property
    def key_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._keys))

    def encrypt(self, plaintext: str) -> str:
        """用 active key 加密；返回内嵌版本与 key_id 的 sealed 值。"""
        key = self._keys[self._active_key_id]
        nonce = secrets.token_bytes(_NONCE_BYTES)
        sealed = AESGCM(key).encrypt(nonce, plaintext.encode("utf-8"), None)
        return (
            f"{SEALED_FORMAT_VERSION}:{self._active_key_id}:"
            f"{_b64encode(nonce)}:{_b64encode(sealed)}"
        )

    def decrypt(self, sealed_value: str) -> str:
        """按 sealed 值内嵌 key_id 选钥解密；认证失败/密钥缺失 → SecretBoxError。"""
        parts = sealed_value.split(":")
        if len(parts) != 4 or parts[0] != SEALED_FORMAT_VERSION:
            raise SecretBoxError("sealed 值格式非法")
        version, key_id, nonce_b64, ct_b64 = parts
        if version != SEALED_FORMAT_VERSION:
            raise SecretBoxError(f"不支持的 sealed 版本: {version}")
        key = self._keys.get(key_id)
        if key is None:
            raise SecretBoxError(f"密钥 {key_id} 不在密钥环中（已撤销或未加载）")
        try:
            plaintext = AESGCM(key).decrypt(
                _b64decode(nonce_b64), _b64decode(ct_b64), None
            )
        except InvalidTag as exc:
            raise SecretBoxError(f"sealed 值认证失败（key {key_id}）") from exc
        return plaintext.decode("utf-8")

    @classmethod
    def generate_key(cls) -> tuple[str, bytes]:
        """生成一把新密钥，返回 (key_id, key bytes)。"""
        key = secrets.token_bytes(_KEY_BYTES)
        return key_id_for(key), key

    @classmethod
    def from_keyring_dir(cls, keys_dir: str | Path) -> SecretBox:
        """从 workspace keys/ 目录加载密钥环（rotation 即：加文件 + 换指针）。

        - ``secret_key_<key_id>``：内容为 base64url 32 字节密钥；
        - ``ACTIVE_KEY``：内容为 active key_id（缺省取字典序最大 key_id）。
        """
        root = Path(keys_dir)
        if not root.is_dir():
            raise SecretBoxError(f"密钥目录不存在: {root}")
        keys: dict[str, bytes] = {}
        for entry in sorted(root.glob(f"{_KEY_FILE_PREFIX}*")):
            key_id = entry.name[len(_KEY_FILE_PREFIX):]
            try:
                key = _b64decode(entry.read_text(encoding="ascii").strip())
            except (ValueError, UnicodeDecodeError) as exc:
                raise SecretBoxError(f"密钥文件 {entry.name} 解析失败") from exc
            if key_id_for(key) != key_id:
                raise SecretBoxError(
                    f"密钥文件 {entry.name} 内容与文件名 key_id 不一致"
                )
            keys[key_id] = key
        if not keys:
            raise SecretBoxError(f"密钥目录 {root} 中没有密钥文件")
        pointer = root / _ACTIVE_KEY_FILE
        if pointer.exists():
            active_key_id = pointer.read_text(encoding="ascii").strip()
            if active_key_id not in keys:
                raise SecretBoxError(
                    f"ACTIVE_KEY 指向的密钥 {active_key_id} 不在密钥环中"
                )
        else:
            active_key_id = max(keys)
        return cls(keys, active_key_id=active_key_id)

    def write_keyring_dir(self, keys_dir: str | Path) -> None:
        """把当前密钥环落盘（生成密钥目录用；rotation 流程由调用方编排）。"""
        root = Path(keys_dir)
        root.mkdir(parents=True, exist_ok=True)
        for key_id, key in self._keys.items():
            path = root / f"{_KEY_FILE_PREFIX}{key_id}"
            if path.exists():
                continue
            _write_private_file(path, _b64encode(key))
        pointer = root / _ACTIVE_KEY_FILE
        _write_private_file(pointer, self._active_key_id)


def _write_private_file(path: Path, content: str) -> None:
    path.write_text(content, encoding="ascii")
    try:
        os.chmod(path, 0o600)
    except OSError:
        # Windows 文件系统（FAT/部分挂载）不支持 POSIX 权限位时忽略。
        logger.debug("chmod 0600 不适用: %s", path)
