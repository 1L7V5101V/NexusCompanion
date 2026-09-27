from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any, Awaitable, Callable

from agent.tool_hooks.base import ToolHook
from agent.tool_hooks.types import (
    HookContext,
    HookTraceItem,
    ToolExecutionRequest,
    ToolExecutionResult,
)

ToolInvoker = Callable[[str, dict[str, Any]], Awaitable[Any]]

DEFAULT_HOOK_TIMEOUT_SECONDS = 5.0


class HookExecutionError(RuntimeError):
    def __init__(self, hook_name: str, event: str, cause: Exception) -> None:
        self.hook_name = hook_name
        self.event = event
        self.cause = cause
        super().__init__(f"hook {hook_name} ({event}) failed: {cause}")


class ToolExecutor:
    _default_timeout = DEFAULT_HOOK_TIMEOUT_SECONDS

    @classmethod
    def set_default_hook_timeout(cls, seconds: float) -> None:
        """进程级默认超时（bootstrap 从 [agent.plugins].hook_timeout_seconds 设置一次）。

        显式传入 ``hook_timeout_seconds`` 的构造点优先；未接入 config 的深层
        pipeline（passive/drift/proactive/subagent）经此统一生效，避免逐层
        Deps 穿线。
        """
        if seconds <= 0:
            raise ValueError("hook_timeout_seconds 必须为正")
        cls._default_timeout = seconds

    def __init__(
        self,
        hooks: Sequence[ToolHook] | None = None,
        *,
        hook_timeout_seconds: float | None = None,
    ) -> None:
        resolved = (
            hook_timeout_seconds
            if hook_timeout_seconds is not None
            else type(self)._default_timeout
        )
        if resolved <= 0:
            raise ValueError("hook_timeout_seconds 必须为正")
        self._hooks = list(hooks or [])
        self._hook_timeout = resolved

    def add_hooks(self, hooks: Sequence[ToolHook]) -> None:
        self._hooks.extend(hooks)

    def _runtime_hooks(self) -> list[ToolHook]:
        from agent.plugins.snapshot import get_current_runtime_snapshot

        snapshot = get_current_runtime_snapshot()
        if snapshot is not None:
            fixed = [
                hook
                for hook in self._hooks
                if not getattr(hook, "snapshot_managed", False)
            ]
            return [*fixed, *snapshot.tool_hooks]
        return self._hooks

    async def _bounded_hook_run(
        self,
        hook: ToolHook,
        ctx: HookContext,
    ) -> object:
        """带界执行 hook.run（§5.9.16 hook policy 分层的超时边界）。

        超时统一转为 :class:`HookExecutionError`，由调用方按层级处置：
        pre（gate）→ fail-closed 拒绝本次工具调用；post（fanout）→ 记录失败。
        """
        try:
            return await asyncio.wait_for(hook.run(ctx), self._hook_timeout)
        except TimeoutError as exc:
            raise HookExecutionError(
                hook.name,
                hook.event,
                TimeoutError(f"hook 超时（>{self._hook_timeout}s）"),
            ) from exc

    async def execute(
        self,
        request: ToolExecutionRequest,
        invoker: ToolInvoker,
    ) -> ToolExecutionResult:
        """执行单次工具调用。

        request 描述“这次想调用什么工具、带什么参数”；
        invoker 是真实执行入口（通常是 ToolRegistry.execute）。

        固定流程：
        1. pre hooks：匹配、改参、必要时拒绝
        2. invoker：用最终参数执行真实工具
        3. post hooks：记录成功或错误后的附加信息与 trace
        """
        current_arguments = dict(request.arguments)
        extra_messages: list[str] = []
        pre_trace: list[HookTraceItem] = []
        post_trace: list[HookTraceItem] = []

        try:
            # pre_hook 是唯一允许改输入/直接 deny 的阶段。
            denied_reason, current_arguments = await self._run_pre_hooks(
                request=request,
                current_arguments=current_arguments,
                extra_messages=extra_messages,
                traces=pre_trace,
            )
        except HookExecutionError as exc:
            return ToolExecutionResult(
                status="error",
                output=f"工具执行出错: {exc}",
                final_arguments=dict(current_arguments),
                extra_messages=extra_messages,
                pre_hook_trace=pre_trace,
                post_hook_trace=post_trace,
            )
        final_arguments = dict(current_arguments)
        if denied_reason:
            return ToolExecutionResult(
                status="denied",
                output=denied_reason,
                final_arguments=final_arguments,
                extra_messages=extra_messages,
                pre_hook_trace=pre_trace,
                post_hook_trace=post_trace,
            )

        try:
            # 这里才进入真实工具执行；hook 本身不直接替代工具实现。
            output = await invoker(request.tool_name, final_arguments)
        except Exception as exc:
            error_text = str(exc)
            try:
                # 工具自身报错后，允许 post_tool_error 做记录型处理。
                # fanout 层：hook 失败不得掩盖/改写工具本身的错误终态。
                await self._run_post_hooks(
                    HookContext(
                        event="post_tool_error",
                        request=request,
                        current_arguments=final_arguments,
                        error=error_text,
                    ),
                    extra_messages=extra_messages,
                    traces=post_trace,
                    fail_open=True,
                )
            except HookExecutionError as hook_exc:
                return ToolExecutionResult(
                    status="error",
                    output=f"工具执行出错: {hook_exc}",
                    final_arguments=final_arguments,
                    extra_messages=extra_messages,
                    pre_hook_trace=pre_trace,
                    post_hook_trace=post_trace,
                )
            return ToolExecutionResult(
                status="error",
                output=f"工具执行出错: {error_text}",
                final_arguments=final_arguments,
                extra_messages=extra_messages,
                pre_hook_trace=pre_trace,
                post_hook_trace=post_trace,
            )

        try:
            # post_tool_use 只做观察和补充信息，不回写执行参数。
            await self._run_post_hooks(
                HookContext(
                    event="post_tool_use",
                    request=request,
                    current_arguments=final_arguments,
                    result=output,
                ),
                extra_messages=extra_messages,
                traces=post_trace,
                fail_open=True,
            )
        except HookExecutionError as exc:
            return ToolExecutionResult(
                status="error",
                output=f"工具执行出错: {exc}",
                final_arguments=final_arguments,
                extra_messages=extra_messages,
                pre_hook_trace=pre_trace,
                post_hook_trace=post_trace,
            )
        return ToolExecutionResult(
            status="success",
            output=output,
            final_arguments=final_arguments,
            extra_messages=extra_messages,
            pre_hook_trace=pre_trace,
            post_hook_trace=post_trace,
        )

    async def preflight(
        self,
        request: ToolExecutionRequest,
    ) -> ToolExecutionResult:
        current_arguments = dict(request.arguments)
        extra_messages: list[str] = []
        pre_trace: list[HookTraceItem] = []
        try:
            denied_reason, current_arguments = await self._run_pre_hooks(
                request=request,
                current_arguments=current_arguments,
                extra_messages=extra_messages,
                traces=pre_trace,
            )
        except HookExecutionError as exc:
            return ToolExecutionResult(
                status="error",
                output=f"工具执行出错: {exc}",
                final_arguments=dict(current_arguments),
                extra_messages=extra_messages,
                pre_hook_trace=pre_trace,
            )
        if denied_reason:
            return ToolExecutionResult(
                status="denied",
                output=denied_reason,
                final_arguments=dict(current_arguments),
                extra_messages=extra_messages,
                pre_hook_trace=pre_trace,
            )
        return ToolExecutionResult(
            status="success",
            output="",
            final_arguments=dict(current_arguments),
            extra_messages=extra_messages,
            pre_hook_trace=pre_trace,
        )

    async def _run_pre_hooks(
        self,
        *,
        request: ToolExecutionRequest,
        current_arguments: dict[str, Any],
        extra_messages: list[str],
        traces: list[HookTraceItem],
    ) -> tuple[str, dict[str, Any]]:
        for hook in self._runtime_hooks():
            if hook.event != "pre_tool_use":
                continue
            ctx = HookContext(
                event="pre_tool_use",
                request=request,
                current_arguments=dict(current_arguments),
            )
            try:
                matched = hook.matches(ctx)
            except Exception as exc:
                raise HookExecutionError(hook.name, hook.event, exc) from exc
            if not matched:
                traces.append(
                    HookTraceItem(
                        hook_name=hook.name,
                        event=hook.event,
                        matched=False,
                    )
                )
                continue
            try:
                outcome = await self._bounded_hook_run(hook, ctx)
            except HookExecutionError:
                raise
            except Exception as exc:
                raise HookExecutionError(hook.name, hook.event, exc) from exc
            if outcome.updated_input is not None:
                current_arguments = dict(outcome.updated_input)
            if outcome.extra_message:
                extra_messages.append(outcome.extra_message)
            traces.append(
                HookTraceItem(
                    hook_name=hook.name,
                    event=hook.event,
                    matched=True,
                    decision=outcome.decision,
                    reason=outcome.reason,
                    extra_message=outcome.extra_message,
                )
            )
            if outcome.decision == "deny":
                reason = outcome.reason.strip() or "工具调用被拦截"
                return reason, current_arguments
        return "", current_arguments

    async def _run_post_hooks(
        self,
        ctx: HookContext,
        *,
        extra_messages: list[str],
        traces: list[HookTraceItem],
        fail_open: bool = False,
    ) -> None:
        for hook in self._runtime_hooks():
            if hook.event != ctx.event:
                continue
            try:
                matched = hook.matches(ctx)
            except Exception as exc:
                if fail_open:
                    traces.append(
                        HookTraceItem(
                            hook_name=hook.name,
                            event=hook.event,
                            matched=False,
                            reason=f"hook failed: {exc}",
                        )
                    )
                    continue
                raise HookExecutionError(hook.name, hook.event, exc) from exc
            if not matched:
                traces.append(
                    HookTraceItem(
                        hook_name=hook.name,
                        event=hook.event,
                        matched=False,
                    )
                )
                continue
            try:
                outcome = await self._bounded_hook_run(hook, ctx)
            except HookExecutionError as exc:
                # fanout 层（post_tool_use/post_tool_error）：超时/异常记录
                # 失败后继续，不改写已提交的工具结果与终态。
                if fail_open:
                    traces.append(
                        HookTraceItem(
                            hook_name=hook.name,
                            event=hook.event,
                            matched=True,
                            reason=f"hook failed: {exc}",
                        )
                    )
                    continue
                raise
            except Exception as exc:
                if fail_open:
                    traces.append(
                        HookTraceItem(
                            hook_name=hook.name,
                            event=hook.event,
                            matched=True,
                            reason=f"hook failed: {exc}",
                        )
                    )
                    continue
                raise HookExecutionError(hook.name, hook.event, exc) from exc
            if outcome.extra_message:
                extra_messages.append(outcome.extra_message)
            traces.append(
                HookTraceItem(
                    hook_name=hook.name,
                    event=hook.event,
                    matched=True,
                    decision=outcome.decision,
                    reason=outcome.reason,
                    extra_message=outcome.extra_message,
                )
            )
