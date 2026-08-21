"""可信 channel identity -> TenantContext 的派生 seam（M4H-2）。

tenant 只能由服务端可信 channel/auth identity 派生：channel 名与 chat_id 均来自
平台服务器（Telegram Update / QQ OneBot 事件）或可信调用方（CLI / programmatic），
永不接受客户端任意 tenant_id。session key（`f"{channel}:{chat_id}"`）不是 tenant
identity——本模块是唯一合法的派生入口，业务代码不得自行解析 session key 反推
tenant，也不得把客户端 metadata / query parameter 当作 tenant 来源。
"""

from __future__ import annotations

from infra.storage.interfaces import TenantContext

__all__ = [
    "DEFAULT_TENANT",
    "assert_tenant_resolved",
    "resolve_tenant",
    "tenant_id_for_channel",
]


DEFAULT_TENANT = "default"
"""显式 single-user / 测试专用默认租户。多用户入口禁止隐式回退到它。"""


def tenant_id_for_channel(channel: str, chat_id: str) -> str:
    """由可信 channel 身份派生稳定 tenant_id。

    服务端纯函数、确定性：同一 (channel, chat_id) 永远得到同一 tenant。telegram /
    qq 等通道按 chat_id 隔离数据（群内共享同一 tenant，成员间不隔离——与现有
    session 语义一致）。
    """
    return f"{channel}:{chat_id}"


def resolve_tenant(channel: str, chat_id: str) -> TenantContext:
    """可信 identity -> TenantContext。适配器在入站边界调用一次。"""
    return TenantContext(tenant_id=tenant_id_for_channel(channel, chat_id))


def assert_tenant_resolved(tenant_id: str) -> str:
    """fail-closed 哨兵：空 tenant_id 表示调用点忘记解析 tenant。

    立即抛错而不是静默落到 DEFAULT_TENANT——多用户路径禁止隐式 default。
    返回原值便于内联使用。
    """
    if not tenant_id.strip():
        raise ValueError(
            "tenant_id 为空：调用点必须从可信 inbound identity 解析 tenant，"
            "禁止隐式回退到 default"
        )
    return tenant_id
