"""C7 工具执行上下文（§5.8.1 / change c7-tool-isolation design ADR-1）。

不可变、由服务端派生：account/tenant/session/turn 等可信字段只能来自认证身份、
可信 channel binding 与当前 turn，模型与客户端帧 SHALL NOT 提供或覆盖
（roadmap §5.9.7 第 1 条）。本类是工具调用的唯一授权依据。

Roadmap §3.2 约束：ContextVar 不得作为授权依据——本上下文经**显式参数**穿线
（pipeline 在 turn 收束时构造一次 → invoker 闭包绑定 → ``ToolRegistry.execute(
context=...)``），不经进程级可变状态传递。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


class TenantPathResolver(Protocol):
    """租户资源 root 解析契约（§5.8.5；具体实现由 task 5.1 落地）。"""

    def attachments_root(self, context: "ToolExecutionContext") -> str: ...

    def scratch_root(self, context: "ToolExecutionContext") -> str: ...

    def exports_root(self, context: "ToolExecutionContext", job_id: str) -> str: ...

    def mcp_root(self, context: "ToolExecutionContext", mcp_id: str) -> str: ...

    def resolve_relative(self, root: str, path: str) -> str: ...


@dataclass(frozen=True)
class ResourceScope:
    """当前上下文允许访问的资源类别（§5.8.5：attachments/scratch/exports/mcp）。"""

    categories: frozenset[str] = frozenset()

    def allows(self, category: str) -> bool:
        return category in self.categories


@dataclass(frozen=True)
class ToolExecutionContext:
    """一次工具调用的可信执行上下文（工具调用的唯一授权依据）。

    ``account_id``/``tenant_id``/``session_id``/``turn_id`` 由服务端从认证身份、
    可信 channel binding 与当前 turn 派生；dev-only 单用户路径使用显式 dev 身份
    （不变量与 C4 dev 回退一致），SHALL NOT 在启用认证的实例上出现。
    """

    request_id: str
    account_id: str
    tenant_id: str
    session_id: str
    turn_id: str
    channel: str
    chat_id: str
    principal_type: str
    capabilities: frozenset[str] = frozenset()
    resource_scope: ResourceScope = field(default_factory=ResourceScope)
    path_resolver: TenantPathResolver | None = None
    # 每 turn 运行时提示（非授权字段）：记忆召回的时间锚点与写入溯源。
    current_timestamp: str = ""
    current_user_source_ref: str = ""

    def __post_init__(self) -> None:
        # fail-closed：tenant 是资源边界，缺失即拒绝构造（不回落默认租户）。
        if not self.tenant_id:
            raise ValueError("ToolExecutionContext.tenant_id 不能为空（fail-closed）")
        if not self.turn_id:
            raise ValueError("ToolExecutionContext.turn_id 不能为空")

    def tool_kwargs(self) -> dict[str, str]:
        """工具执行时注入的 kwarg。

        前三个为可信身份键（最高优先级，覆盖 arguments 同名字段）；后两个为
        per-turn 提示键（同样由服务端派生，模型参数不参与覆盖）。
        """
        return {
            "channel": self.channel,
            "chat_id": self.chat_id,
            "tenant_id": self.tenant_id,
            "current_timestamp": self.current_timestamp,
            "current_user_source_ref": self.current_user_source_ref,
        }
