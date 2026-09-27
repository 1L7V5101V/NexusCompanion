"""C7 task 3.2：TenantToolGateHook——执行前账号状态 × 租户白名单重查。

对应 spec「三层工具目录与租户白名单」（schema 可见 ≠ 可执行）与
「账号状态联动」（suspended/revoked/unknown → 结构化拒绝，fail-closed）。
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.admission.revocation import RevocationGate, TenantStatus
from agent.tool_hooks.tenant_gate import (
    DENY_ACCOUNT_STATUS,
    DENY_TENANT_SCOPE,
    TenantToolGateHook,
)
from agent.tool_hooks.types import HookContext, HookEvent, ToolExecutionRequest
from agent.tools.base import Tool
from agent.tools.context import ToolExecutionContext
from agent.tools.registry import ToolRegistry


class _StubTool(Tool):
    name = "stub"
    description = "stub"
    parameters = {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs: Any) -> str:
        return "ok"


def _registry_with_all() -> ToolRegistry:
    registry = ToolRegistry()
    for name in ("recall_memory", "read_file", "shell", "spawn"):
        tool = type(
            f"_T_{name}",
            (_StubTool,),
            {"name": name, "description": f"stub {name}"},
        )()
        registry.register(tool)
    return registry


def _context(principal: str = "user", tenant: str = "tenant:acct-1"):
    return ToolExecutionContext(
        request_id="req-1",
        account_id="acct-1",
        tenant_id=tenant,
        session_id="chat:tenant:acct-1",
        turn_id="turn-1",
        channel="chat",
        chat_id="tenant:acct-1",
        principal_type=principal,
    )


def _hook_ctx(tool_name: str, context=None) -> HookContext:
    request = ToolExecutionRequest(
        call_id="c1",
        tool_name=tool_name,
        arguments={},
        source="passive",
        session_key="chat:tenant:acct-1",
        tool_context=context,
    )
    return HookContext(event="pre_tool_use", request=request, current_arguments={})


async def _status(status: TenantStatus) -> TenantStatus:
    return status


@pytest.mark.asyncio
async def test_user_principal_denied_for_closed_tool() -> None:
    hook = TenantToolGateHook(_registry_with_all())
    outcome = await hook.run(_hook_ctx("shell", _context()))
    assert outcome.decision == "deny"
    assert DENY_TENANT_SCOPE in outcome.reason


@pytest.mark.asyncio
async def test_user_principal_allowed_for_whitelisted_tool() -> None:
    hook = TenantToolGateHook(_registry_with_all())
    outcome = await hook.run(_hook_ctx("recall_memory", _context()))
    assert outcome.decision == "pass"


@pytest.mark.asyncio
async def test_dev_principal_bypasses_whitelist() -> None:
    hook = TenantToolGateHook(_registry_with_all())
    outcome = await hook.run(_hook_ctx("shell", _context(principal="dev")))
    assert outcome.decision == "pass"


@pytest.mark.asyncio
async def test_suspended_account_denied_even_for_allowed_tool() -> None:
    gate = RevocationGate(lambda tenant: _status(TenantStatus.SUSPENDED))
    hook = TenantToolGateHook(_registry_with_all(), gate)
    outcome = await hook.run(_hook_ctx("recall_memory", _context()))
    assert outcome.decision == "deny"
    assert DENY_ACCOUNT_STATUS in outcome.reason


@pytest.mark.asyncio
async def test_unknown_status_fails_closed() -> None:
    gate = RevocationGate(lambda tenant: _status(TenantStatus.UNKNOWN))
    hook = TenantToolGateHook(_registry_with_all(), gate)
    outcome = await hook.run(_hook_ctx("recall_memory", _context()))
    assert outcome.decision == "deny"


@pytest.mark.asyncio
async def test_provider_error_fails_closed() -> None:
    async def _boom(tenant: str) -> TenantStatus:
        raise RuntimeError("db down")

    hook = TenantToolGateHook(_registry_with_all(), RevocationGate(_boom))
    outcome = await hook.run(_hook_ctx("recall_memory", _context()))
    assert outcome.decision == "deny"


@pytest.mark.asyncio
async def test_active_account_allowed() -> None:
    gate = RevocationGate(lambda tenant: _status(TenantStatus.ACTIVE))
    hook = TenantToolGateHook(_registry_with_all(), gate)
    outcome = await hook.run(_hook_ctx("recall_memory", _context()))
    assert outcome.decision == "pass"


@pytest.mark.asyncio
async def test_no_context_dev_open_gate_passes() -> None:
    """未接线路径（无 context + dev-open gate）：放行，不静默降级。"""
    hook = TenantToolGateHook(_registry_with_all(), RevocationGate(None))
    outcome = await hook.run(_hook_ctx("shell", None))
    assert outcome.decision == "pass"
