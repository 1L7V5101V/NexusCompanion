"""repository 层共享的 id 归一化。

asyncpg 返回 ``pgproto.UUID``（非 ``uuid.UUID`` 子类，也没有 ``.replace``），
直接喂给 ``uuid.UUID()`` 会抛 AttributeError；统一按 canonical hex 字符串再构造，
同时兼容 ``uuid.UUID`` / ``str`` / ``pgproto.UUID`` 输入。

此前该逻辑在 ``auth_repo`` / ``provisioning_repo`` / ``auth.service`` 各有一份
拷贝且实现已漂移（``str(value)`` vs ``hex=str(value)``），收敛到此处。
"""

from __future__ import annotations

import uuid

__all__ = ["to_uuid"]


def to_uuid(value: uuid.UUID | str) -> uuid.UUID:
    """归一化为 ``uuid.UUID``；``pgproto.UUID`` 经 canonical hex 再构造。"""
    if isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(hex=str(value))
