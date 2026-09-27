"""C7 task 3.2：pre-tool 执行前重查——账号状态 × 租户白名单（§5.8.2/§5.8.8）。

schema 可见性只是三层防线的第一道；本 hook 在**每次执行前**重新校验：

1. **账号状态**：委托 C8 :class:`RevocationGate`（fail-closed：suspended/revoked/
   unknown/provider 异常一律拒绝；provider 未接线 = 显式 dev-open，结构化日志）；
2. **租户白名单**：principal 为 ``user`` 时，工具必须落在租户可见目录
   （:func:`agent.tools.catalog.tenant_visible_names`，白名单 ∩ registry − 关闭
   清单 ∪ engine 注入）；principal 为 dev/admin（owner 路径）不在此层过滤。

拒绝错误码冻结（对模型可见的稳定文案，不得回显账号状态细节）：
``tool_denied_account_status`` / ``tool_denied_tenant_scope``。
"""

from __future__ import annotations

from agent.admission.revocation import RevocationGate, RevocationRejected
from agent.tool_hooks.base import ToolHook
from agent.tool_hooks.types import HookContext, HookOutcome
from agent.tools.catalog import tenant_visible_names
from agent.tools.registry import ToolRegistry

DENY_ACCOUNT_STATUS = "tool_denied_account_status"
DENY_TENANT_SCOPE = "tool_denied_tenant_scope"


class TenantToolGateHook(ToolHook):
    name = "tenant_tool_gate"
    event = "pre_tool_use"

    def __init__(
        self,
        registry: ToolRegistry,
        revocation_gate: RevocationGate | None = None,
    ) -> None:
        self._registry = registry
        self._revocation_gate = revocation_gate

    def matches(self, ctx: HookContext) -> bool:
        return True

    async def run(self, ctx: HookContext) -> HookOutcome:
        request = ctx.request
        context = request.tool_context

        # 1. 账号状态重查（revocation 语义由 gate 承担；未接线路径 dev-open）。
        if self._revocation_gate is not None and context is not None:
            try:
                await self._revocation_gate.check(
                    context.tenant_id, action=f"tool:{request.tool_name}"
                )
            except RevocationRejected:
                return HookOutcome(
                    decision="deny",
                    reason=(
                        f"错误：当前账号状态不允许执行该工具（code={DENY_ACCOUNT_STATUS}）"
                    ),
                )

        # 2. 租户白名单重查（仅 user principal；dev/admin 走 owner 全量视图）。
        if context is not None and context.principal_type == "user":
            visible = tenant_visible_names(self._registry, context)
            if visible is not None and request.tool_name not in visible:
                return HookOutcome(
                    decision="deny",
                    reason=(
                        f"错误：工具不在当前租户可用目录（code={DENY_TENANT_SCOPE}）"
                    ),
                )

        return HookOutcome(decision="pass")
