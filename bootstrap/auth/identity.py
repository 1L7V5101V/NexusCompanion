"""已认证 session → WebChat 服务端派生身份（§5.9.1）。

``check_ws_handshake`` 只回答「凭据是否有效」，不解析归属。本模块把有效 session
映射为 WebChat 的三元组 ``account_id → tenant_id → canonical conversation_id``：
tenant 只能由服务端可信身份派生，SHALL NOT 取自客户端帧字段，也 SHALL NOT 在
派生失败时回落到 ``DEFAULT_TENANT``（fail-closed）。

C5 的 ``check_ws_handshake`` docstring 已声明其返回的 session dict「供 C4 通道
派生 tenant 归属」——本模块即该接缝的实现。
"""

from __future__ import annotations

from typing import Any

from infra.channels.web_chat_channel import WebChatIdentity

from bootstrap.auth.runtime import AuthRuntime

__all__ = [
    "WebChatIdentityError",
    "resolve_webchat_identity",
]


class WebChatIdentityError(RuntimeError):
    """无法为该 session 派生可用身份（fail-closed，不回落 dev 默认租户）。"""


async def resolve_webchat_identity(
    runtime: AuthRuntime,
    session: dict[str, Any],
) -> WebChatIdentity:
    """按已认证 session 派生 WebChat 身份（C1 身份链唯一入口）。

    - session 无绑定账号 → 拒绝（admin session 不得用于用户面）；
    - 账号尚无 canonical conversation（provisioning 未完成）→ 拒绝，
      不回落 ``DEFAULT_TENANT``。
    """
    account_id = session.get("account_id")
    if account_id is None:
        raise WebChatIdentityError("session 未绑定账号")

    conversations = await runtime.canonical_repo.list_conversations_by_account(account_id)
    if not conversations:
        raise WebChatIdentityError("账号尚无 canonical conversation")

    # 账号可拥有 0..N 个 agent；created_at 升序的确定性枚举下取首个作为默认入口。
    conversation = conversations[0]
    tenant_id = str(conversation["tenant_id"])
    conversation_id = str(conversation["id"])
    return WebChatIdentity(
        account_id=str(account_id),
        tenant_id=tenant_id,
        conversation_id=conversation_id,
        # session 路由键按 tenant 区分，避免不同登录者共享 ``chat:local``。
        session_key=f"chat:{tenant_id}",
        chat_id=tenant_id,
    )
