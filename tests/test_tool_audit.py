"""C7 task 7.1：tool_audit_events 写入接线 + 参数脱敏（design ADR-6）。

- 每次工具调用（含执行前拒绝）在 registry 终态写一行审计；
- secret 键只存 hash，长内容截断，原始参数不可还原；
- 外层 turn 取消：先写 cancelled 再原样上抛（不吞）；
- 未接审计（无 sink / 无 context）行为不变；
- LogAuditSink（单机结构化日志兜底）与 PgToolAuditSink（repo 层）各测。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from agent.admission.tool_audit import (
    LogAuditSink,
    ToolAuditEvent,
    NullAuditSink,
    redact_arguments,
    is_secret_key,
)
from agent.tools.base import Tool, ToolEffect, ToolResult
from agent.tools.context import ToolExecutionContext
from agent.tools.registry import ToolRegistry

from bootstrap.audit import PgToolAuditSink


class _CollectingSink:
    def __init__(self) -> None:
        self.events: list[ToolAuditEvent] = []

    async def write(self, event: ToolAuditEvent) -> None:
        self.events.append(event)


def _ctx(tenant: str = "tenant:a") -> ToolExecutionContext:
    return ToolExecutionContext(
        request_id="req-uuid-1",
        account_id="acct-uuid-1",
        tenant_id=tenant,
        session_id="sess-1",
        turn_id="turn-uuid-1",
        channel="chat",
        chat_id=tenant,
        principal_type="user",
    )


def _echo_tool(name: str = "audit_echo", *, raises: bool = False) -> Tool:
    async def execute(self: Any, **kwargs: Any) -> str:
        if raises:
            raise RuntimeError("boom")
        return "ok"

    return type(
        f"_Audit_{name}",
        (Tool,),
        {
            "name": name,
            "description": "audit stub",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "execute": execute,
        },
    )()


# ── 脱敏 ─────────────────────────────────────────────────────────


def test_secret_keys_hashed_not_plaintext():
    redacted, digest = redact_arguments(
        {"api_key": "sk-super-secret", "token": "tok-123", "desc": "hello"}
    )
    assert "sk-super-secret" not in redacted
    assert "tok-123" not in redacted
    data = json.loads(redacted)
    assert data["api_key"] == {
        "__hash__": hashlib.sha256(b"sk-super-secret").hexdigest()
    }
    assert data["desc"] == "hello"
    assert len(digest) == 64


def test_long_content_truncated():
    long_value = "x" * 5000
    redacted, _ = redact_arguments({"content": long_value})
    assert long_value not in redacted
    assert "truncated" in redacted


def test_arguments_hash_reproducible_and_irreversible():
    args = {"a": 1, "b": "two"}
    _, d1 = redact_arguments(args)
    _, d2 = redact_arguments(dict(args))
    assert d1 == d2
    _, d3 = redact_arguments({"a": 1, "b": "tw0"})
    assert d1 != d3
    # digest 为 64 位 hex（仅 0-9a-f），不含原文内容字符。
    assert len(d1) == 64
    assert "two" not in d1


def test_nested_structure_bounded():
    deep = {"l1": {"l2": {"l3": {"l4": {"l5": "secret"}}}}}
    redacted, _ = redact_arguments(deep)
    assert "secret" not in redacted
    assert "<redacted:depth>" in redacted


def test_secret_key_fragments():
    assert is_secret_key("api_key")
    assert is_secret_key("openai_api_key")
    assert is_secret_key("AUTH_TOKEN")
    assert is_secret_key("access_token")
    assert not is_secret_key("content")
    assert not is_secret_key("description")


# ── registry 审计流 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_successful_call_writes_audit():
    registry = ToolRegistry()
    sink = _CollectingSink()
    registry.set_audit_sink(sink)
    registry.register(_echo_tool(), effect=ToolEffect.READ_ONLY)

    result = await registry.execute("audit_echo", {"desc": "hi"}, context=_ctx())

    assert result == "ok"
    assert len(sink.events) == 1
    ev = sink.events[0]
    assert ev.status == "succeeded"
    assert ev.tool_name == "audit_echo"
    assert ev.tenant_id == "tenant:a"
    assert ev.effect_class == ToolEffect.READ_ONLY.value
    assert ev.tool_call_id == "req-uuid-1"
    assert ev.duration_ms is not None and ev.duration_ms >= 0
    assert ev.error_code is None


@pytest.mark.asyncio
async def test_denied_effect_writes_rejected():
    registry = ToolRegistry()
    sink = _CollectingSink()
    registry.set_audit_sink(sink)
    registry.register(_echo_tool("audit_proc"), effect=ToolEffect.PROCESS_EXEC)

    result = await registry.execute("audit_proc", {}, context=_ctx())

    assert "tool_denied_effect" in str(result)
    assert len(sink.events) == 1
    assert sink.events[0].status == "rejected"
    assert sink.events[0].error_code == "tool_denied_effect"


@pytest.mark.asyncio
async def test_unknown_tool_writes_rejected():
    registry = ToolRegistry()
    sink = _CollectingSink()
    registry.set_audit_sink(sink)

    result = await registry.execute("no_such_tool", {}, context=_ctx())

    assert "不存在" in str(result)
    assert len(sink.events) == 1
    assert sink.events[0].status == "rejected"
    assert sink.events[0].error_code == "tool_not_found"


@pytest.mark.asyncio
async def test_execution_exception_writes_failed():
    registry = ToolRegistry()
    sink = _CollectingSink()
    registry.set_audit_sink(sink)
    registry.register(_echo_tool("audit_boom", raises=True), effect=ToolEffect.READ_ONLY)

    result = await registry.execute("audit_boom", {}, context=_ctx())

    assert "执行出错" in str(result)
    assert len(sink.events) == 1
    assert sink.events[0].status == "failed"


@pytest.mark.asyncio
async def test_tenant_cancellation_writes_cancelled():
    from agent.admission.tool_cancellation import shared_registry

    async def _hang(self: Any, **kwargs: Any) -> str:
        await asyncio.Event().wait()
        return "never"

    tool = type(
        "_AuditSlow",
        (Tool,),
        {
            "name": "audit_slow",
            "description": "hang",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "execute": _hang,
        },
    )()
    registry = ToolRegistry()
    sink = _CollectingSink()
    registry.set_audit_sink(sink)
    registry.register(tool, effect=ToolEffect.READ_ONLY)

    turn = asyncio.create_task(
        registry.execute("audit_slow", {}, context=_ctx("tenant:slow"))
    )
    await asyncio.sleep(0.05)
    await shared_registry().cancel_tenant("tenant:slow", reason="test")
    result = await asyncio.wait_for(turn, timeout=5)
    assert isinstance(result, str)
    assert "tool_cancelled_account_status" in result
    assert len(sink.events) == 1
    assert sink.events[0].status == "cancelled"


@pytest.mark.asyncio
async def test_outer_cancellation_writes_cancelled_and_reraises():
    async def _hang(self: Any, **kwargs: Any) -> str:
        await asyncio.Event().wait()
        return "never"

    tool = type(
        "_AuditSlow2",
        (Tool,),
        {
            "name": "audit_slow2",
            "description": "hang",
            "parameters": {"type": "object", "properties": {}, "required": []},
            "execute": _hang,
        },
    )()
    registry = ToolRegistry()
    sink = _CollectingSink()
    registry.set_audit_sink(sink)
    registry.register(tool, effect=ToolEffect.READ_ONLY)

    turn = asyncio.create_task(
        registry.execute("audit_slow2", {}, context=_ctx("tenant:outer"))
    )
    await asyncio.sleep(0.05)
    turn.cancel()
    with pytest.raises(asyncio.CancelledError):
        await turn
    await asyncio.sleep(0.05)

    assert len(sink.events) == 1
    assert sink.events[0].status == "cancelled"


@pytest.mark.asyncio
async def test_secret_args_not_in_audit_row():
    registry = ToolRegistry()
    sink = _CollectingSink()
    registry.set_audit_sink(sink)
    registry.register(_echo_tool("audit_secret"), effect=ToolEffect.READ_ONLY)

    await registry.execute(
        "audit_secret",
        {"api_key": "sk-plaintext-123", "content": "x" * 999},
        context=_ctx(),
    )

    ev = sink.events[0]
    assert "sk-plaintext-123" not in ev.arguments_redacted
    data = json.loads(ev.arguments_redacted)
    assert data["api_key"]["__hash__"] == hashlib.sha256(
        b"sk-plaintext-123"
    ).hexdigest()
    assert len(ev.arguments_hash) == 64


@pytest.mark.asyncio
async def test_no_sink_or_context_keeps_behavior():
    registry = ToolRegistry()
    registry.register(_echo_tool(), effect=ToolEffect.READ_ONLY)

    # 无 sink：不审计、正常返回。
    assert await registry.execute("audit_echo", {}) == "ok"
    # 有 sink 但无 context：不审计（身份缺失无可写行）。
    sink = _CollectingSink()
    registry.set_audit_sink(sink)
    assert await registry.execute("audit_echo", {}) == "ok"
    assert sink.events == []


@pytest.mark.asyncio
async def test_sink_failure_does_not_block_call(monkeypatch):
    class _BrokenSink:
        async def write(self, event: ToolAuditEvent) -> None:
            raise RuntimeError("db down")

    registry = ToolRegistry()
    registry.set_audit_sink(_BrokenSink())
    registry.register(_echo_tool(), effect=ToolEffect.READ_ONLY)

    # 审计失败只告警，工具调用正常完成。
    assert await registry.execute("audit_echo", {}, context=_ctx()) == "ok"


@pytest.mark.asyncio
async def test_log_sink_writes_ndjson(tmp_path: Path):
    sink = LogAuditSink(tmp_path / "logs")
    ev = ToolAuditEvent(
        tenant_id="tenant:a",
        tool_name="t",
        effect_class="read-only",
        status="succeeded",
        request_id="req-1",
        account_id="acct-1",
        arguments_redacted='{"desc": "hi"}',
        arguments_hash="h" * 64,
        duration_ms=3,
    )
    await sink.write(ev)

    lines = (tmp_path / "logs" / "tool_audit.ndjson").read_text(
        encoding="utf-8"
    ).strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["tenant_id"] == "tenant:a"
    assert row["status"] == "succeeded"
    assert row["tool_name"] == "t"


@pytest.mark.asyncio
async def test_pg_sink_forwards_to_repo():
    repo = AsyncMock()
    repo.record_event = AsyncMock()
    sink = PgToolAuditSink(repo)
    ev = ToolAuditEvent(
        tenant_id="tenant:a",
        tool_name="t",
        effect_class="read-only",
        status="rejected",
    )
    await sink.write(ev)
    repo.record_event.assert_awaited_once_with(ev)

    # repo 抛错 → fail-open，不阻断调用方。
    repo.record_event.side_effect = RuntimeError("db down")
    await sink.write(ev)  # 只告警


def test_null_sink_is_noop():
    event = ToolAuditEvent(
        tenant_id="t", tool_name="x", effect_class="read-only", status="succeeded"
    )
    assert asyncio.run(NullAuditSink().write(event)) is None