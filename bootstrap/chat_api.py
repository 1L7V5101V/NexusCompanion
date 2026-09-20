from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from infra.channels.web_chat_protocol import CLOSE_DEV_ONLY

if TYPE_CHECKING:
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
                {"type": "websocket.close", "code": CLOSE_DEV_ONLY, "reason": "dev-only"}
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
    allow_public_bind: bool = False,
) -> FastAPI:
    app = FastAPI(title="Nexus Chat API")
    app.state.workspace = workspace
    app.state.channel = channel
    # dev-only 门禁第 3 层（design ADR-3）：默认只接受回环客户端；反向代理后
    # 也不能用公网来源冒充本机。显式 allow_public_bind 才关闭本层。
    app.add_middleware(_DevOnlyGuardMiddleware, allow_public_bind=allow_public_bind)
    project_root = Path(__file__).resolve().parent.parent
    static_dir = project_root / "static" / "chat"
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

    @app.get("/api/chat/sessions")
    def list_sessions(page: int = Query(1), page_size: int = Query(50)) -> dict[str, Any]:
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

    @app.get("/api/chat/sessions/{session_key:path}/messages")
    def list_messages(
        session_key: str,
        page: int = Query(1),
        page_size: int = Query(50),
        sort_by: str = Query("seq"),
        sort_order: str = Query("asc"),
    ) -> dict[str, Any]:
        ctx = channel._require_ctx()
        items, total = ctx.session_manager._store.list_messages_for_dashboard(
            session_key=session_key,
            page=page,
            page_size=page_size,
            sort_by=sort_by,
            sort_order=sort_order,
        )
        return {"items": items, "total": total}

    @app.websocket("/ws")
    async def chat_ws(websocket: WebSocket) -> None:
        await channel.handle_websocket(websocket)

    @app.post("/api/chat/uploads")
    async def upload_file(
        request: Request,
        filename: str = Query(default="upload.bin"),
    ) -> dict[str, str]:
        data = await request.body()
        if not data:
            raise HTTPException(status_code=400, detail="上传内容不能为空")
        clean_name = Path(filename).name or "upload.bin"
        return channel.save_upload(data, clean_name)

    @app.get("/api/chat/media")
    def read_media(path: str = Query(...)) -> FileResponse:
        requested = Path(path).expanduser().resolve()
        if not _can_read_media(channel, requested):
            raise HTTPException(status_code=404, detail="文件不存在")
        if not requested.is_file():
            raise HTTPException(status_code=404, detail="文件不存在")
        return FileResponse(requested)

    return app


def build_chat_server(
    *,
    workspace: Path,
    channel: "WebChatChannel",
    dev_mode: bool,
    host: str = "127.0.0.1",
    port: int = 6322,
    allow_public_bind: bool = False,
) -> uvicorn.Server:
    """构造 dev-only WebChat 服务器；非 dev 或非回环绑定直接拒绝（ADR-3）。"""
    if not dev_mode:
        raise RuntimeError(
            "WebChat 通道仅限 agent.dev_mode=true（P0.5 dev-only）；"
            "P1 认证与 tenant 隔离落地前不得公网暴露。"
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
            allow_public_bind=allow_public_bind,
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


def _can_read_media(channel: "WebChatChannel", path: Path) -> bool:
    if any(_is_relative_to(path, root.resolve()) for root in channel.upload_roots()):
        return True
    if channel.has_media(path):
        return True
    try:
        ctx = channel._require_ctx()
    except RuntimeError:
        return False
    store = ctx.session_manager._store
    media_path_exists = getattr(store, "media_path_exists", None)
    if callable(media_path_exists):
        return bool(media_path_exists(path))
    return False
