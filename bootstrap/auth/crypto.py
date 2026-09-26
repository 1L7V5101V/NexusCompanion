"""C5 auth 凭据 crypto（design.md ADR-1）。

冻结语义（PILOT_ROADMAP §5.9.3 + §10 OPEN FOR P-1 SPEC「Digest/encryption/key
rotation」在本 change 的关闭部分）：

- 凭据原值 = ``prefix + secrets.token_urlsafe(32)``（256-bit CSPRNG）。邀请
  Token ``nxt_``、admin recovery token ``nad_``、session cookie 值 ``ns_``。
- digest = ``HMAC-SHA-256(pepper, utf8(原值))`` hex；pepper ≥32 字节 CSPRNG。
- pepper 来源优先级：env ``NEXUS_AUTH_PEPPER`` → ``<workspace>/secrets/auth_pepper``
  （首次使用自动生成 64 hex 并落盘）。不进 config.toml、不进数据库、不打日志。
- pepper 轮换 = 全部既有 digest 失效，属维护窗口操作（runbook）；``digest_version``
  列保留为未来 per-version pepper map 演进位，当前恒为 1。
- CSRF token = ``HMAC-SHA-256(pepper, "csrf:" || session_id.bytes)`` hex：
  session-bound、服务端可重算（GET /api/auth/csrf 幂等重发）、无明文落库。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
from pathlib import Path

__all__ = [
    "PepperProvider",
    "SESSION_COOKIE_VALUE_PREFIX",
    "TOKEN_PREFIX_INVITATION",
    "TOKEN_PREFIX_RECOVERY",
    "csrf_for_session",
    "digest_value",
    "new_token",
]

logger = logging.getLogger(__name__)

TOKEN_PREFIX_INVITATION = "nxt_"
TOKEN_PREFIX_RECOVERY = "nad_"
SESSION_COOKIE_VALUE_PREFIX = "ns_"

_MIN_PEPPER_BYTES = 32
_PEPPER_FILE_NAME = "auth_pepper"


def new_token(prefix: str) -> str:
    """生成 256-bit CSPRNG 凭据原值（明文只在签发/兑换响应中出现一次）。"""
    return prefix + secrets.token_urlsafe(32)


def digest_value(raw: str, pepper: bytes) -> str:
    """HMAC-SHA-256(pepper, raw) hex digest（64 字符，入库/比对唯一形态）。"""
    return hmac.new(pepper, raw.encode("utf-8"), hashlib.sha256).hexdigest()


def csrf_for_session(session_id: bytes, pepper: bytes) -> str:
    """从 session id 派生 session-bound CSRF token（可重算，不落库）。"""
    return hmac.new(
        pepper, b"csrf:" + session_id, hashlib.sha256
    ).hexdigest()


class PepperProvider:
    """解析并缓存服务端 pepper（≥32 字节）。

    解析顺序：``NEXUS_AUTH_PEPPER`` env（hex 或原始字符串，长度不足拒绝）→
    workspace secrets 文件（不存在则自动生成并写回）。文件与 env 均缺失时
    自动生成仅用于本地首次启用；生产部署应显式提供 env 并纳入 secret 备份
    （runbook：pepper 与数据库备份必须对应同一代次）。
    """

    def __init__(self, secrets_dir: Path, env_var: str = "NEXUS_AUTH_PEPPER"):
        self._secrets_dir = secrets_dir
        self._env_var = env_var
        self._pepper: bytes | None = None

    def get(self) -> bytes:
        if self._pepper is None:
            self._pepper = self._load_or_create()
        return self._pepper

    def _load_or_create(self) -> bytes:
        env_value = os.environ.get(self._env_var, "").strip()
        if env_value:
            pepper = _coerce_pepper(env_value)
            if pepper is None:
                raise ValueError(
                    f"{self._env_var} 长度不足（需 ≥{_MIN_PEPPER_BYTES} 字节）"
                )
            return pepper
        path = self._secrets_dir / _PEPPER_FILE_NAME
        if path.exists():
            file_value = path.read_text(encoding="utf-8").strip()
            pepper = _coerce_pepper(file_value)
            if pepper is None:
                raise ValueError(
                    f"pepper 文件 {path} 内容长度不足（需 ≥{_MIN_PEPPER_BYTES} 字节）"
                )
            return pepper
        pepper = secrets.token_bytes(_MIN_PEPPER_BYTES)
        self._secrets_dir.mkdir(parents=True, exist_ok=True)
        path.write_text(pepper.hex() + "\n", encoding="utf-8")
        logger.info("auth pepper 已生成并写入 %s（勿纳入普通备份/日志）", path)
        return pepper


def _coerce_pepper(value: str) -> bytes | None:
    """hex 优先解码，否则按 utf-8 原文；长度不足返回 None。"""
    raw = value.strip()
    if not raw:
        return None
    if _is_hex(raw):
        decoded = bytes.fromhex(raw)
        if len(decoded) >= _MIN_PEPPER_BYTES:
            return decoded
    encoded = raw.encode("utf-8")
    if len(encoded) >= _MIN_PEPPER_BYTES:
        return encoded
    return None


def _is_hex(value: str) -> bool:
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True
