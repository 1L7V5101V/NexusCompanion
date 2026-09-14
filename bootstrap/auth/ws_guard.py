"""C5 WebSocket handshake guard（design.md ADR-4）。

函数面的最小契约：``check_ws_handshake`` 校验 Cookie（普通用户 session）与
Origin allowlist，供 C4 WebChat 通道在 accept 前调用（ADR-4「WS handshake 校验
Cookie + Origin」）。失败以 :class:`WSHandshakeRejected` 表达；回环边界（ADR-5）
由 HTTP 依赖层负责，本模块只做凭据 + Origin 两项。
"""

from __future__ import annotations

from collections.abc import Mapping
from http.cookies import CookieError, SimpleCookie
from urllib.parse import urlsplit

from bootstrap.auth.runtime import AuthRuntime
from bootstrap.auth.service import cookie_name
from bootstrap.db.repository.auth_repo import (
    SessionForbiddenError,
    SessionInvalidError,
)

__all__ = [
    "WSHandshakeRejected",
    "check_ws_handshake",
    "ws_origin",
    "ws_session_cookie",
]


class WSHandshakeRejected(Exception):
    """WS handshake 失败（无有效 Cookie / Origin 不在 allowlist）。

    调用方（channel 层）将其转换为协议级拒绝：不泄露具体原因，仅区分
    ``auth``（凭据无效）与 ``origin``（来源禁止），与 HTTP 401/403 对齐。
    """

    def __init__(self, *, origin: bool = False) -> None:
        self.origin = origin
        super().__init__("ws handshake rejected" if not origin else "ws origin forbidden")


def ws_session_cookie(headers: Mapping[str, str], cookie_name_value: str) -> str:
    """从 WS headers 解析目标 Cookie。``headers`` 为 header 名→值映射。"""
    raw = headers.get("cookie", "")
    if not raw:
        return ""
    try:
        jar = SimpleCookie(raw)
    except CookieError:
        return ""
    morsel = jar.get(cookie_name_value)
    return morsel.value if morsel is not None else ""


def ws_origin(headers: Mapping[str, str]) -> str:
    """从 WS headers 取 Origin（缺省取 Referer 的 origin 部分，ADR-4 第 1 步）。"""
    origin = headers.get("origin", "")
    if origin:
        return origin.strip()
    referer = headers.get("referer", "")
    if not referer:
        return ""
    parts = urlsplit(referer)
    return f"{parts.scheme}://{parts.netloc}"


async def check_ws_handshake(
    runtime: AuthRuntime,
    headers: Mapping[str, str],
    *,
    admin: bool = False,
) -> dict:
    """校验 WS handshake：Cookie session → Origin allowlist。

    - 普通连接要求有效 user session（``principal_type='user'``）；
      ``admin=True`` 时校验 admin session（web admin 走回环边界）。
    - Origin/Referer 必须命中 ``auth.origin_allowlist``。
    - 失败抛 :class:`WSHandshakeRejected`；成功返回该 session dict（含
      ``id``/``account_id``），供 C4 通道派生 tenant 归属。
    """
    cfg = runtime.config
    cookie = cookie_name(admin=admin, secure=cfg.cookie_secure)
    raw = ws_session_cookie(headers, cookie)
    if not raw:
        raise WSHandshakeRejected()
    try:
        if admin:
            session = await runtime.admin.validate_admin_session(raw)
        else:
            session = await runtime.auth.validate_user_session(raw)
    except (SessionInvalidError, SessionForbiddenError):
        raise WSHandshakeRejected() from None
    origin = ws_origin(headers)
    if origin not in cfg.origin_allowlist:
        raise WSHandshakeRejected(origin=True)
    return session