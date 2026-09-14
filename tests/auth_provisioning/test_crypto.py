"""C5 凭据 crypto 单元测试（design ADR-1，PILOT_ROADMAP §5.9.3）。

纯 Python 无 PG 依赖：token 原值前缀/熵、HMAC-SHA-256 digest 形态、CSRF
派生确定性、pepper 来源优先级（env → workspace secrets 文件）与不落日志。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bootstrap.auth.crypto import (
    SESSION_COOKIE_VALUE_PREFIX,
    TOKEN_PREFIX_INVITATION,
    TOKEN_PREFIX_RECOVERY,
    PepperProvider,
    csrf_for_session,
    digest_value,
    new_token,
)

PEPPER = bytes.fromhex("01" * 32)


def test_new_token_has_prefix_and_high_entropy() -> None:
    """256-bit CSPRNG 原值：前缀保留、URL-safe base64、两次生成不同。"""
    a = new_token(TOKEN_PREFIX_INVITATION)
    b = new_token(TOKEN_PREFIX_INVITATION)
    assert a.startswith("nxt_") and b.startswith("nxt_")
    assert len(a) == len("nxt_") + 43  # 32 bytes → token_urlsafe 43 chars
    assert a != b
    assert new_token(TOKEN_PREFIX_RECOVERY).startswith("nad_")
    assert new_token(SESSION_COOKIE_VALUE_PREFIX).startswith("ns_")


def test_digest_value_sha256_hex_and_deterministic() -> None:
    """HMAC-SHA-256(pepper, raw) → 64 hex；同 pepper 稳定、异 pepper 不同。"""
    d1 = digest_value("nxt_abc", PEPPER)
    assert len(d1) == 64
    assert int(d1, 16) >= 0  # 合法 hex
    assert d1 == digest_value("nxt_abc", PEPPER)
    assert d1 != digest_value("nxt_abd", PEPPER)
    assert d1 != digest_value("nxt_abc", bytes.fromhex("02" * 32))
    # digest 不包含明文。
    assert "abc" not in d1


def test_csrf_for_session_bound_and_recomputable() -> None:
    """CSRF = HMAC(pepper, 'csrf:'+session_id.bytes)：session-bound、可重算。"""
    sid_a = bytes.fromhex("aa" * 16)
    sid_b = bytes.fromhex("bb" * 16)
    t = csrf_for_session(sid_a, PEPPER)
    assert len(t) == 64
    assert t == csrf_for_session(sid_a, PEPPER)
    assert t != csrf_for_session(sid_b, PEPPER)
    assert t != csrf_for_session(sid_a, bytes.fromhex("03" * 32))


def test_pepper_env_priority_over_file(monkeypatch, tmp_path: Path) -> None:
    """env 优先于文件：env 值生效且文件内容不被采用。"""
    monkeypatch.setenv("NEXUS_AUTH_PEPPER", "ab" * 32)
    provider = PepperProvider(tmp_path / "secrets")
    assert provider.get() == bytes.fromhex("ab" * 32)
    # 文件即便存在也不覆盖 env。
    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir(parents=True, exist_ok=True)
    (secrets_dir / "auth_pepper").write_text("ff" * 32 + "\n", encoding="utf-8")
    assert provider.get() == bytes.fromhex("ab" * 32)


def test_pepper_generates_and_persists_file(monkeypatch, tmp_path: Path) -> None:
    """无 env 且无文件：生成 ≥32 字节 pepper 落盘；再次实例化复用文件。"""
    monkeypatch.delenv("NEXUS_AUTH_PEPPER", raising=False)
    secrets_dir = tmp_path / "secrets"

    p1 = PepperProvider(secrets_dir)
    pepper1 = p1.get()
    assert len(pepper1) >= 32
    file_path = secrets_dir / "auth_pepper"
    assert file_path.exists()

    p2 = PepperProvider(secrets_dir)
    assert p2.get() == pepper1  # 文件持久：重启后同一代次 pepper


def test_pepper_hex_or_utf8_and_length_rejection(monkeypatch, tmp_path: Path) -> None:
    """hex 优先解码；非 hex 长串按 utf-8；长度不足（env 或文件）抛 ValueError。"""
    monkeypatch.delenv("NEXUS_AUTH_PEPPER", raising=False)

    # 非 hex 的 utf-8 长串（32+ 字节）被接受并按字节使用。
    long_ascii = "p" * 40
    monkeypatch.setenv("NEXUS_AUTH_PEPPER", long_ascii)
    assert PepperProvider(tmp_path / "s1").get() == long_ascii.encode("utf-8")

    # env 短串拒绝。
    monkeypatch.setenv("NEXUS_AUTH_PEPPER", "tooshort")
    with pytest.raises(ValueError):
        PepperProvider(tmp_path / "s2").get()

    # 文件内容短拒绝。
    monkeypatch.delenv("NEXUS_AUTH_PEPPER", raising=False)
    secrets = tmp_path / "s3"
    secrets.mkdir()
    (secrets / "auth_pepper").write_text("aa\n", encoding="utf-8")
    with pytest.raises(ValueError):
        PepperProvider(secrets).get()


def test_pepper_not_written_to_logs(tmp_path: Path, monkeypatch, caplog) -> None:
    """pepper 生成日志只含路径，不含 pepper 值本身（§5.9.3/ADR-1）。"""
    monkeypatch.delenv("NEXUS_AUTH_PEPPER", raising=False)
    import logging

    with caplog.at_level(logging.INFO, logger="bootstrap.auth.crypto"):
        provider = PepperProvider(tmp_path / "secret-dir")
        pepper = provider.get()
    messages = "\n".join(r.message for r in caplog.records)
    assert "已生成并写入" in messages
    assert "勿纳入普通备份/日志" in messages
    assert pepper.hex() not in messages  # 日志不含 pepper 值