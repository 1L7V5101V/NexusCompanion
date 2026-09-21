"""C5 auth/provisioning 包：凭据 crypto、服务层、HTTP API 装配与 CLI 共享构件。

公开面（供 chat_api/dashboard_api/AppRuntime/main.py 使用）：
- :func:`create_auth_runtime` — 按 ``config.storage.postgres_url`` 装配服务单例
- :func:`build_auth_api` / :func:`build_admin_api` — WebChat 用户面与 Dashboard 管理面路由
- :func:`check_ws_handshake` — WS handshake（Cookie + Origin）校验函数
- :func:`resolve_webchat_identity` — 已认证 session → WebChat 服务端派生身份（§5.9.1）
"""

from bootstrap.auth.api import build_admin_api, build_auth_api
from bootstrap.auth.identity import (
    WebChatIdentityError,
    resolve_webchat_identity,
)
from bootstrap.auth.runtime import AuthRuntime, create_auth_runtime
from bootstrap.auth.ws_guard import (
    WSHandshakeRejected,
    check_ws_handshake,
)

__all__ = [
    "AuthRuntime",
    "WSHandshakeRejected",
    "WebChatIdentityError",
    "build_admin_api",
    "build_auth_api",
    "check_ws_handshake",
    "create_auth_runtime",
    "resolve_webchat_identity",
]