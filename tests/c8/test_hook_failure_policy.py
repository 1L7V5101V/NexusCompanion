"""C8 P2 hook failure 分层测试（task-08 2.2，§5.9.16）。

覆盖：pre-tool（gate）超时/异常 → fail-closed 拒绝且真实工具不执行；
post_tool_use（fanout）超时/异常 → 记录失败、工具结果与终态不变、后续 hook
继续；EventBus observe/fanout 观察者有界 timeout → 单观察者失败隔离；
``[agent.plugins]`` timeout 配置加载与进程级默认生效。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from agent.tool_hooks.executor import DEFAULT_HOOK_TIMEOUT_SECONDS, ToolExecutor
from agent.tool_hooks.types import (
    HookContext,
    HookOutcome,
    ToolExecutionRequest,
    ToolExecutionResult,
)
from agent.config import _load_plugin_runtime_config
from agent.config_models import PluginRuntimeConfig
from agent.tool_hooks.base import ToolHook
from bus.event_bus import EventBus


def _request() -> ToolExecutionRequest:
    return ToolExecutionRequest(
        call_id="c1",
        tool_name="dummy",
        arguments={"x": 1},
        source="passive",
    )


async def _invoke(tool_name: str, arguments: dict[str, Any]) -> Any:
    return {"tool": tool_name, "arguments": arguments}


class _HangingHook(ToolHook):
    def __init__(self, name: str, event: str) -> None:
        self.name = name
        self.event = event

    def matches(self, ctx: HookContext) -> bool:
        return True

    async def run(self, ctx: HookContext) -> HookOutcome:
        await asyncio.sleep(30)
        raise AssertionError("超时取消后不应执行到这里")


class _BoomHook(ToolHook):
    def __init__(self, name: str, event: str) -> None:
        self.name = name
        self.event = event

    def matches(self, ctx: HookContext) -> bool:
        return True

    async def run(self, ctx: HookContext) -> HookOutcome:
        raise RuntimeError("hook boom")


class _RecordingHook(ToolHook):
    def __init__(self, name: str, event: str) -> None:
        self.name = name
        self.event = event
        self.calls: list[HookContext] = []

    def matches(self, ctx: HookContext) -> bool:
        return True

    async def run(self, ctx: HookContext) -> HookOutcome:
        self.calls.append(ctx)
        return HookOutcome(extra_message=f"{self.name} saw it")


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ── pre-tool（gate 层）：fail-closed ─────────────────────────────────────────


def test_pre_hook_timeout_rejects_tool_call() -> None:
    executor = ToolExecutor(
        [_HangingHook("slow_gate", "pre_tool_use")],
        hook_timeout_seconds=0.05,
    )
    invoked: list[tuple[str, dict[str, Any]]] = []

    async def _spy(tool_name: str, arguments: dict[str, Any]) -> Any:
        invoked.append((tool_name, arguments))
        return await _invoke(tool_name, arguments)

    result = _run(executor.execute(_request(), _spy))
    assert result.status == "error"
    assert "slow_gate" in str(result.output)
    assert "超时" in str(result.output)
    assert invoked == []  # 真实工具未执行


def test_pre_hook_exception_rejects_tool_call() -> None:
    executor = ToolExecutor([_BoomHook("boom_gate", "pre_tool_use")])
    result = _run(executor.execute(_request(), _invoke))
    assert result.status == "error"
    assert "boom_gate" in str(result.output)
    assert "hook boom" in str(result.output)


# ── post-tool（fanout 层）：有界 timeout，不改终态 ───────────────────────────


def test_post_hook_timeout_does_not_change_terminal_state() -> None:
    late_recorder = _RecordingHook("recorder", "post_tool_use")
    executor = ToolExecutor(
        [_HangingHook("slow_fanout", "post_tool_use"), late_recorder],
        hook_timeout_seconds=0.05,
    )
    result = _run(executor.execute(_request(), _invoke))
    assert result.status == "success"
    assert result.output == {"tool": "dummy", "arguments": {"x": 1}}
    assert any("slow_fanout" in item.reason for item in result.post_hook_trace)
    # 后续观察者仍被执行
    assert len(late_recorder.calls) == 1
    assert result.extra_messages == ["recorder saw it"]


def test_post_error_hook_timeout_does_not_mask_tool_error() -> None:
    executor = ToolExecutor(
        [_HangingHook("slow_error_fanout", "post_tool_error")],
        hook_timeout_seconds=0.05,
    )

    async def _broken(_tool_name: str, _arguments: dict[str, Any]) -> Any:
        raise RuntimeError("boom")

    result = _run(executor.execute(_request(), _broken))
    assert result.status == "error"
    assert result.output == "工具执行出错: boom"
    assert any("slow_error_fanout" in item.reason for item in result.post_hook_trace)


def test_executor_default_timeout_is_bounded() -> None:
    assert DEFAULT_HOOK_TIMEOUT_SECONDS > 0
    executor = ToolExecutor([_HangingHook("slow", "pre_tool_use")])
    assert executor._hook_timeout == DEFAULT_HOOK_TIMEOUT_SECONDS


def test_process_default_timeout_override(tmp_path: object) -> None:
    ToolExecutor.set_default_hook_timeout(0.05)
    try:
        executor = ToolExecutor([_HangingHook("slow", "pre_tool_use")])
        assert executor._hook_timeout == 0.05
        result = _run(executor.execute(_request(), _invoke))
        assert result.status == "error"
        assert "超时" in str(result.output)
    finally:
        ToolExecutor.set_default_hook_timeout(DEFAULT_HOOK_TIMEOUT_SECONDS)
    with pytest.raises(ValueError):
        ToolExecutor.set_default_hook_timeout(0)
    with pytest.raises(ValueError):
        ToolExecutor([], hook_timeout_seconds=-1)


# ── EventBus 观察者（telemetry/fanout 层）：有界 timeout 隔离 ────────────────


@pytest.mark.asyncio
async def test_observe_hanging_observer_isolated() -> None:
    bus = EventBus(observer_timeout_seconds=0.05)
    seen: list[str] = []

    async def _hanging(event: object) -> None:
        await asyncio.sleep(30)

    async def _healthy(event: object) -> None:
        seen.append(type(event).__name__)

    _ = bus.on(_ProbeEvent, _hanging)
    _ = bus.on(_ProbeEvent, _healthy)
    await bus.observe(_ProbeEvent())
    assert seen == ["_ProbeEvent"]


@pytest.mark.asyncio
async def test_fanout_hanging_observer_isolated_and_counted() -> None:
    bus = EventBus(observer_timeout_seconds=0.05)
    seen: list[str] = []

    async def _hanging(event: object) -> None:
        await asyncio.sleep(30)

    async def _healthy(event: object) -> None:
        seen.append("ok")

    _ = bus.on(_ProbeEvent, _hanging)
    _ = bus.on_any(_healthy)
    await bus.fanout(_ProbeEvent())
    assert seen == ["ok"]


@pytest.mark.asyncio
async def test_emit_intercept_handler_exception_propagates() -> None:
    """emit intercept 是 gate 层：异常上抛（保持既有语义），不改写事件为静默。"""

    bus = EventBus()

    async def _boom(event: _ProbeEvent) -> _ProbeEvent:
        raise RuntimeError("intercept boom")

    _ = bus.on(_ProbeEvent, _boom)
    with pytest.raises(RuntimeError, match="intercept boom"):
        await bus.emit(_ProbeEvent())


class _ProbeEvent:
    pass


# ── [agent.plugins] 配置加载 ────────────────────────────────────────────────


def test_plugin_runtime_config_defaults() -> None:
    cfg = _load_plugin_runtime_config({})
    assert cfg == PluginRuntimeConfig(
        hook_timeout_seconds=5.0,
        observer_timeout_seconds=5.0,
    )


def test_plugin_runtime_config_overrides_and_validation() -> None:
    cfg = _load_plugin_runtime_config(
        {"agent": {"plugins": {"hook_timeout_seconds": 2.5}}}
    )
    assert cfg.hook_timeout_seconds == 2.5
    assert cfg.observer_timeout_seconds == 5.0
    with pytest.raises(ValueError, match="hook_timeout_seconds"):
        _load_plugin_runtime_config(
            {"agent": {"plugins": {"hook_timeout_seconds": 0}}}
        )
