"""C7 task 2.1：ToolExecutionContext 不可变性与派生来源校验。"""

from __future__ import annotations

import dataclasses

import pytest

from agent.tools.context import ResourceScope, ToolExecutionContext


def _ctx(**overrides: object) -> ToolExecutionContext:
    fields: dict[str, object] = {
        "request_id": "req-1",
        "account_id": "acct-1",
        "tenant_id": "tenant:acct-1",
        "session_id": "chat:tenant:acct-1",
        "turn_id": "turn-1",
        "channel": "chat",
        "chat_id": "tenant:acct-1",
        "principal_type": "user",
    }
    fields.update(overrides)
    return ToolExecutionContext(**fields)  # type: ignore[arg-type]


def test_frozen_dataclass_rejects_mutation() -> None:
    ctx = _ctx()
    with pytest.raises(dataclasses.FrozenInstanceError):
        ctx.tenant_id = "tenant:other"  # type: ignore[misc]


def test_empty_tenant_fails_closed() -> None:
    with pytest.raises(ValueError, match="tenant_id"):
        _ctx(tenant_id="")


def test_empty_turn_id_rejected() -> None:
    with pytest.raises(ValueError, match="turn_id"):
        _ctx(turn_id="")


def test_tool_kwargs_expose_identity_keys_only() -> None:
    ctx = _ctx()
    kwargs = ctx.tool_kwargs()
    assert kwargs == {
        "channel": "chat",
        "chat_id": "tenant:acct-1",
        "tenant_id": "tenant:acct-1",
        "current_timestamp": "",
        "current_user_source_ref": "",
    }
    # 其余可信字段不经 kwargs 暴露（避免 LLM 上下文泄漏与参数面伪造）。
    assert "account_id" not in kwargs
    assert "session_id" not in kwargs
    assert "turn_id" not in kwargs


def test_resource_scope_allows() -> None:
    scope = ResourceScope(categories=frozenset({"attachments", "scratch"}))
    assert scope.allows("attachments")
    assert not scope.allows("system")
