"""C5 auth/admin HTTP API（design.md ADR-3/4/5，PILOT_ROADMAP §5.4/§5.9.3）。

路由：
- ``/api/auth/*``       普通测试用户的 exchange/csrf/logout/me（挂 WebChat Gateway）
- ``/api/admin/*``      管理员 recovery exchange/csrf/测试账号/凭据撤销（挂 Dashboard）

安全契约：
- 401 = 无有效 principal/session 或 token 无效/已消费/已过期。统一错误体
  ``{"detail": "invalid credentials"}``（兑换）或 ``{"detail": "authentication
  required"}``（session），不区分「不存在 vs 已过期 vs 已撤销」（ADR-3）。
- 403 = principal 有效但被禁止：账号 suspended/revoked、CSRF/Origin 失败、
  admin route 非回环来源。
- mutation 依序校验 Origin/Referer → session-bound ``X-CSRF-Token``（ADR-4）。
  ``exchange`` 建立会话**之前**没有 session，故无 CSRF 可绑定，但仍校验 Origin
  （否则登录端点可被跨站发起）；GET/HEAD/OPTIONS 只需有效 session。
- admin HTTP/API 默认只允许部署主机本机来源（``request.client.host`` ∈
  ``admin_allow_ips``），反代/Tunnel 必须不转发 ``/api/admin/*``（ADR-5）。
- Cookie 属性（§5.9.3 冻结）：``HttpOnly``/``Secure``(非 dev)/``Path=/``/无 Domain
  /``SameSite=Lax``；明文 session 值只出现在 Set-Cookie 响应中一次。
"""

from __future__ import annotations

import hmac
import uuid
from http.cookies import CookieError, SimpleCookie
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from bootstrap.auth.runtime import AuthRuntime
from bootstrap.auth.service import cookie_name
from bootstrap.db.repository.auth_repo import (
    CredentialExchangeError,
    SessionForbiddenError,
    SessionInvalidError,
)
from bootstrap.db.repository.provisioning_repo import ProvisioningStateError

__all__ = [
    "CreateAccountRequest",
    "ExchangeRequest",
    "build_admin_api",
    "build_auth_api",
]

_HTTP_401_AUTH_REQUIRED = "authentication required"
_HTTP_401_INVALID = "invalid credentials"
_HTTP_403 = "forbidden"

# 排除 _HTTPS_PROBE 变量名（pyright 无要求，仅为可读性分组）
_pr = _HTTP_401_AUTH_REQUIRED


class ExchangeRequest(BaseModel):
    token: str = Field(min_length=1, max_length=300)


class CreateAccountRequest(BaseModel):
    display_name: str = Field(default="", max_length=200)


def build_auth_api(runtime: AuthRuntime) -> APIRouter:
    """普通测试用户面：``/api/auth/*``（挂 WebChat Gateway）。"""
    router = APIRouter()
    cfg = runtime.config
    secure = cfg.cookie_secure
    user_cookie = cookie_name(admin=False, secure=secure)

    class _Session:
        def __init__(self, session: dict, raw_cookie: str) -> None:
            self.session = session
            self.raw_cookie = raw_cookie

    async def _user_session(request: Request) -> _Session:
        raw = _get_cookie_value(request.headers.get("cookie", ""), user_cookie)
        if not raw:
            raise HTTPException(401, detail=_pr)
        try:
            return _Session(
                session=await runtime.auth.validate_user_session(raw), raw_cookie=raw
            )
        except SessionInvalidError:
            raise HTTPException(401, detail=_pr) from None
        except SessionForbiddenError:
            raise HTTPException(403, detail=_HTTP_403) from None

    async def _user_mutation(request: Request, ctx: _Session = Depends(_user_session)) -> _Session:
        await _require_csrf(runtime, request, ctx.session["id"])
        return ctx

    @router.post("/api/auth/exchange")
    async def exchange(body: ExchangeRequest, request: Request) -> JSONResponse:
        _require_origin(runtime, request)
        try:
            session, raw_cookie = await runtime.auth.exchange_invitation(
                body.token,
                user_agent=request.headers.get("user-agent", "")[:255],
            )
        except CredentialExchangeError:
            raise HTTPException(401, detail=_HTTP_401_INVALID) from None
        resp = JSONResponse(
            status_code=200,
            content={
                "session_id": str(session["id"]),
                "account_id": str(session["account_id"]),
            },
        )
        _set_cookie(
            resp,
            user_cookie,
            raw_cookie,
            secure=secure,
            max_age=cfg.session_absolute_s,
        )
        return resp

    @router.get("/api/auth/csrf")
    async def csrf(ctx: _Session = Depends(_user_session)) -> dict[str, str]:
        return {"csrf_token": runtime.auth.csrf_token(ctx.session["id"])}

    @router.post("/api/auth/logout")
    async def logout(ctx: _Session = Depends(_user_mutation)) -> JSONResponse:
        if ctx.raw_cookie:
            await runtime.auth.logout(ctx.raw_cookie)
        resp = JSONResponse(status_code=200, content={"status": "ok"})
        _clear_cookie(resp, user_cookie, secure=secure)
        return resp

    @router.get("/api/auth/me")
    async def me(ctx: _Session = Depends(_user_session)) -> dict[str, Any]:
        account = await runtime.provisioning.get_account(ctx.session["account_id"])
        return {
            "account_id": str(ctx.session["account_id"]),
            "display_name": (account or {}).get("display_name", ""),
            "status": (account or {}).get("status", "active"),
        }

    return router


def build_admin_api(runtime: AuthRuntime) -> APIRouter:
    """管理员面：``/api/admin/*``（挂 Dashboard；回环边界 ADR-5）。"""
    router = APIRouter()
    cfg = runtime.config
    secure = cfg.cookie_secure
    admin_cookie = cookie_name(admin=True, secure=secure)

    class _AdminSession:
        def __init__(self, session: dict, raw_cookie: str) -> None:
            self.session = session
            self.raw_cookie = raw_cookie

    async def _loopback(request: Request) -> None:
        host = request.client.host if request.client else ""
        if host not in cfg.admin_allow_ips:
            raise HTTPException(403, detail=_HTTP_403)

    async def _admin_session(request: Request, _: None = Depends(_loopback)) -> _AdminSession:
        raw = _get_cookie_value(request.headers.get("cookie", ""), admin_cookie)
        if not raw:
            raise HTTPException(401, detail=_pr)
        try:
            return _AdminSession(
                session=await runtime.admin.validate_admin_session(raw), raw_cookie=raw
            )
        except SessionInvalidError:
            raise HTTPException(401, detail=_pr) from None
        except SessionForbiddenError:
            raise HTTPException(403, detail=_HTTP_403) from None

    async def _admin_mutation(request: Request, ctx: _AdminSession = Depends(_admin_session)) -> _AdminSession:
        await _require_csrf(runtime, request, ctx.session["id"])
        return ctx

    @router.post("/api/admin/auth/exchange")
    async def exchange(body: ExchangeRequest, request: Request, _: None = Depends(_loopback)) -> JSONResponse:
        _require_origin(runtime, request)
        try:
            session, raw_cookie = await runtime.admin.exchange_recovery(
                body.token,
                user_agent=request.headers.get("user-agent", "")[:255],
            )
        except CredentialExchangeError:
            raise HTTPException(401, detail=_HTTP_401_INVALID) from None
        resp = JSONResponse(status_code=200, content={"session_id": str(session["id"])})
        _set_cookie(
            resp,
            admin_cookie,
            raw_cookie,
            secure=secure,
            max_age=cfg.admin_absolute_s,
        )
        return resp

    @router.get("/api/admin/auth/csrf")
    async def csrf(ctx: _AdminSession = Depends(_admin_session)) -> dict[str, str]:
        return {"csrf_token": runtime.auth.csrf_token(ctx.session["id"])}

    @router.post("/api/admin/test-accounts")
    async def create_account(
        body: CreateAccountRequest, ctx: _AdminSession = Depends(_admin_mutation)
    ) -> dict[str, Any]:
        created = await runtime.provisioning.create_account(display_name=body.display_name)
        job_id = created["job"]["id"]
        await runtime.provisioning.run_pending(max_jobs=16)
        account = await runtime.provisioning.get_account(created["account"]["id"])
        if account is not None and account["status"] == "active":
            _, raw_token = await runtime.auth.issue_invitation(
                account["id"], issued_by="admin:api"
            )
            return {"account": account, "job": {"status": "ready"}, "token": raw_token}
        job = await runtime.provisioning.get_job(job_id)
        return {"account": account or created["account"], "job": job}

    @router.get("/api/admin/test-accounts")
    async def list_accounts(_: _AdminSession = Depends(_admin_session)) -> dict[str, Any]:
        return {"items": await runtime.provisioning.list_accounts()}

    @router.post("/api/admin/test-accounts/{account_id}/suspend")
    async def suspend_account(
        account_id: str, _: _AdminSession = Depends(_admin_mutation)
    ) -> dict[str, str]:
        try:
            await runtime.provisioning.suspend_account(account_id, reason="admin:suspend")
        except ProvisioningStateError:
            raise HTTPException(409, detail="invalid account state") from None
        return {"status": "suspended"}

    @router.post("/api/admin/test-accounts/{account_id}/unsuspend")
    async def unsuspend_account(
        account_id: str, _: _AdminSession = Depends(_admin_mutation)
    ) -> dict[str, str]:
        try:
            await runtime.provisioning.unsuspend_account(account_id)
        except ProvisioningStateError:
            raise HTTPException(409, detail="invalid account state") from None
        return {"status": "active"}

    @router.post("/api/admin/test-accounts/{account_id}/revoke")
    async def revoke_account(
        account_id: str, _: _AdminSession = Depends(_admin_mutation)
    ) -> dict[str, str]:
        try:
            await runtime.provisioning.revoke_account(account_id, reason="admin:revoke")
        except ProvisioningStateError:
            raise HTTPException(409, detail="invalid account state") from None
        return {"status": "revoked"}

    @router.post("/api/admin/tokens/{token_id}/revoke")
    async def revoke_token(
        token_id: str, _: _AdminSession = Depends(_admin_mutation)
    ) -> dict[str, str]:
        # 幂等 no-op：不存在/已撤销也返回成功，避免泄露 Token 是否存在（ADR-3）。
        await runtime.auth.revoke_token(token_id, reason="admin:revoke_token")
        return {"status": "revoked"}

    return router


def _require_origin(runtime: AuthRuntime, request: Request) -> None:
    """校验 Origin/Referer 命中 allowlist（ADR-4 第 1 步）。

    对**全部** mutation 生效，包括尚未建立 session 的 ``exchange``：那时没有
    session-bound CSRF 可校验（CSRF 由 session id 派生），但 Origin 仍适用，
    否则登录端点可被跨站发起（login CSRF）。
    """
    if _request_origin(request) not in runtime.config.origin_allowlist:
        raise HTTPException(403, detail=_HTTP_403)


async def _require_csrf(
    runtime: AuthRuntime, request: Request, session_id: uuid.UUID | str
) -> None:
    """mutation 依序校验 Origin/Referer → session-bound CSRF（ADR-4）。"""
    _require_origin(runtime, request)
    sent = request.headers.get("x-csrf-token", "")
    expected = runtime.auth.csrf_token(session_id)
    if not hmac.compare_digest(sent, expected):
        raise HTTPException(403, detail=_HTTP_403)


def _get_cookie_value(header: str, name: str) -> str:
    if not header:
        return ""
    try:
        jar = SimpleCookie(header)
    except CookieError:
        return ""
    morsel = jar.get(name)
    return morsel.value if morsel is not None else ""


def _request_origin(request: Request) -> str:
    """Origin 头优先；缺失时从 Referer 解析 origin 部分（ADR-4 第 1 步）。"""
    origin = request.headers.get("origin")
    if origin:
        return origin.strip()
    referer = request.headers.get("referer")
    if not referer:
        return ""
    parts = urlsplit(referer)
    return f"{parts.scheme}://{parts.netloc}"


def _set_cookie(
    response: JSONResponse,
    name: str,
    value: str,
    *,
    secure: bool,
    max_age: int | None = None,
) -> None:
    cookie = f"{name}={value}; Path=/; HttpOnly; SameSite=Lax"
    if secure:
        cookie += "; Secure"
    if max_age is not None:
        cookie += f"; Max-Age={int(max_age)}"
    response.headers.append("Set-Cookie", cookie)


def _clear_cookie(response: JSONResponse, name: str, *, secure: bool) -> None:
    cookie = f"{name}=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0"
    if secure:
        cookie += "; Secure"
    response.headers.append("Set-Cookie", cookie)


def _unused() -> Literal[0]:
    return 0