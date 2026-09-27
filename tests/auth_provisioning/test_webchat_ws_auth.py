"""webchat-auth-wiring 任务 1：`/ws` 握手认证接线与拒绝语义（design ADR-1 / ADR-4）。

不依赖 `starlette.testclient`：本仓 `pytest.ini` 有 `addopts = -W error`，而本地
`.venv` 的 anyio 版本在导入 testclient 时抛第三方 `DeprecationWarning`，会让整个
文件收集失败（既有环境问题，与本次改动无关）。这里直接取 `create_chat_app` 注册的
`/ws` 端点调用，断言真正的接线行为：

- 凭据失败（无 Cookie / 会话无效 / 已撤销 / 账号 suspended）→ `close(4401)`；
- 来源不在 allowlist → 同样 `close(4401)`，且与凭据失败的 close code/reason **逐字一致**
  （不泄露是「没凭据」还是「来源不对」，更不泄露账号状态）；
- 失败时 SHALL NOT 进入 `channel.handle_websocket`（= 零入队）；
- 凭据有效但账号无 canonical conversation → `close(4403)`（fail-closed，不回落默认租户）；
- 凭据与身份都成立 → 进入通道，并传入按 session 派生的身份。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bootstrap.auth.service import AuthConfig, cookie_name
from bootstrap.chat_api import create_chat_app
from bootstrap.db.repository.auth_repo import (
    SessionForbiddenError,
    SessionInvalidError,
)

ORIGIN = "https://nexus.example.com"
CLOSE_UNAUTHORIZED = 4401
CLOSE_NO_IDENTITY = 4403


# ── stubs ────────────────────────────────────────────────────────


class _AuthStub:
    def __init__(self, *, error: Exception | None = None, session: dict[str, Any] | None = None) -> None:
        self._error = error
        self._session = session

    async def validate_user_session(self, raw: str) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return self._session or {"id": "sess-1", "account_id": "acct-a"}


class _CanonicalRepoStub:
    def __init__(self, conversations: list[dict[str, Any]]) -> None:
        self._conversations = conversations

    async def list_conversations_by_account(self, account_id: Any) -> list[dict[str, Any]]:
        return list(self._conversations)


class _RuntimeStub:
    """`check_ws_handshake` 只用 `.config` 与 `.auth`；身份派生用 `.canonical_repo`。"""

    def __init__(
        self,
        *,
        auth: _AuthStub,
        conversations: list[dict[str, Any]] | None = None,
        origin_allowlist: list[str] | None = None,
    ) -> None:
        self.config = AuthConfig(
            enabled=True,
            cookie_secure=True,
            origin_allowlist=origin_allowlist if origin_allowlist is not None else [ORIGIN],
        )
        self.auth = auth
        self.canonical_repo = _CanonicalRepoStub(conversations or [])


class _ChannelStub:
    """进入即代表可能入队——用它做「零入队」断言。"""

    name = "chat"

    def __init__(self) -> None:
        self.identities: list[Any] = []

    async def handle_websocket(self, websocket: Any, *, identity: Any = None) -> None:
        self.identities.append(identity)


class _WSStub:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = headers
        self.accepted = False
        self.closed: tuple[int, str] | None = None

    async def accept(self) -> None:
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = "") -> None:
        self.closed = (code, reason)


# ── helpers ──────────────────────────────────────────────────────

_SECURE_COOKIE = cookie_name(admin=False, secure=True)


def _cookie_header() -> str:
    """构造一个「格式合法」的会话 Cookie；有效性完全由 stub 决定。"""
    return f"{_SECURE_COOKIE}=raw-session-value"


def _endpoint(app: Any) -> Any:
    for route in app.routes:
        if getattr(route, "path", None) == "/ws":
            return route.endpoint
    raise AssertionError("/ws route not registered")


async def _invoke(
    tmp_path: Path,
    runtime: _RuntimeStub,
    headers: dict[str, str],
) -> tuple[_ChannelStub, _WSStub]:
    channel = _ChannelStub()
    app = create_chat_app(workspace=tmp_path, channel=channel, auth_runtime=runtime)  # type: ignore[arg-type]
    ws = _WSStub(headers)
    await _endpoint(app)(ws)
    return channel, ws


def _runtime(error: Exception | None = None, conversations: list[dict[str, Any]] | None = None) -> _RuntimeStub:
    return _RuntimeStub(auth=_AuthStub(error=error), conversations=conversations)


_CONV = [{"id": "conv-a", "tenant_id": "tenant-a", "account_id": "acct-a", "status": "active"}]


# ── 任务 1.1 / 1.3：凭据与来源拒绝 + 零入队 ──────────────────────


@pytest.mark.asyncio
async def test_handshake_rejects_when_cookie_missing(tmp_path: Path) -> None:
    channel, ws = await _invoke(tmp_path, _runtime(conversations=_CONV), {"origin": ORIGIN})

    assert ws.closed is not None and ws.closed[0] == CLOSE_UNAUTHORIZED
    assert channel.identities == []  # 零入队：从未进入通道


@pytest.mark.asyncio
async def test_handshake_rejects_invalid_session(tmp_path: Path) -> None:
    runtime = _RuntimeStub(auth=_AuthStub(error=SessionInvalidError("authentication required")), conversations=_CONV)
    channel, ws = await _invoke(tmp_path, runtime, {"cookie": _cookie_header(), "origin": ORIGIN})

    assert ws.closed is not None and ws.closed[0] == CLOSE_UNAUTHORIZED
    assert channel.identities == []


@pytest.mark.asyncio
async def test_handshake_rejects_suspended_account(tmp_path: Path) -> None:
    """账号 suspended 走 403 语义，但 WS 侧与其余凭据失败**不可区分**。"""
    runtime = _RuntimeStub(auth=_AuthStub(error=SessionForbiddenError("forbidden")), conversations=_CONV)
    channel, ws = await _invoke(tmp_path, runtime, {"cookie": _cookie_header(), "origin": ORIGIN})

    assert ws.closed is not None and ws.closed[0] == CLOSE_UNAUTHORIZED
    assert channel.identities == []


@pytest.mark.asyncio
async def test_handshake_rejects_origin_not_allowed(tmp_path: Path) -> None:
    channel, ws = await _invoke(
        tmp_path,
        _runtime(conversations=_CONV),
        {"cookie": _cookie_header(), "origin": "https://evil.example.com"},
    )

    assert ws.closed is not None and ws.closed[0] == CLOSE_UNAUTHORIZED
    assert channel.identities == []


@pytest.mark.asyncio
async def test_rejections_do_not_leak_which_check_failed(tmp_path: Path) -> None:
    """三种凭据失败 + 来源失败 → close code 与 reason 逐字一致（不泄露原因/账号状态）。"""
    cases = [
        {"origin": ORIGIN},  # 无 Cookie
        {"cookie": _cookie_header(), "origin": ORIGIN},  # 会话无效
        {"cookie": _cookie_header(), "origin": "https://evil.example.com"},  # 来源不匹配
    ]
    runtimes = [
        _runtime(conversations=_CONV),
        _RuntimeStub(auth=_AuthStub(error=SessionInvalidError("x")), conversations=_CONV),
        _runtime(conversations=_CONV),
    ]
    observed: list[tuple[int, str]] = []
    for runtime, headers in zip(runtimes, cases):
        _, ws = await _invoke(tmp_path, runtime, headers)
        assert ws.closed is not None
        observed.append(ws.closed)

    assert len(set(observed)) == 1, f"拒绝语义不一致，泄露了失败原因: {observed}"


# ── 任务 1.2 / 2.1：凭据有效但身份派生失败 → fail-closed ──────────


@pytest.mark.asyncio
async def test_handshake_rejects_when_no_canonical_conversation(tmp_path: Path) -> None:
    channel, ws = await _invoke(
        tmp_path,
        _runtime(conversations=[]),
        {"cookie": _cookie_header(), "origin": ORIGIN},
    )

    assert ws.closed is not None and ws.closed[0] == CLOSE_NO_IDENTITY
    assert channel.identities == []  # 不回落默认租户、不进入通道


# ── 正向：接线把派生身份传进通道 ─────────────────────────────────


@pytest.mark.asyncio
async def test_handshake_passes_derived_identity_into_channel(tmp_path: Path) -> None:
    channel, ws = await _invoke(
        tmp_path,
        _runtime(conversations=_CONV),
        {"cookie": _cookie_header(), "origin": ORIGIN},
    )

    assert ws.closed is None
    assert len(channel.identities) == 1
    identity = channel.identities[0]
    assert identity.account_id == "acct-a"
    assert identity.tenant_id == "tenant-a"
    assert identity.conversation_id == "conv-a"
    # session 路由键按 tenant 区分，不共用 dev 的 chat:local
    assert identity.chat_id == "tenant-a"


@pytest.mark.asyncio
async def test_unauthenticated_instance_does_not_gate_ws(tmp_path: Path) -> None:
    """未装配 auth_runtime（dev 回退）时不引入握手门禁，身份为 None（由通道回退）。"""
    channel = _ChannelStub()
    app = create_chat_app(workspace=tmp_path, channel=channel)  # type: ignore[arg-type]
    ws = _WSStub({})
    await _endpoint(app)(ws)

    assert ws.closed is None
    assert channel.identities == [None]
