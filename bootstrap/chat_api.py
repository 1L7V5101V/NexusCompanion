from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import logging
import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from urllib.parse import quote

from infra.channels.web_chat_protocol import CLOSE_DEV_ONLY

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from agent.config_models import AttachmentConfig
    from bootstrap.auth.runtime import AuthRuntime
    from infra.channels.web_chat_channel import WebChatChannel

# 回环集合：dev-only 门禁在绑定层与运行期都以此判定。
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost", "testclient"})


def is_loopback_host(host: str) -> bool:
    """地址是否属于回环集合（含整个 IPv4 回环网段 127.0.0.0/8）。"""
    value = (host or "").strip().strip("[]").lower()
    if value in _LOOPBACK_HOSTS:
        return True
    return value.startswith("127.")


def _client_host(request: Request | WebSocket) -> str:
    client = request.client
    return getattr(client, "host", "") or ""


class _DevOnlyGuardMiddleware:
    """纯 ASGI dev-only 门禁：非回环客户端在进入应用前被拒绝。

    刻意不用 ``BaseHTTPMiddleware`` / ``app.middleware("websocket")``（本环境
    Starlette 对 websocket 中间件的处理会与 HTTP dispatch 混用），直接按 ASGI
    scope 判定：HTTP 返回 403 JSON，WebSocket 以 ``CLOSE_DEV_ONLY`` 关闭。
    这样静态挂载点（``/assets``）也一并受门禁覆盖。
    """

    def __init__(self, app: Any, *, allow_public_bind: bool) -> None:
        self._app = app
        self._allow_public_bind = allow_public_bind

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        if self._allow_public_bind or scope.get("type") not in {"http", "websocket"}:
            await self._app(scope, receive, send)
            return
        client = scope.get("client") or ()
        host = str(client[0]) if client else ""
        if is_loopback_host(host):
            await self._app(scope, receive, send)
            return
        if scope["type"] == "websocket":
            await send(
                {
                    "type": "websocket.close",
                    "code": CLOSE_DEV_ONLY,
                    "reason": "dev-only",
                }
            )
            return
        payload = b'{"detail":"WebChat is dev-only; non-loopback client rejected"}'
        await send(
            {
                "type": "http.response.start",
                "status": 403,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


def create_chat_app(
    *,
    workspace: Path,
    channel: WebChatChannel,
    auth_runtime: "AuthRuntime | None" = None,
    allow_public_bind: bool = False,
    static_root: Path | None = None,
    durable_runtime: Any = None,
    attachment_config: "AttachmentConfig | None" = None,
    source_breakdown_provider: Any = None,
    telegram_binding: Any = None,
) -> FastAPI:
    app = FastAPI(title="Nexus Chat API")
    app.state.workspace = workspace
    app.state.channel = channel
    app.state.durable_runtime = durable_runtime
    # C6: attachment 服务（上传/媒体 BREAKING 契约）。durable_runtime 为 None（无
    # PG durable 控制面）时 uploads/media 返回 503（fail-closed：不落 /tmp、不写
    # 单用户路径）；auth 未启用时多媒体不可用（dev 模式下端点不存在即回退）。
    if durable_runtime is not None and attachment_config is not None:
        from bootstrap.attachments.blob_store import AttachmentBlobStore
        from bootstrap.attachments.service import AttachmentService
        from bootstrap.attachments.telemetry import build_default_attachment_telemetry
        from bootstrap.db.repository.attachment_repo import AttachmentRepository

        # 本服务只在 PG durable 模式装配，因此 blob 必落租户命名空间（与 C7
        # TenantPathResolver 的 attachments_root 同一棵树），不给跨租户共用扁平根留路。
        blob_store = AttachmentBlobStore(workspace, multi_tenant=True)
        service = AttachmentService(
            AttachmentRepository(durable_runtime.session_factory),
            blob_store,
            attachment_config,
            telemetry=build_default_attachment_telemetry(),
        )
        app.state.attachment_service = service
    # C4 dev-only 门禁第 3 层（design ADR-3）：默认只接受回环客户端；反向代理后
    # 也不能用公网来源冒充本机。显式 allow_public_bind 才关闭本层。
    # 与 C5 auth 并存（两者都 fail-closed，取交集）：dev 门禁约束来源网络，
    # auth 约束主体/session，任一不满足都拒绝。
    app.add_middleware(_DevOnlyGuardMiddleware, allow_public_bind=allow_public_bind)
    if auth_runtime is not None:
        # C5: auth.enabled 时挂用户面 `/api/auth/*`（design ADR-7）；默认关闭时
        # 行为与 P0.5 完全一致（不挂路由、WS 不校验）。
        from bootstrap.auth import build_auth_api

        app.include_router(build_auth_api(auth_runtime))
        app.state.auth_runtime = auth_runtime

    async def _require_user_session(request: Request) -> dict[str, Any]:
        """用户面 HTTP 端点的凭据门禁（auth 启用时生效）。

        与 WS 握手使用同一套语义（Cookie + session 有效性 + 403 主体禁用），
        使「HTTP 与 WebSocket 统一认证」成立；未启用认证时不引入门禁（dev 回退）。
        返回 session dict（durable 重建的属主派生用）。
        """
        if auth_runtime is None:
            return {}
        from bootstrap.auth.service import cookie_name
        from bootstrap.auth.ws_guard import ws_session_cookie
        from bootstrap.db.repository.auth_repo import (
            SessionForbiddenError,
            SessionInvalidError,
        )

        raw = ws_session_cookie(
            request.headers,
            cookie_name(admin=False, secure=auth_runtime.config.cookie_secure),
        )
        if not raw:
            raise HTTPException(401, detail="authentication required")
        try:
            return await auth_runtime.auth.validate_user_session(raw)
        except SessionInvalidError:
            raise HTTPException(401, detail="authentication required") from None
        except SessionForbiddenError:
            raise HTTPException(403, detail="forbidden") from None

    # 生产从仓库根的 static/chat 提供（镜像里 /app/static/chat）；测试可传
    # static_root 隔离，避免被本地已构建的 bundle 影响（否则 / 会返回 HTML
    # 而非无 bundle 时的 JSON 回退）。
    project_root = Path(__file__).resolve().parent.parent
    static_dir = (
        static_root if static_root is not None else project_root / "static" / "chat"
    )
    index_file = static_dir / "index.html"
    static_dir.mkdir(parents=True, exist_ok=True)
    app.mount(
        "/assets",
        StaticFiles(directory=static_dir, check_dir=False),
        name="chat_assets",
    )

    @app.get("/", response_model=None)
    def chat_index() -> FileResponse | dict[str, str]:
        if index_file.exists():
            return FileResponse(index_file)
        return {"status": "ok", "channel": channel.name}

    @app.get("/api/chat/sessions", dependencies=[Depends(_require_user_session)])
    def list_sessions(
        page: int = Query(1), page_size: int = Query(50)
    ) -> dict[str, Any]:
        ctx = channel._require_ctx()
        items, total = ctx.session_manager._store.list_sessions_for_dashboard(
            channel=channel.name,
            page=page,
            page_size=page_size,
        )
        visible = [
            item
            for item in items
            if str(item.get("first_message_content") or "").strip()
        ]
        return {"items": visible, "total": len(visible)}

    @app.get(
        "/api/chat/sessions/{session_key:path}/messages",
        dependencies=[Depends(_require_user_session)],
    )
    async def list_messages(
        session: dict[str, Any] = Depends(_require_user_session),
        session_key: str = "",
        page: int = Query(1),
        page_size: int = Query(50),
        sort_by: str = Query("seq"),
        sort_order: str = Query("asc"),
    ) -> dict[str, Any]:
        # durable 重建分支（pg-durable-sot-cutover task 5.2）：canonical 流为
        # 权威；tenant 由已认证 session 派生（§5.9.1），URL 的 session_key 只做
        # 归属匹配，不参与授权。
        if (
            durable_runtime is not None
            and auth_runtime is not None
            and session_key.startswith("chat:")
        ):
            from bootstrap.db.repository.canonical_repo import (
                CanonicalIdentityRepository,
                CanonicalMessageRepository,
            )

            account_id = session.get("account_id")
            conversations = (
                await CanonicalIdentityRepository(durable_runtime.session_factory)
                .list_conversations_by_account(account_id)
                if account_id is not None
                else []
            )
            requested_tenant = session_key[len("chat:") :]
            owned = [
                conv
                for conv in conversations
                if str(conv["tenant_id"]) == requested_tenant
            ]
            if not owned:
                raise HTTPException(404, detail="session not found")
            conv = owned[0]
            tenant_id = str(conv["tenant_id"])
            messages = await CanonicalMessageRepository(
                durable_runtime.session_factory
            ).fetch_messages(tenant_id, conv["id"])
            items = [
                {
                    "seq": int(m["sequence"]),
                    "role": str(m["role"]),
                    "content": str(m["content"] or ""),
                    "thinking": (m.get("metadata") or {}).get("thinking"),
                    "created_at": m.get("created_at") or None,
                }
                for m in messages
                if str(m["role"]) in ("user", "assistant")
            ]
            reverse = str(sort_order).lower() == "desc"
            items.sort(key=lambda item: item["seq"], reverse=reverse)
            safe_page = max(1, int(page))
            safe_size = max(1, min(int(page_size), 200))
            start = (safe_page - 1) * safe_size
            window = items[start : start + safe_size]
            return {"items": window, "total": len(items)}
        ctx = channel._require_ctx()
        items, total = ctx.session_manager._store.list_messages_for_dashboard(
            session_key=session_key,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        return {"items": items, "total": total}

    @app.get(
        "/api/persona/source-breakdown",
        dependencies=[Depends(_require_user_session)],
    )
    async def persona_source_breakdown(
        session: dict[str, Any] = Depends(_require_user_session),
    ) -> dict[str, Any]:
        """prompt source breakdown（c9-persona-relationship ADR-6）。

        仅 admin/debug 开放：dev 模式（auth 未启用）即调试面直接可用；
        认证模式下普通用户一律 404（不泄露能力存在性），admin principal
        放行。内容只有区块 metadata（label/chars/tokens/is_static），
        不含 prompt 正文，更不含模型隐藏推理。
        """
        if session.get("principal_type") != "admin" and auth_runtime is not None:
            raise HTTPException(404, detail="not found")
        provider = source_breakdown_provider
        if provider is None:
            raise HTTPException(404, detail="not found")
        breakdown = provider() or []
        return {
            "items": [
                {
                    "name": getattr(item, "name", ""),
                    "chars": int(getattr(item, "chars", 0) or 0),
                    "est_tokens": int(getattr(item, "est_tokens", 0) or 0),
                    "is_static": bool(getattr(item, "is_static", False)),
                    "cache_hit": bool(getattr(item, "cache_hit", False)),
                }
                for item in breakdown
            ]
        }

    @app.websocket("/ws")
    async def chat_ws(websocket: WebSocket) -> None:
        identity = None
        if auth_runtime is not None:
            # C5（design ADR-4）：auth.enabled 时 WS handshake 校验 Cookie + Origin。
            from bootstrap.auth import WSHandshakeRejected, check_ws_handshake

            try:
                session = await check_ws_handshake(auth_runtime, websocket.headers)
            except WSHandshakeRejected:
                await websocket.close(code=4401, reason="authentication required")
                return
            # 凭据有效 → 按该 session 派生服务端身份三元组（§5.9.1）。派生失败
            # fail-closed：拒绝连接而不是回落到 dev 默认租户（design ADR-6）。
            from bootstrap.auth import WebChatIdentityError, resolve_webchat_identity

            try:
                identity = await resolve_webchat_identity(auth_runtime, session)
            except WebChatIdentityError:
                await websocket.close(code=4403, reason="no canonical identity")
                return
            # C9 ADR-3：PG durable 模式下未完成 onboarding 的 tenant 不放行收发
            # （先完成一次性人设设置；dev 路径 durable 为 None 不受影响）。
            if durable_runtime is not None:
                from bootstrap.db.repository.persona_repo import PersonaRepository

                if not await PersonaRepository(
                    durable_runtime.session_factory
                ).has_profile(str(identity.tenant_id)):
                    await websocket.close(
                        code=4403, reason="persona_onboarding_required"
                    )
                    return
        await channel.handle_websocket(websocket, identity=identity)

    # ── C9 persona onboarding（仅 PG durable + auth 模式装配；dev 路径不存在
    #    这些端点，行为不变）。语义见 c9-persona-relationship design ADR-3：
    #    一次性提交 + 用户侧无任何修改入口（PATCH/PUT/DELETE 无路由 → 404）。
    _user_content_deps = _require_user_session
    if durable_runtime is not None and auth_runtime is not None:
        from bootstrap.db.repository.persona_repo import (
            OnboardingAlreadyCompletedError,
            PersonaRepository,
        )

        _PERSONA_TEXT_MAX = 20_000

        def _persona_repo() -> "PersonaRepository":
            return PersonaRepository(durable_runtime.session_factory)

        async def _require_onboarded(request: Request) -> dict[str, Any]:
            """用户内容面 onboarding 门禁（uploads/media/WS）：已完成人设设置才放行。

            dev 路径（durable/auth 缺一）不装配本依赖。未 onboarding → 403 +
            机器可读码 ``persona_onboarding_required``。
            """
            session = await _require_user_session(request)
            identity = await _resolve_endpoint_identity(auth_runtime, session)
            if identity is None:
                raise HTTPException(403, detail="forbidden")
            if not await _persona_repo().has_profile(str(identity.tenant_id)):
                raise HTTPException(403, detail="persona_onboarding_required")
            return session

        # PG+auth 模式：用户内容面切到 onboarding 门禁（覆盖默认 session 门禁）。
        _user_content_deps = _require_onboarded

        @app.get(
            "/api/persona/status",
            dependencies=[Depends(_require_user_session)],
        )
        async def persona_status(
            session: dict[str, Any] = Depends(_require_user_session),
        ) -> dict[str, Any]:
            identity = await _resolve_endpoint_identity(auth_runtime, session)
            if identity is None:
                raise HTTPException(403, detail="forbidden")
            repo = _persona_repo()
            onboarded = await repo.has_profile(str(identity.tenant_id))
            templates = [
                {"id": t["id"], "name": t["name"]}
                for t in await repo.list_templates(enabled_only=True)
            ]
            return {
                "onboarding_required": not onboarded,
                "templates": templates,
            }

        @app.get(
            "/api/persona/templates",
            dependencies=[Depends(_require_user_session)],
        )
        async def persona_templates(
            session: dict[str, Any] = Depends(_require_user_session),
        ) -> dict[str, Any]:
            """启用模板目录（正文仅在 onboarding 未完成时可见——提交后无需再暴露）。"""
            identity = await _resolve_endpoint_identity(auth_runtime, session)
            if identity is None:
                raise HTTPException(403, detail="forbidden")
            repo = _persona_repo()
            if await repo.has_profile(str(identity.tenant_id)):
                raise HTTPException(404, detail="not found")
            templates = await repo.list_templates(enabled_only=True)
            return {
                "items": [
                    {
                        "id": t["id"],
                        "name": t["name"],
                        "identity": t["identity"],
                        "personality_rules": t["personality_rules"],
                        "self_model": t["self_model"],
                    }
                    for t in templates
                ]
            }

        @app.post("/api/persona/onboarding", status_code=201)
        async def persona_onboarding(
            body: _OnboardingBody,
            session: dict[str, Any] = Depends(_require_user_session),
        ) -> dict[str, Any]:
            """一次性人设设置提交（原子：profile 抢位 + RelationshipState 种子 + 审计）。"""
            identity = await _resolve_endpoint_identity(auth_runtime, session)
            if identity is None:
                raise HTTPException(403, detail="forbidden")
            repo = _persona_repo()
            identity_text = body.identity
            rules_text = body.personality_rules
            self_text = body.self_model
            template_id: str | None = None
            if body.source == "template":
                if not body.template_id:
                    raise HTTPException(400, detail="template_id required")
                tpl = await repo.get_template(body.template_id)
                if tpl is None or not tpl["enabled"]:
                    raise HTTPException(404, detail="template not found")
                template_id = tpl["id"]
                identity_text = tpl["identity"]
                rules_text = tpl["personality_rules"]
                self_text = tpl["self_model"]
            else:
                if not (identity_text and rules_text and self_text):
                    raise HTTPException(
                        400, detail="identity/personality_rules/self_model required"
                    )
            for name, value in (
                ("identity", identity_text),
                ("personality_rules", rules_text),
                ("self_model", self_text),
            ):
                if len(value) > _PERSONA_TEXT_MAX:
                    raise HTTPException(413, detail=f"{name}_too_large")
            try:
                profile = await repo.submit_onboarding(
                    tenant_id=str(identity.tenant_id),
                    source=body.source,
                    identity=identity_text,
                    personality_rules=rules_text,
                    self_model=self_text,
                    template_id=template_id,
                )
            except OnboardingAlreadyCompletedError:
                raise HTTPException(
                    409, detail="persona_onboarding_already_completed"
                ) from None
            return {
                "status": "completed",
                "source": profile["source"],
            }

        # 用户内容面依赖（uploads/media/WS 共用）：PG+auth 模式走 onboarding 门禁，
        # dev 模式维持原 _require_user_session（行为不变）。

    # ── C10 Telegram binding 用户面（pilot_identity_binding 开启时由 app 注入
    #    telegram_binding；关闭时不挂路由，行为与现状一致）。绑定码是账号凭据：
    #    明文只在签发响应出现一次，digest 不落 API/日志（ADR-2）。──
    if telegram_binding is not None:
        from bootstrap.db.repository.telegram_repo import (
            TelegramCodeLimitError,
        )
        from bootstrap.telegram_binding import TelegramAccountNotReadyError

        class _IssueCodeBody(BaseModel):
            note: str = Field(default="", max_length=255)

        @app.get("/api/telegram/binding", dependencies=[Depends(_require_user_session)])
        async def telegram_binding_status(
            session: dict[str, Any] = Depends(_require_user_session),
        ) -> dict[str, Any]:
            identity = await _resolve_endpoint_identity(auth_runtime, session)
            if identity is None:
                raise HTTPException(403, detail="forbidden")
            status = await telegram_binding.binding_status_for_account(
                str(identity.account_id)
            )
            return status

        @app.post("/api/telegram/binding-codes", status_code=201)
        async def issue_telegram_binding_code(
            body: _IssueCodeBody,
            session: dict[str, Any] = Depends(_require_user_session),
        ) -> dict[str, Any]:
            identity = await _resolve_endpoint_identity(auth_runtime, session)
            if identity is None:
                raise HTTPException(403, detail="forbidden")
            try:
                issued = await telegram_binding.issue_code(
                    account_id=str(identity.account_id),
                    issued_by=f"user:{str(identity.account_id)[:8]}",
                    note=body.note,
                )
            except TelegramCodeLimitError:
                raise HTTPException(
                    429, detail="too many open binding codes"
                ) from None
            except TelegramAccountNotReadyError as exc:
                raise HTTPException(409, detail=str(exc)) from None
            return {
                "code": issued["code"],
                "expires_at": issued["expires_at"],
                "tenant_id": issued["tenant_id"],
                "note": issued["note"],
            }

    @app.post("/api/chat/uploads", dependencies=[Depends(_user_content_deps)])
    async def upload_file(
        request: Request,
        session: dict[str, Any] = Depends(_require_user_session),
        filename: str = Query(default="upload.bin"),
    ) -> dict[str, str]:
        """上传（C6 BREAKING 契约）：返回 ``{"attachment_id", "url"}``。

        成功响应绝不包含本地路径/storage_key；旧 `save_upload` 的本地 path 回显
        已移除。auth+PG durable 模式才可用；否则 503（fail-closed，不落 /tmp）。
        """
        service = getattr(app.state, "attachment_service", None)
        if service is None:
            raise HTTPException(503, detail="attachments unavailable")
        identity = await _resolve_endpoint_identity(auth_runtime, session)
        if identity is None:
            raise HTTPException(403, detail="forbidden")
        # ADR-7：先按 Content-Length 快速拒绝超限，避免为注定失败的上传读满 body。
        declared = request.headers.get("content-length")
        if declared:
            try:
                if int(declared) > service.config.max_file_bytes:
                    raise HTTPException(413, detail="upload_too_large")
            except ValueError:
                raise HTTPException(400, detail="invalid content-length") from None
        data = await request.body()
        if not data:
            raise HTTPException(status_code=400, detail="上传内容不能为空")
        clean_name = Path(filename).name or "upload.bin"
        from bootstrap.attachments.validation import AttachmentError

        try:
            result = await service.upload(
                account_id=identity.account_id,
                tenant_id=identity.tenant_id,
                filename=clean_name,
                data=data,
            )
        except AttachmentError as exc:
            code = getattr(exc, "code", "upload_error")
            status = _upload_error_status(code)
            raise HTTPException(status_code=status, detail=code) from None
        except Exception:
            logger.exception("attachment upload 失败")
            raise HTTPException(status_code=500, detail="upload failed") from None
        # upload.finished 由 service 内部记录点 emit（C12 §8.1）；此处不再重复。
        return {"attachment_id": result.attachment_id, "url": result.url}

    @app.get("/api/chat/media", dependencies=[Depends(_user_content_deps)])
    async def read_media(
        session: dict[str, Any] = Depends(_require_user_session),
        attachment_id: str | None = Query(default=None),
        path: str | None = Query(default=None),
    ) -> Response:
        """读取（C6 BREAKING）：按 attachment_id + session 派生租户归属。

        客户端可控的旧 `path` 参数已移除：携带 `path` 直接 400（拒绝旧寻址）；
        跨租户/不存在 → 404（不泄露存在性）；blob 缺失 → 404 + missing 标记。
        """
        if path is not None:
            raise HTTPException(400, detail="path parameter removed")
        if not attachment_id:
            raise HTTPException(422, detail="attachment_id required")
        service = getattr(app.state, "attachment_service", None)
        if service is None:
            raise HTTPException(503, detail="attachments unavailable")
        identity = await _resolve_endpoint_identity(auth_runtime, session)
        if identity is None:
            raise HTTPException(403, detail="forbidden")
        try:
            record, blob_path = await service.fetch(
                account_id=identity.account_id,
                tenant_id=identity.tenant_id,
                attachment_id=attachment_id,
            )
        except Exception:
            raise HTTPException(404, detail="file not found") from None
        # fetch.finished 由 service 内部记录点 emit（C12 §8.1）；此处不再重复。
        # FileResponse 流式回传（ADR-7）：20 MiB 附件不再整体读入内存。
        display = record.filename_display or f"attachment{record.server_ext}"
        return FileResponse(
            blob_path,
            media_type=record.detected_mime,
            headers={"Content-Disposition": f"inline; filename*=utf-8''{quote(display)}"},
        )
    return app


def build_chat_server(
    *,
    workspace: Path,
    channel: "WebChatChannel",
    dev_mode: bool,
    host: str = "127.0.0.1",
    port: int = 6322,
    auth_runtime: "AuthRuntime | None" = None,
    allow_public_bind: bool = False,
    durable_runtime: Any = None,
    attachment_config: "AttachmentConfig | None" = None,
    source_breakdown_provider: Any = None,
    telegram_binding: Any = None,
) -> uvicorn.Server:
    """构造 WebChat 服务器（design ADR-3 三态门禁）。

    放行条件：已装配 ``auth_runtime``（auth.enabled）**或** ``dev_mode``。
    两者皆无则 fail-fast——未启用认证时只能走 dev 回退，不得静默放行。
    """
    if auth_runtime is None and not dev_mode:
        raise RuntimeError(
            "WebChat 通道未启用 [auth] 时要求 agent.dev_mode=true（P0.5 dev-only 回退）；"
            "启用认证后由 session 派生身份，不再要求 dev_mode。"
        )
    if not allow_public_bind and not is_loopback_host(host):
        raise RuntimeError(
            f"WebChat dev 模式拒绝绑定非回环地址 {host!r}；"
            "如确需非本机调试请显式设置 [channels.chat].allow_public_bind=true"
            "（P1 前禁止公网暴露）。"
        )
    config = uvicorn.Config(
        create_chat_app(
            workspace=workspace,
            channel=channel,
            auth_runtime=auth_runtime,
            allow_public_bind=allow_public_bind,
            durable_runtime=durable_runtime,
            attachment_config=attachment_config,
            source_breakdown_provider=source_breakdown_provider,
            telegram_binding=telegram_binding,
        ),
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    return uvicorn.Server(config)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        _ = path.relative_to(root)
        return True
    except ValueError:
        return False


class _OnboardingBody(BaseModel):
    """onboarding 提交体（模块级：FastAPI 注解解析需要可全局解析的名字）。"""

    source: str = Field(pattern="^(template|custom)$")
    template_id: str | None = None
    identity: str = ""
    personality_rules: str = ""
    self_model: str = ""


async def _resolve_endpoint_identity(
    auth_runtime: "AuthRuntime | None",
    session: dict[str, Any],
) -> Any | None:
    """HTTP 端点身份派生（与 WS 同一派生点，§5.9.1）。

    auth 未启用（dev 模式）返回 None（组件不可用语义由调用方映射为 503/403）；
    auth 启用但无法派生（无账号/无 canonical）→ None（fail-closed，不回落 dev
    默认租户）。返回 WebChatIdentity（含 account_id / tenant_id）。
    """
    if auth_runtime is None:
        return None
    from bootstrap.auth import WebChatIdentityError, resolve_webchat_identity

    try:
        return await resolve_webchat_identity(auth_runtime, session)
    except WebChatIdentityError:
        return None


_UPLOAD_ERROR_STATUS: dict[str, int] = {
    "upload_empty": 400,
    "upload_too_large": 413,
    "upload_type_denied": 415,
    "upload_ext_mismatch": 415,
    "upload_pixel_limit": 415,
    "upload_decode_timeout": 415,
    "upload_frames_limit": 415,
    "upload_text_limit": 415,
    "upload_encoding": 415,
}


def _upload_error_status(code: str) -> int:
    return _UPLOAD_ERROR_STATUS.get(code, 415)
